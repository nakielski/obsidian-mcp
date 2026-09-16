"""MCP server exposing an Obsidian vault to LLM clients via Tools, Resources, and Prompts."""
from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from pydantic import BaseModel, Field, RootModel

from . import policies
from .vault import Note, Vault

# ---- Module-level singleton Vault instance ----------------------------

_vault_instance: Vault | None = None


def _get_vault() -> Vault:
    """Get or create the singleton Vault instance."""
    global _vault_instance
    if _vault_instance is None:
        root = os.environ.get("OBSIDIAN_VAULT_ROOT")
        if not root:
            import pathlib
            root = str(pathlib.Path(__file__).resolve().parents[2] / "examples" / "mock-vault")
        _vault_instance = Vault(root)
    return _vault_instance


def set_vault(vault: Vault) -> None:
    """Inject a Vault instance (for testing)."""
    global _vault_instance
    _vault_instance = vault


mcp = FastMCP("obsidian-mcp")


# ---- helpers -----------------------------------------------------------

def _note_to_dict(n: Note) -> dict:
    return {
        "path": n.path,
        "title": n.title,
        "tags": n.tags,
        "metadata": n.metadata,
        "content": n.content,
    }


def _actionable_message(e: Exception) -> str:
    """Return an actionable error message for the AI based on exception type."""
    text = str(e)
    if isinstance(e, FileNotFoundError):
        return (
            f"Note not found: {text}. "
            "Verify the vault-relative path, or use list_notes / list_note_paths to see available notes."
        )
    if isinstance(e, FileExistsError):
        return (
            f"Note already exists: {text}. "
            "Use update_note to overwrite the body, or choose a different path."
        )
    if isinstance(e, PermissionError):
        return (
            f"Path is outside the vault or not allowed: {text}. "
            "Use a vault-relative path (e.g., 'Inbox/Note.md') and avoid '..' or absolute paths."
        )
    if isinstance(e, ValueError):
        return (
            f"Invalid value: {text}. "
            "Check the parameter format and allowed values, then try again."
        )
    return f"Unexpected error: {text}. Review the request and retry, or inspect the vault state."


def _to_mcp_error(e: Exception) -> McpError:
    """Construct McpError correctly with ErrorData."""
    if isinstance(e, (FileNotFoundError, ValueError)):
        code = -32602
    elif isinstance(e, PermissionError):
        code = -32603
    elif isinstance(e, FileExistsError):
        code = -32603
    else:
        code = -32603
    return McpError(ErrorData(
        code=code,
        message=_actionable_message(e),
        data={"error_type": type(e).__name__},
    ))


# ---- output schema models ----------------------------------------------

class ReadNoteOutput(BaseModel):
    path: str
    title: str
    tags: list[str]
    metadata: dict[str, Any]
    content: str

class SearchResultItem(BaseModel):
    path: str
    title: str
    tags: list[str]

class SearchNotesOutput(RootModel[list[SearchResultItem]]):
    pass

class ListNotesOutput(RootModel[list[SearchResultItem]]):
    pass

class ListNotePathsOutput(RootModel[list[str]]):
    pass

class CreateNoteOutput(BaseModel):
    created: str

class UpdateNoteOutput(BaseModel):
    updated: str

class DeleteNoteOutput(BaseModel):
    deleted: str

class ArchiveNoteOutput(BaseModel):
    archived: str
    backlinks: list[str]

class SearchQueryItem(BaseModel):
    path: str
    title: str
    score: float
    tags: list[str]
    folder: str
    typ: str
    status: str
    uid: str

class SearchQueryOutput(BaseModel):
    query: str
    corrected: str
    total: int
    results: list[SearchQueryItem]

class RebuildIndexOutput(BaseModel):
    indexed: int
    index_dir: str

class IndexStatusOutput(BaseModel):
    indexed_docs: int
    vault_notes: int
    index_dir: str | None = None
    in_sync: bool
    enabled: bool = True

class AppendToNoteOutput(BaseModel):
    appended: bool
    path: str
    created: bool

class BacklinkItem(BaseModel):
    path: str
    title: str

class GetBacklinksOutput(RootModel[list[BacklinkItem]]):
    pass

class FrontmatterGetOutput(BaseModel):
    key: str
    value: Any | None

class FrontmatterSetOutput(BaseModel):
    key: str
    value: Any
    path: str

class FrontmatterDeleteOutput(BaseModel):
    key: str
    deleted: bool
    path: str

ManageFrontmatterOutput = FrontmatterGetOutput | FrontmatterSetOutput | FrontmatterDeleteOutput

class ManageTagsOutput(BaseModel):
    path: str
    tags: list[str]

class PatchNoteOutput(BaseModel):
    path: str
    heading: str
    action: str

class DailyNoteReadExistsOutput(BaseModel):
    date: str
    exists: bool
    note: dict[str, Any]

class DailyNoteReadMissingOutput(BaseModel):
    date: str
    exists: bool

class DailyNoteAppendOutput(BaseModel):
    date: str
    appended: bool
    created: bool

DailyNoteOutput = DailyNoteReadExistsOutput | DailyNoteReadMissingOutput | DailyNoteAppendOutput

class SearchAndReplaceOutput(BaseModel):
    path: str
    replacements: int
    new_content_preview: str

class GraphNode(BaseModel):
    path: str
    title: str
    tags: list[str]

class GraphEdge(BaseModel):
    source: str
    target: str
    target_exists: bool

class GraphStats(BaseModel):
    total_notes: int
    total_edges: int
    orphan_count: int
    broken_link_count: int

class VaultGraphOutput(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: GraphStats

class OrphanItem(BaseModel):
    path: str
    title: str

class FindOrphansOutput(RootModel[list[OrphanItem]]):
    pass

class GetOutlinksOutput(RootModel[list[str]]):
    pass

class HealthCheckItem(BaseModel):
    count: int
    items: list[Any]

class VaultHealthOutput(BaseModel):
    score: int | float
    total_notes: int
    checks: dict[str, HealthCheckItem]


# ---- TOOLS (actions the LLM can call) ----------------------------------

@mcp.tool()
def read_note(
    path: str = Field(
        description="Vault-relative path to the note file. Must end with .md. Example: 'Inbox/2026-07-20.md'."
    ),
) -> ReadNoteOutput:
    """Read a note from the Obsidian vault.

    Use this when the user wants to view the full content, metadata (frontmatter),
    or tags of a specific note. Also useful to verify a note exists before editing.

    Returns a JSON object with keys: path, title, tags, metadata, content.
    Fails with an MCP error if the note does not exist.

    Side effects: READ-ONLY and idempotent.
    """
    try:
        n = _get_vault().read(path)
        return _note_to_dict(n)
    except (FileNotFoundError, PermissionError, ValueError) as e:
        raise _to_mcp_error(e)
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def search_notes(
    query: str = Field(
        description="Free-text search term. Searches titles and Markdown bodies case-insensitively. Example: 'Qdrant'."
    ),
) -> SearchNotesOutput:
    """Search the vault for notes matching a full-text query.

    Use this when the user asks to find notes about a keyword or topic but does
    not name a specific file.

    Returns a JSON array of objects with keys: path, title, tags.
    An empty query or no matches returns an empty JSON array.

    Side effects: READ-ONLY.
    """
    hits = _get_vault().search(query)
    summary = [
        {"path": h.path, "title": h.title, "tags": h.tags} for h in hits
    ]
    return SearchNotesOutput(summary)


@mcp.tool()
def list_notes(
    folder: str = Field(
        default="",
        description="Subfolder path relative to the vault root. Empty string (default) lists the entire vault. Example: 'Projects'.",
    ),
) -> ListNotesOutput:
    """List all notes in the vault, optionally scoped to a subfolder.

    Use this when the user wants an overview of available notes, or to confirm a
    path exists before reading or creating a note.

    Returns a JSON array of objects with keys: path, title, tags.

    Side effects: READ-ONLY.
    """
    try:
        notes = _get_vault().list_notes(folder)
    except PermissionError as e:
        raise _to_mcp_error(e)
    summary = [
        {"path": n.path, "title": n.title, "tags": n.tags} for n in notes
    ]
    return ListNotesOutput(summary)


@mcp.tool()
def list_note_paths(
    folder: str = Field(
        default="",
        description="Subfolder path relative to the vault root. Empty string (default) returns paths for the entire vault. Example: 'Projects'.",
    ),
) -> ListNotePathsOutput:
    """List note paths only, without parsing content or frontmatter.

    Use this for structure queries or when you only need file paths, not titles
    or content. Faster than list_notes for large vaults.

    Returns a JSON array of vault-relative path strings.

    Side effects: READ-ONLY.
    """
    try:
        paths = _get_vault().list_note_paths(folder)
    except PermissionError as e:
        raise _to_mcp_error(e)
    return ListNotePathsOutput(paths)


@mcp.tool()
def create_note(
    path: str = Field(
        description="Vault-relative path for the new note. Must end with .md. Example: 'Inbox/my-note.md'."
    ),
    content: str = Field(
        description="Markdown body content for the note. YAML frontmatter is generated automatically from tags. Example: '# My Note' with body text below. Content with leading YAML frontmatter is parsed as the note's frontmatter; wiki/ notes require a uid field."
    ),
    tags: list[str] | None = Field(
        default=None,
        description="Optional list of tags to store in the note's YAML frontmatter. Defaults to None (no tags). Example: ['mcp', 'ai'].",
    ),
) -> CreateNoteOutput:
    """Create a new note with YAML frontmatter.

    Use this when the user wants to add a brand-new note that does not already
    exist.

    Returns a JSON object: {"created": "<vault-relative-path>"}.
    Fails with an MCP error if the note already exists or the path is invalid.

    Side effects: WRITES a new .md file to disk. Not idempotent if the file exists.
    Leading YAML frontmatter in content is parsed; wiki/ notes must include a uid field.
    """
    try:
        n = _get_vault().create(path, content, tags)
        return {"created": n.path}
    except (PermissionError, FileExistsError, ValueError, OSError) as e:
        raise _to_mcp_error(e)


@mcp.tool()
def update_note(
    path: str = Field(
        description="Vault-relative path of the note to update. Example: 'Inbox/2026-07-20.md'."
    ),
    content: str = Field(
        description="New Markdown body to replace the existing body. YAML frontmatter and tags are preserved. Example: '# Updated Title' with new content below."
    ),
) -> UpdateNoteOutput:
    """Replace the Markdown body of an existing note while preserving YAML frontmatter.

    Use this when the user wants to rewrite the content of an existing note
    without changing its metadata or tags.

    Returns a JSON object: {"updated": "<vault-relative-path>"}.
    Fails with an MCP error if the note does not exist.

    Side effects: DESTRUCTIVE to the existing note body (overwrites it).
    Frontmatter and tags are preserved.
    """
    try:
        n = _get_vault().update(path, content)
        return {"updated": n.path}
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def get_backlinks(
    title: str = Field(
        description="Title of the target note to search for in [[wikilinks]]. Example: 'Second Brain Agent'."
    ),
) -> GetBacklinksOutput:
    """Find all notes that link to the given note via [[wikilinks]].

    Use this when the user asks which notes reference a specific note or topic.

    Returns a JSON array of objects with keys: path, title.
    An empty result means no notes link to that title.

    Side effects: READ-ONLY.
    """
    hits = _get_vault().get_backlinks(title)
    summary = [{"path": h.path, "title": h.title} for h in hits]
    return GetBacklinksOutput(summary)


@mcp.tool()
def delete_note(
    path: str = Field(
        description="Vault-relative path of the note to delete. Example: 'Inbox/old-note.md'."
    ),
) -> DeleteNoteOutput:
    """Permanently delete a note from the vault. This action cannot be undone.

    Use this when the user wants to remove a note entirely.

    Returns a JSON object: {"deleted": "<vault-relative-path>"}.
    Fails with an MCP error if the note does not exist.

    Side effects: DESTRUCTIVE — permanently removes the file from disk.
    """
    try:
        deleted = _get_vault().delete(path)
        return {"deleted": deleted}
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def archive_note(
    path: str = Field(
        description="Vault-relative path of the note to archive. Example: 'wiki/concepts/old.md'."
    ),
    archive_dir: str = Field(
        default="06 Archive",
        description="Archive folder inside the vault. Default: '06 Archive'.",
    ),
) -> ArchiveNoteOutput:
    """Archive a note instead of deleting it: move it to the archive folder.

    Use this as the DEFAULT removal action. The vault rule is "never delete,
    archive" — this tool moves the note and reports which notes still link to
    it so their [[wikilinks]] can be updated or marked as archived.

    Returns {"archived": "<new-path>", "backlinks": ["<linking note paths>"]}.
    Fails with an MCP error if the note does not exist or the archive move
    would overwrite an existing file.

    Side effects: MOVES the file; does not touch the linking notes.
    """
    try:
        v = _get_vault()
        note = v.read(path)
        incoming = [
            b.path for b in v.get_backlinks(note.title)
        ]
        target = policies.archive_target(path, archive_dir)
        if v._resolve(target).exists():
            raise FileExistsError(
                f"Archive target already exists: {target}. Rename the note or "
                "clear the archive target first."
            )
        src = v._resolve(path)
        dst = v._resolve(target)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        v._after_write(path, "archive", None)
        result: dict = {"archived": target, "backlinks": incoming}
        # keep the wiki index in sync when archiving wiki notes
        policies.update_wiki_index(
            v.root, path, note.title, "", "", action="archive"
        )
        return result
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def append_to_note(
    path: str = Field(
        description="Vault-relative path of the note. Example: 'Inbox/Journal.md'."
    ),
    content: str = Field(
        description="Markdown content to append to the end of the note. Example: '- New idea'. Content with leading YAML frontmatter is parsed as the note's frontmatter; wiki/ notes require a uid field."
    ),
) -> AppendToNoteOutput:
    """Append Markdown content to the end of a note, creating it if it does not exist.

    Use this when the user wants to add new lines or entries to an existing note,
    or start a new note with initial content.

    Returns a JSON object: {"appended": true, "path": "<path>", "created": <bool>}.

    Side effects: WRITES to disk; creates the note if it does not already exist.
    Leading YAML frontmatter in content is parsed; wiki/ notes must include a uid field.
    """
    try:
        n, created = _get_vault().append(path, content)
        return {"appended": True, "path": n.path, "created": created}
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def manage_frontmatter(
    path: str = Field(
        description="Vault-relative path of the note. Example: 'Inbox/2026-07-20.md'."
    ),
    action: str = Field(
        description="Action to perform: 'get' (read a key), 'set' (write a key), or 'delete' (remove a key). Example: 'set'."
    ),
    key: str = Field(
        description="YAML frontmatter key name. Example: 'status'."
    ),
    value: Any = Field(
        default=None,
        description="New value when action='set'. Required for 'set'; ignored for 'get' and 'delete'. Supports str, int, float, bool, or list. Example: 'review'.",
    ),
) -> ManageFrontmatterOutput:
    """Get, set, or delete a single key in a note's YAML frontmatter.

    Use this when the user wants to read or modify metadata (e.g., status,
    priority) without touching the note body.

    Returns a JSON object:
      - get: {"key": "<key>", "value": <value>}
      - set: {"key": "<key>", "value": <value>, "path": "<path>"}
      - delete: {"key": "<key>", "deleted": true, "path": "<path>"}

    Side effects: 'set' and 'delete' modify frontmatter on disk; 'get' is READ-ONLY.
    """
    action = action.strip().lower()
    if action not in {"get", "set", "delete"}:
        raise _to_mcp_error(ValueError(
            f"Invalid frontmatter action '{action}'. "
            "Use one of: 'get', 'set', or 'delete'."
        ))
    vault = _get_vault()
    try:
        if action == "get":
            val = vault.get_frontmatter(path, key)
            return {"key": key, "value": val}
        if action == "set":
            if value is None:
                raise _to_mcp_error(ValueError(
                    "value is required when action='set'. Provide a value such as a string, number, or list."
                ))
            if not isinstance(value, (str, int, float, bool, list)):
                raise _to_mcp_error(ValueError(
                    "value must be a scalar (str, int, float, bool) or a list. "
                    "Use a JSON-compatible type for the frontmatter value."
                ))
            vault.set_frontmatter(path, key, value)
            return {"key": key, "value": value, "path": path}
        vault.delete_frontmatter(path, key)
        return {"key": key, "deleted": True, "path": path}
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def manage_tags(
    path: str = Field(
        description="Vault-relative path of the note. Example: 'Inbox/2026-07-20.md'."
    ),
    action: str = Field(
        description="Action to perform: 'add', 'remove', or 'list'. Example: 'add'."
    ),
    tags: list[str] | None = Field(
        default=None,
        description="Tags to add or remove (ignored when action='list'). Defaults to None (treated as an empty list). Example: ['mcp', 'ai'].",
    ),
) -> ManageTagsOutput:
    """Add, remove, or list tags on a note (combines frontmatter and inline #tags).

    Use this when the user wants to update or inspect note tags.

    Returns a JSON object: {"path": "<path>", "tags": [<updated-or-current-tags>]}.
    Fails with an MCP error if the action is invalid.

    Side effects: 'add' and 'remove' modify the note's frontmatter on disk;
    'list' is READ-ONLY.
    """
    action = action.strip().lower()
    if action not in {"add", "remove", "list"}:
        raise _to_mcp_error(ValueError(
            f"Invalid tag action '{action}'. Use one of: 'add', 'remove', or 'list'."
        ))
    vault = _get_vault()
    try:
        if action == "add":
            updated = vault.add_tags(path, tags or [])
            return {"path": path, "tags": updated}
        if action == "remove":
            updated = vault.remove_tags(path, tags or [])
            return {"path": path, "tags": updated}
        all_tags = vault.list_tags(path)
        return {"path": path, "tags": all_tags}
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def patch_note(
    path: str = Field(
        description="Vault-relative path of the note. Example: 'Inbox/structured.md'."
    ),
    heading: str = Field(
        description="Exact heading text to target, without leading # marks. Example: 'Section A'."
    ),
    action: str = Field(
        description="Edit action: 'append' (add after existing section body), 'prepend' (add after heading), or 'replace' (replace whole section body). Example: 'append'."
    ),
    content: str = Field(
        description="Markdown content to insert or to replace the section body with. Example: '- New bullet'."
    ),
) -> PatchNoteOutput:
    """Surgically edit content under a specific Markdown heading.

    Use this when the user wants to append, prepend, or replace text within a
    section without rewriting the entire note.

    Returns a JSON object: {"path": "<path>", "heading": "<heading>", "action": "<action>"}.
    Fails with an MCP error if the heading is not found.

    Side effects: MODIFIES the note body on disk.
    """
    action = action.strip().lower()
    if action not in {"append", "prepend", "replace"}:
        raise _to_mcp_error(ValueError(
            f"Invalid patch action '{action}'. Use one of: 'append', 'prepend', or 'replace'."
        ))
    try:
        n = _get_vault().patch_section(path, heading, action, content)
        return {"path": n.path, "heading": heading, "action": action}
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def daily_note(
    action: str = Field(
        description="Action to perform: 'read' (return the note or existence flag) or 'append' (add content). Example: 'append'."
    ),
    content: str = Field(
        default="",
        description="Markdown content to append when action='append'. Ignored when action='read'. Defaults to empty string. Example: '- Met with team'.",
    ),
    date: str = Field(
        default="",
        description="ISO date string in YYYY-MM-DD format. Defaults to today's date if omitted. Example: '2026-07-23'.",
    ),
) -> DailyNoteOutput:
    """Read or append to a daily note in the daily notes folder (YYYY-MM-DD.md).

    Use this when the user wants to view today's note, a specific date's note,
    or add an entry to a daily log.

    Returns a JSON object:
      - read existing: {"date": "<date>", "exists": true, "note": {...}}
      - read missing: {"date": "<date>", "exists": false}
      - append: {"date": "<date>", "appended": true, "created": <bool>}

    Side effects: 'append' creates or writes the daily note on disk; 'read' is READ-ONLY.
    """
    action = action.strip().lower()
    if action not in {"read", "append"}:
        raise _to_mcp_error(ValueError(
            f"Invalid daily-note action '{action}'. Use 'read' or 'append'."
        ))
    if not date:
        date = datetime.date.today().isoformat()
    vault = _get_vault()
    try:
        p = vault.daily_note_path(date)
        rel_path = str(p.relative_to(vault.root))
        if action == "read":
            if p.exists():
                n = vault.read(rel_path)
                return {"date": date, "exists": True, "note": _note_to_dict(n)}
            return {"date": date, "exists": False}
        n, created = vault.append(rel_path, content)
        return {"date": date, "appended": True, "created": created}
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def search_and_replace(
    path: str = Field(
        description="Vault-relative path of the note to edit. Example: 'Inbox/2026-07-20.md'."
    ),
    find: str = Field(
        description="Literal string or regex pattern to search for in the note body. Example: 'Hello' or a regex pattern."
    ),
    replace: str = Field(
        description="Replacement string; supports regex capture groups when use_regex=true. Example: 'Hi' or backreferences."
    ),
    use_regex: bool = Field(
        default=False,
        description="If true, treat 'find' as a regular expression pattern. Defaults to false.",
    ),
    case_sensitive: bool = Field(
        default=True,
        description="If true, matching respects uppercase/lowercase. Defaults to true.",
    ),
) -> SearchAndReplaceOutput:
    """Find and replace text in the body of an existing note.

    Use this when the user wants to change specific words, phrases, or patterns
    in a note.

    Returns a JSON object:
      {"path": "<path>", "replacements": <count>, "new_content_preview": "<first 200 chars>"}.
    Fails with an MCP error if the note does not exist.

    Side effects: MODIFIES the note body on disk.
    """
    try:
        new_content, count = _get_vault().search_and_replace(
            path, find, replace, use_regex, case_sensitive
        )
        return {
            "path": path,
            "replacements": count,
            "new_content_preview": new_content[:200],
        }
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def vault_graph() -> VaultGraphOutput:
    """Return the complete vault link graph built from [[wikilinks]].

    Use this when the user wants to understand how notes are connected, visualize
    relationships, or find hubs and isolated pages.

    Returns a JSON object with:
      - nodes: list of {path, title, tags}
      - edges: list of {source, target, target_exists}
      - stats: {total_notes, total_edges, orphan_count, broken_link_count}

    Side effects: READ-ONLY.
    """
    try:
        return _get_vault().vault_graph()
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def find_orphans() -> FindOrphansOutput:
    """Find isolated notes with no incoming or outgoing wikilinks.

    Use this when the user wants to identify disconnected notes that may need
    linking, merging, or removal.

    Returns a JSON array of objects with keys: path, title.

    Side effects: READ-ONLY.
    """
    try:
        orphans = _get_vault().find_orphans()
        return FindOrphansOutput([{"path": n.path, "title": n.title} for n in orphans])
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def get_outlinks(
    path: str = Field(
        description="Vault-relative path of the note. Example: 'Inbox/2026-07-20.md'."
    ),
) -> GetOutlinksOutput:
    """Return the outgoing [[wikilinks]] from a single note.

    Use this when the user wants to see what a note references, trace forward
    references, or verify link targets.

    Returns a JSON array of link target strings. Empty array means the note has
    no outgoing wikilinks.

    Side effects: READ-ONLY.
    """
    try:
        links = _get_vault().get_outlinks(path)
        return GetOutlinksOutput(links)
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def vault_health() -> VaultHealthOutput:
    """Analyze vault health and return a structured report.

    Use this when the user wants a quick quality overview: broken links,
    untagged notes, empty notes, TODO/FIXME markers, and duplicate titles.

    Returns a JSON object:
      {
        "score": <0-100>,
        "total_notes": <int>,
        "checks": {
          "broken_links": {"count": <int>, "items": [...]},
          "untagged_notes": {"count": <int>, "items": [...]},
          "empty_notes": {"count": <int>, "items": [...]},
          "todos": {"count": <int>, "items": [...]},
          "fixmes": {"count": <int>, "items": [...]},
          "duplicate_titles": {"count": <int>, "items": [...]}
        }
      }

    Side effects: READ-ONLY.
    """
    try:
        return _get_vault().vault_health()
    except Exception as e:
        raise _to_mcp_error(e)


# ---- PROMPTS -----------------------------------------------------------

@mcp.prompt(
    name="session-start",
    description="Review daily notes and project context before starting work",
)
def session_start(project: str = "") -> str:
    """Morning/session-start workflow template."""
    today = datetime.date.today().isoformat()
    focus = f" Focus on the '{project}' project folder." if project else ""
    return (
        f"Today is {today}. Start the work session by reading today's daily note, "
        f"listing recent notes, and checking for open tasks or TODOs.{focus} "
        "Summarize what needs attention before doing any work."
    )


@mcp.prompt(
    name="session-end",
    description="Document accomplishments, decisions, and open questions",
)
def session_end(project: str = "") -> str:
    """End-of-session workflow template."""
    today = datetime.date.today().isoformat()
    focus = f" Include updates specific to the '{project}' project." if project else ""
    return (
        f"End the work session for {today}. Append a brief session summary to "
        f"today's daily note covering: (1) what was accomplished, (2) key decisions "
        f"made, (3) open questions, and (4) next steps.{focus}"
    )


@mcp.prompt(
    name="project-checkin",
    description="Review and update a specific project's documentation",
)
def project_checkin(project: str) -> str:
    """Project documentation check-in workflow template."""
    return (
        f"Perform a documentation check-in for the '{project}' project. "
        f"List notes in the '{project}' folder, scan for TODO/FIXME items, "
        "identify outdated pages, and suggest concrete updates to keep the "
        "project docs current."
    )


# ---- RESOURCES --------------------------------------------------------

@mcp.resource("vault://structure", mime_type="application/json")
def vault_structure() -> str:
    """Return the folder/file structure of the vault as a JSON tree."""
    paths = _get_vault().list_note_paths()
    tree: dict = {}
    for path_str in paths:
        parts = path_str.split("/")
        node = tree
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = None
    return json.dumps(tree, ensure_ascii=False, indent=2)


# ---- entry point ------------------------------------------------------

# ---- indexed search tools ----------------------------------------------


@mcp.tool()
def search_query(
    query: str = Field(
        description="Query string with Lucene-style syntax. Plain terms search "
        "title+body; supported operators: field queries (title:, tags:, folder:, "
        "typ:, status:, uid:), AND/OR/NOT, phrases \"...\", wildcard *, fuzzy "
        "term~N (e.g. maier~2), ranges mtime:[2026-01-01 to 2026-12-31]. "
        "Example: 'tags:aktien AND mayer NOT crypto'."
    ),
    limit: int = Field(
        default=20, description="Maximum number of results. Default 20."
    ),
) -> SearchQueryOutput:
    """Run a ranked, tokenized full-text query against the vault index.

    Use this instead of search_notes when you need ranking, multi-word
    matching, boolean logic, field filters, fuzzy matching, or when a plain
    search_notes call returned no results for a multi-word phrase.

    Returns {"query", "corrected" (did-you-mean, empty string if none),
    "total", "results": [{path, title, score, tags, folder, typ, status, uid}]}."""

    Side effects: none (read-only). Requires VAULT_INDEX enabled (default).
    """
    try:
        v = _get_vault()
        ix = v._init_index()
        if ix is None:
            raise RuntimeError(
                "Search index unavailable (whoosh3 missing or VAULT_INDEX=0). "
                "Use search_notes for plain substring search."
            )
        results = ix.search(query, limit=limit)
        corrected = ix.corrected(query)
        out: dict = {
            "query": query,
            "corrected": corrected,
            "total": len(results),
            "results": results,
        }
        return out
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def rebuild_index() -> RebuildIndexOutput:
    """Rebuild the full-text search index from all vault notes.

    Use this after major external changes (e.g. sync restored files), or when
    search results look incomplete. Incremental updates happen automatically
    on every write; a rebuild is only needed for out-of-band changes.

    Returns {"indexed": N, "index_dir": "<path>"}.

    Side effects: rewrites the index directory (index only, never notes).
    """
    try:
        v = _get_vault()
        ix = v._init_index()
        if ix is None:
            raise RuntimeError(
                "Search index unavailable (whoosh3 missing or VAULT_INDEX=0)."
            )
        stats = ix.rebuild(v.list_notes())
        return stats
    except Exception as e:
        raise _to_mcp_error(e)


@mcp.tool()
def index_status() -> IndexStatusOutput:
    """Report search index health: document count vs. vault note count.

    Use this to verify index freshness. If counts diverge, run rebuild_index.

    Returns {"indexed_docs", "vault_notes", "index_dir", "in_sync"}.

    Side effects: none (read-only).
    """
    try:
        v = _get_vault()
        ix = v._init_index()
        if ix is None:
            return {
                "indexed_docs": 0,
                "vault_notes": len(v.list_notes()),
                "index_dir": None,
                "in_sync": False,
                "enabled": False,
            }
        indexed = ix.count()
        notes = len(v.list_notes())
        return {
            "indexed_docs": indexed,
            "vault_notes": notes,
            "index_dir": str(ix.index_dir),
            "in_sync": indexed == notes,
            "enabled": True,
        }
    except Exception as e:
        raise _to_mcp_error(e)


def main() -> None:
    """Run the MCP server on stdio."""
    root = os.environ.get("OBSIDIAN_VAULT_ROOT", "")
    if not root:
        print("[obsidian-mcp] No OBSIDIAN_VAULT_ROOT set, using mock vault.", file=sys.stderr)
    _get_vault()
    mcp.run()


if __name__ == "__main__":
    main()
