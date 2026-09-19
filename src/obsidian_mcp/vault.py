"""Vault filesystem operations: read, search, create, update, and manage notes."""
from __future__ import annotations

import datetime
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from . import policies

try:
    from .search_index import VaultIndex
except ImportError:
    VaultIndex = None


@dataclass
class Note:
    """Represents a single Obsidian note."""

    path: str
    title: str
    content: str
    metadata: dict
    tags: list[str]


# ---- serialization helpers ---------------------------------------------

def _require_wiki_uid(rel_path: str, metadata: dict) -> None:
    """Reject wiki note creation without a non-empty uid field.

    Exemptions:
    - Non-wiki layers (raw, para, …) — no uid required.
    - wiki/log/ paths — log files carry no uid by convention.
    - wiki/index.json — index file, not a note; also written directly by
      policies.update_wiki_index, which never routes through create/append.
    - update() — body-only replacement, existing FM is preserved; caller is exempt.
    """
    norm = rel_path.replace("\\", "/")
    if policies.classify_layer(norm) != "wiki":
        return
    if norm.startswith("wiki/log/"):
        return
    if norm == "wiki/index.json":
        return
    uid = metadata.get("uid", "")
    if not str(uid).strip():
        raise ValueError(
            f"wiki note requires uid: {rel_path}. "
            "Add 'uid: <unique-id>' to the frontmatter."
        )


def _serialize_value(v):
    """Recursively convert YAML-parsed values to JSON-serializable types."""
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _serialize_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_serialize_value(i) for i in v]
    return v


def _serialize_metadata(metadata: dict) -> dict:
    """Return a JSON-safe copy of frontmatter metadata."""
    return {k: _serialize_value(v) for k, v in metadata.items()}


# ---- text extraction helpers -------------------------------------------

def _strip_code_blocks(content: str) -> str:
    """Remove fenced (```...```) and inline (`...`) code for safe parsing."""
    cleaned = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    cleaned = re.sub(r"`[^`]+`", "", cleaned)
    return cleaned


def _extract_title(path: Path, content: str) -> str:
    """Title = first H1 in markdown (ignoring code blocks), else filename."""
    safe_content = _strip_code_blocks(content)
    m = re.search(r"^#\s+(.+)$", safe_content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return path.stem


def _extract_inline_tags(content: str) -> set[str]:
    """Extract inline #tags (not in code blocks, not in frontmatter)."""
    cleaned = _strip_code_blocks(content)
    return {m.group(1).lower() for m in re.finditer(r"(?<!\w)#([a-zA-Z][\w/-]+)", cleaned)}


def _extract_wikilinks(content: str) -> list[str]:
    """Extract wikilink targets from note content, excluding code blocks.

    Returns deduplicated target strings preserving first-seen order.
    """
    cleaned = _strip_code_blocks(content)
    pattern = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
    seen: set[str] = set()
    results: list[str] = []
    for match in re.finditer(pattern, cleaned):
        target = match.group(1).strip()
        if not target:
            continue
        key = target.lower()
        if key not in seen:
            seen.add(key)
            results.append(target)
    return results


def _load_capped(p: Path):
    """Enforce the note size cap on every frontmatter load.

    load_note() already caps, but every RMW method parses via
    frontmatter.load directly — a >2 MiB file sailed through those paths
    unbounded.
    """
    if p.stat().st_size > _MAX_NOTE_BYTES:
        raise ValueError(
            f"Note exceeds the {_MAX_NOTE_BYTES // (1024 * 1024)} MiB size cap: "
            f"{p.name} ({p.stat().st_size} bytes)"
        )
    return frontmatter.load(p)

def _fence_flags(lines: list[str]) -> list[bool]:
    """One O(n) pass — fence state per line.

    Returns flags[i] = True when lines[i] is INSIDE a fenced code block.
    Fences must start with <4 spaces of indentation (CommonMark); a ```
    inside a 4-space indented code block is code, not a fence toggle.
    """
    flags = [False] * len(lines)
    fence = None
    for i, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        if fence is not None:
            flags[i] = True
            if indent < 4 and stripped.startswith(fence):
                fence = None
        elif indent < 4 and (stripped.startswith("```") or stripped.startswith("~~~")):
            fence = stripped[:3]
            # the opening fence line itself counts as inside
            flags[i] = True
    return flags


# Cap note file size before parsing. This bounds the worst-case parse
# cost per note (pathologically large files slow every list-based tool).
# It is NOT a YAML alias-bomb guard: PyYAML aliases are shared references,
# they do not expand during load (verified empirically).
_MAX_NOTE_BYTES = 2 * 1024 * 1024  # 2 MiB


def load_note(full_path: Path, vault_root: Path) -> Note:
    """Parse a .md file into a Note object."""
    # S13: reject oversized notes before YAML/frontmatter parsing —
    # bounds per-note parse cost (see _MAX_NOTE_BYTES note above).
    if full_path.stat().st_size > _MAX_NOTE_BYTES:
        raise ValueError(
            f"Note exceeds the {_MAX_NOTE_BYTES // (1024 * 1024)} MiB size cap: "
            f"{full_path.name} ({full_path.stat().st_size} bytes)"
        )
    post = frontmatter.load(full_path)
    content = post.content
    rel = str(full_path.relative_to(vault_root))
    fm_tags = post.metadata.get("tags", [])
    if isinstance(fm_tags, str):
        fm_tags = [fm_tags]
    fm_tag_set = {str(t).lower() for t in fm_tags}
    all_tags = sorted(fm_tag_set | _extract_inline_tags(content))
    return Note(
        path=rel,
        title=_extract_title(full_path, content),
        content=content,
        metadata=_serialize_metadata(dict(post.metadata)),
        tags=all_tags,
    )


class Vault:
    """High-level access to an Obsidian vault directory."""

    def _init_index(self):
        """Lazily create the search index (VAULT_INDEX=0 disables)."""
        if getattr(self, "_index", None) is not None:
            return self._index
        if VaultIndex is None or not policies._flag("VAULT_INDEX"):
            self._index = None
            return None
        try:
            from .search_index import default_index_dir

            self._index = VaultIndex(self.root, default_index_dir(self.root))
        except Exception:
            self._index = None
        return self._index

    # Internal bookkeeping files never reachable via tools (deny-list).
    # Component names, case-insensitive; '.'/'..' already rejected above.
    # Checked on BOTH the input segments and the resolved path (F1: a
    # symlink 'Inbox/link' -> '.vault-write-log.jsonl' resolves to the real
    # file, so an input-only check is bypassable).
    _DENY_COMPONENTS = frozenset(
        {
            ".vault-write-log.jsonl",  # legacy audit log (write path)
            ".vault-write-log.d",      # per-actor audit shards
            ".vault-lock",             # advisory lock file
            ".obsidian-mcp-index",     # Whoosh index dir
            ".obsidian",               # Obsidian app config dir (any depth)
        }
    )

    def _deny_check_resolved(self, candidate: Path, original: str) -> None:
        """F1: re-run the deny-list on the RESOLVED absolute path.

        resolve() follows symlinks, so a link planted in an allowed folder
        that points at a denied file must be caught here. Hardlinks cannot
        be caught by name (same inode, different name) — that residual risk
        requires filesystem access and is documented in DEPLOYMENT.md.
        """
        try:
            parts = [s.casefold() for s in candidate.relative_to(self.root).parts]
        except ValueError:
            return  # outside the vault — containment check handles this
        for banned in self._DENY_COMPONENTS:
            if banned in parts:
                raise PermissionError(
                    f"Access denied: '{banned}' is an internal bookkeeping "
                    "file of the vault server and not accessible via tools "
                    f"({original})."
                )

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Vault root not found: {self.root}")
        # Serializes read-modify-write cycles (append, patch_section,
        # search_and_replace, set_frontmatter, add/remove_tags) within this
        # process. NOTE: FastMCP 1.29.x runs sync tools INLINE on the event
        # loop, so in-process parallelism is currently
        # impossible — this lock is future-proofing for async/threaded
        # dispatch and protects library consumers. Cross-process safety is
        # handled by .vault-lock + the B3 index flock.
        self._rw_lock = threading.RLock()

    # ---- helpers -----------------------------------------------------

    def _resolve(self, rel_path: str) -> tuple[Path, str]:
        """Resolve a file path safely and return (absolute_path, normalized_rel).

        Security invariants:
        - absolute paths rejected; '.'/'..' segments rejected BEFORE resolution
          (prevents suffix-append escapes like 'Inbox/..' writing outside the vault)
        - containment checked after resolve AND re-checked after suffix handling
        - normalized_rel is derived from the resolved absolute path, never from
          the raw input string, so layer policies classify the actual target
          ('./raw/x.md', ' raw/x.md', 'raw//x.md' all normalize to 'raw/x.md')
        """
        raw = rel_path.strip()
        if not raw:
            raise ValueError("Path must not be empty")
        if Path(raw).is_absolute():
            raise PermissionError(f"Absolute paths not allowed: {rel_path}")
        norm_input = raw.replace("\\", "/")
        # Split on '/' ourselves: PurePosixPath collapses '.' segments, so
        # checking its parts would silently accept 'Inbox/.' (resolving to a
        # root-level Inbox.md). Reject explicit '.'/'..' segments instead.
        segments = [s for s in norm_input.split("/") if s]
        if ".." in segments or "." in segments:
            raise PermissionError(
                f"'.' and '..' path segments are not allowed: {rel_path}. "
                "Use a plain vault-relative path (e.g. 'Inbox/Note.md')."
            )
        # Deny-list: internal bookkeeping files are never tool-accessible.
        # Component-based (not prefix-based) so nested paths cannot smuggle
        # these names past the check ('Inbox/.obsidian/app.json' is blocked
        # too). Casefolded for case-insensitive filesystem parity. The write
        # log itself stays readable: its audit trail must be inspectable.
        lowered = [s.casefold() for s in segments]
        for banned in self._DENY_COMPONENTS:
            if banned in lowered:
                raise PermissionError(
                    f"Access denied: '{banned}' is an internal bookkeeping "
                    "file of the vault server and not accessible via tools "
                    f"({rel_path})."
                )
        if lowered and lowered[0] == ".obsidian":
            raise PermissionError(
                f"Access denied: '.obsidian' is the Obsidian app config dir "
                f"and not accessible via tools ({rel_path})."
            )
        candidate = (self.root / norm_input).resolve()
        candidate = self._contained(candidate, rel_path)
        self._deny_check_resolved(candidate, rel_path)
        if not candidate.suffix:
            candidate = candidate.with_suffix(".md")
        candidate = self._contained(candidate, rel_path)
        self._deny_check_resolved(candidate, rel_path)
        # F4: wiki/index.json exactly (legit 'sources/index.json' stays usable)
        rel_norm = str(candidate.relative_to(self.root)).replace("\\", "/")
        # The wiki index family by exact path AND by prefix —
        # 'wiki/index.json.lock/x.md' would create a DIRECTORY named like
        # the lock file and permanently kill index updates.
        lowered_rel = rel_norm.casefold()
        if (
            lowered_rel in {"wiki/index.json", "wiki/index.json.lock", "wiki/index.json.tmp"}
            or lowered_rel.startswith("wiki/index.json.")
        ):
            raise PermissionError(
                f"Access denied: '{rel_norm}' is derived bookkeeping "
                f"and not accessible via tools ({rel_path})."
            )
        return candidate, rel_norm

    def _is_daily_note(self, norm_rel: str) -> bool:
        """True when the normalized path sits inside OBSIDIAN_DAILY_DIR."""
        daily_dir = os.environ.get("OBSIDIAN_DAILY_DIR", "Daily").replace("\\", "/").strip("/")
        if not daily_dir:
            return False
        return norm_rel.replace("\\", "/").casefold().startswith(daily_dir.casefold() + "/")

    def _contained(self, candidate: Path, original: str) -> Path:
        """Raise PermissionError unless candidate sits inside the vault root."""
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Path escapes vault root: {original}")
        return candidate

    def _resolve_folder(self, folder: str) -> Path:
        """Resolve a folder path safely (no traversal escapes, deny-list enforced)."""
        folder = folder.strip()
        if not folder:
            return self.root
        if Path(folder).is_absolute():
            raise PermissionError(f"Absolute folder paths not allowed: {folder}")
        norm = folder.replace("\\", "/")
        segments = [s for s in norm.split("/") if s]
        if ".." in segments or "." in segments:
            raise PermissionError(
                f"'.' and '..' path segments are not allowed: {folder}."
            )
        lowered = [s.casefold() for s in segments]
        for banned in self._DENY_COMPONENTS:
            if banned in lowered:
                raise PermissionError(
                    f"Access denied: '{banned}' is internal bookkeeping "
                    f"and not accessible via tools ({folder})."
                )
        if lowered and lowered[0] == ".obsidian":
            raise PermissionError(
                f"Access denied: '.obsidian' is the Obsidian app config dir "
                f"and not accessible via tools ({folder})."
            )
        candidate = (self.root / norm).resolve()
        self._deny_check_resolved(candidate, folder)
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Folder escapes vault root: {folder}")
        return candidate

    @staticmethod
    def _is_in_trash(p: Path, vault_root: Path) -> bool:
        """Check whether a path sits inside the vault's .trash folder."""
        try:
            return ".trash" in p.relative_to(vault_root).parts
        except ValueError:
            return True

    def _is_hidden_internal(self, p: Path) -> bool:
        """F2: exclude internal/bookkeeping paths from all listings.

        rglob walks see everything on disk; without this filter .obsidian/
        plugin docs and audit shards would leak into list_notes, search,
        graph, health and the Whoosh index.
        """
        try:
            parts = [s.casefold() for s in p.relative_to(self.root).parts]
        except ValueError:
            return True
        if parts and parts[0] in {".obsidian", ".trash", ".vault-write-log.d", ".obsidian-mcp-index"}:
            return True
        return any(c in parts for c in (".vault-write-log.jsonl", ".vault-lock"))

    @staticmethod
    def _normalize_tags(tags) -> list[str]:
        """Normalize a tag list to lowercase strings."""
        if isinstance(tags, str):
            return [tags.lower()]
        return [str(t).lower() for t in tags]

    # ---- lightweight API (no full file parse) ------------------------

    def list_note_paths(self, folder: str = "") -> list[str]:
        """Return relative note paths WITHOUT parsing file contents."""
        base = self._resolve_folder(folder)
        return [
            str(p.relative_to(self.root))
            for p in sorted(base.rglob("*.md"))
            if not self._is_in_trash(p, self.root) and not self._is_hidden_internal(p)
        ]

    # ---- full API ----------------------------------------------------

    def read(self, rel_path: str) -> Note:
        p, _ = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        return load_note(p, self.root)

    def list_notes(self, folder: str = "") -> list[Note]:
        base = self._resolve_folder(folder)
        results = []
        for p in sorted(base.rglob("*.md")):
            if self._is_in_trash(p, self.root):
                continue
            if self._is_hidden_internal(p):
                continue
            try:
                results.append(load_note(p, self.root))
            except Exception:
                continue
        return results

    def search(self, query: str, limit: int = 100) -> list[Note]:
        """Case-insensitive substring search over titles and bodies.

        Bounded result set — an empty/broad query no longer returns the
        entire vault (unbounded payload on large vaults).
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        q = query.lower()
        if not q:
            return []
        out: list[Note] = []
        for n in self.list_notes():
            if q in n.title.lower() or q in n.content.lower():
                out.append(n)
                if len(out) >= limit:
                    break
        return out

    def create(self, rel_path: str, content: str, tags: list[str] | None = None) -> Note:
        policies.assert_no_foreign_lock(self.root)
        p, norm = self._resolve(rel_path)
        if p.exists():
            raise FileExistsError(f"Note already exists: {rel_path}")
        # Parse FM before mkdir so a rejected create leaves no files or directories.
        # Content may already carry YAML frontmatter — parse it instead of
        # wrapping it a second time (frontmatter.Post would treat it as body).
        if content.lstrip().startswith("---"):
            post = frontmatter.loads(content)
        else:
            post = frontmatter.Post(content)
        if tags:
            existing = post.metadata.get("tags", [])
            if isinstance(existing, str):
                existing = [existing]
            merged = sorted({str(t).lower() for t in list(existing) + list(tags)})
            post["tags"] = merged
        _require_wiki_uid(norm, dict(post.metadata))
        # Reject oversized content BEFORE writing — an orphan file that
        # no tool can read back would otherwise be left on disk.
        if len(frontmatter.dumps(post).encode("utf-8")) > _MAX_NOTE_BYTES:
            raise ValueError(
                f"Note exceeds the {_MAX_NOTE_BYTES // (1024 * 1024)} MiB size cap: {rel_path}"
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        note = load_note(p, self.root)
        self._after_write(norm, "create", note)
        return note

    def update(self, rel_path: str, content: str) -> Note:
        with self._rw_lock:
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            policies.assert_mutable(norm)
            if not p.exists():
                raise FileNotFoundError(f"Note not found: {rel_path}")
            if not p.is_file():
                raise ValueError(f"Not a file: {rel_path}")
            post = _load_capped(p)
            post.content = content
            # Cap BEFORE writing — writing first made the note
            # permanently unreadable (load_note then always raises).
            if len(frontmatter.dumps(post).encode("utf-8")) > _MAX_NOTE_BYTES:
                raise ValueError(
                    f"Note exceeds the {_MAX_NOTE_BYTES // (1024 * 1024)} MiB size cap: {rel_path}"
                )
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            note = load_note(p, self.root)
            self._after_write(norm, "modify", note)
            return note

    def delete(self, rel_path: str) -> str:
        """Permanently delete a note. Returns the vault-relative path."""
        policies.assert_no_foreign_lock(self.root)
        p, norm = self._resolve(rel_path)
        policies.assert_mutable(norm)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        # S6: capture the uid before unlink — delete must log what it deletes
        try:
            deleted_note = load_note(p, self.root)
        except Exception:
            deleted_note = None
        p.unlink()
        self._after_write(norm, "delete", deleted_note)
        return norm

    def append(self, rel_path: str, content: str) -> tuple[Note, bool]:
        with self._rw_lock:
            """Append content to an existing note, or create it if missing."""
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            created = False
            if p.exists():
                if not p.is_file():
                    raise ValueError(f"Not a file: {rel_path}")
                # Daily notes are journaling: appending entries to an existing
                # daily note must work even when OBSIDIAN_DAILY_DIR points into
                # a policy-protected layer (e.g. 'raw/Daily'). Other raw/ notes
                # stay immutable (S3).
                if not self._is_daily_note(norm):
                    policies.assert_mutable(norm)
                post = _load_capped(p)
                existing = post.content or ""
                if existing:
                    post.content = existing.rstrip("\n") + "\n" + content
                else:
                    post.content = content
                p.write_text(frontmatter.dumps(post), encoding="utf-8")
            else:
                if content.lstrip().startswith("---"):
                    post = frontmatter.loads(content)
                else:
                    post = frontmatter.Post(content)
                _require_wiki_uid(norm, dict(post.metadata))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(frontmatter.dumps(post), encoding="utf-8")
                created = True
            note = load_note(p, self.root)
            self._after_write(norm, "create" if created else "modify", note)
            return note, created

        # ---- frontmatter management --------------------------------------

    def get_frontmatter(self, rel_path: str, key: str):
        """Return the value of a frontmatter key, or None if not set."""
        p, _ = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        post = _load_capped(p)
        return _serialize_value(post.metadata.get(key))

    def set_frontmatter(self, rel_path: str, key: str, value) -> None:
        with self._rw_lock:
            """Set a frontmatter key while preserving body and other metadata."""
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            policies.assert_mutable(norm)
            if not p.exists():
                raise FileNotFoundError(f"Note not found: {rel_path}")
            if not p.is_file():
                raise ValueError(f"Not a file: {rel_path}")
            post = _load_capped(p)
            post[key] = value
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            self._after_write(norm, "modify", self.read(rel_path))

    def delete_frontmatter(self, rel_path: str, key: str) -> None:
        with self._rw_lock:
            """Delete a frontmatter key if present."""
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            policies.assert_mutable(norm)
            if not p.exists():
                raise FileNotFoundError(f"Note not found: {rel_path}")
            if not p.is_file():
                raise ValueError(f"Not a file: {rel_path}")
            post = _load_capped(p)
            if key in post.metadata:
                del post.metadata[key]
                p.write_text(frontmatter.dumps(post), encoding="utf-8")
                self._after_write(norm, "modify", self.read(rel_path))

        # ---- tag management ----------------------------------------------

    def add_tags(self, rel_path: str, tags: list[str]) -> list[str]:
        with self._rw_lock:
            """Add tags to the frontmatter tags array (deduplicated, lowercase)."""
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            policies.assert_mutable(norm)
            if not p.exists():
                raise FileNotFoundError(f"Note not found: {rel_path}")
            post = _load_capped(p)
            current = self._normalize_tags(post.metadata.get("tags", []))
            for t in tags:
                tl = t.lower()
                if tl not in current:
                    current.append(tl)
            post.metadata["tags"] = current
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            self._after_write(norm, "modify", self.read(rel_path))
            return current

    def remove_tags(self, rel_path: str, tags: list[str]) -> list[str]:
        with self._rw_lock:
            """Remove tags from the frontmatter tags array case-insensitively."""
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            policies.assert_mutable(norm)
            if not p.exists():
                raise FileNotFoundError(f"Note not found: {rel_path}")
            post = _load_capped(p)
            current = self._normalize_tags(post.metadata.get("tags", []))
            remove_set = {t.lower() for t in tags}
            current = [t for t in current if t not in remove_set]
            post.metadata["tags"] = current
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            self._after_write(norm, "modify", self.read(rel_path))
            return current

    def list_tags(self, rel_path: str) -> list[str]:
        """Return all tags (frontmatter + inline), deduplicated and sorted."""
        n = self.read(rel_path)
        return sorted(set(n.tags))

    # ---- policy hooks ---------------------------------------------------

    def _after_write(self, rel_path: str, action: str, note) -> dict:
        """Run post-write policies: write log, wiki index upkeep.

        Advisory results are collected and returned; failures never break the
        completed write.
        """
        layer = policies.classify_layer(rel_path)
        result: dict = {"layer": layer}
        uid = ""
        if note is not None:
            meta = note.metadata if hasattr(note, "metadata") else {}
            uid = str(meta.get("uid", "") or "")
        result["log"] = policies.log_write(self.root, rel_path, action, layer, uid=uid)
        ix = self._init_index()
        if ix is not None:
            try:
                if action == "delete" or action == "archive" or note is None:
                    ix.remove_note(rel_path)
                    if action == "archive" and note is not None:
                        ix.index_note(note)
                else:
                    ix.index_note(note)
                result["indexed"] = True
            except Exception:
                result["indexed"] = False
        if note is not None and layer == "wiki":
            meta = note.metadata if hasattr(note, "metadata") else {}
            result["index"] = policies.update_wiki_index(
                self.root,
                rel_path,
                getattr(note, "title", "") or "",
                str(meta.get("typ", meta.get("type", "")) or ""),
                str(meta.get("status", "") or ""),
                str(meta.get("uid", "") or ""),
                action=action,
            )
        return result

    def check_note_links(self, rel_path: str) -> list[str]:
        """Return wikilink targets in a note that resolve to no known title."""
        note = self.read(rel_path)
        titles = {n.title for n in self.list_notes()}
        return policies.check_links(self.root, note.content, titles)

    # ---- section patching --------------------------------------------

    def patch_section(self, rel_path: str, heading: str, action: str, content: str) -> Note:
        with self._rw_lock:
            """Surgically edit the content under a heading."""
            if action not in {"append", "prepend", "replace"}:
                raise ValueError(f"Invalid patch action: {action}")
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            policies.assert_mutable(norm)
            if not p.exists():
                raise FileNotFoundError(f"Note not found: {rel_path}")
            if not p.is_file():
                raise ValueError(f"Not a file: {rel_path}")

            post = _load_capped(p)
            lines = post.content.splitlines()
            heading_re = re.compile(r"^(#{1,6})\s+" + re.escape(heading) + r"\s*$", re.IGNORECASE)

            # Precompute fence state per line in ONE O(n) pass. A naive
            # per-line rescan is O(n^2) and can freeze a large-note patch.
            # Fences are only fences with <4 spaces of indentation; a lone
            # ``` inside a 4-space indented code block is indented CODE, not a
            # fence toggle (CommonMark).
            fence_flags = _fence_flags(lines)

            start_idx = None
            level = None
            for i, line in enumerate(lines):
                if fence_flags[i]:
                    continue
                m = heading_re.match(line)
                if m:
                    start_idx = i
                    level = len(m.group(1))
                    break
            if start_idx is None:
                raise ValueError(f"Heading not found: {heading}")

            end_idx = len(lines)
            next_heading_re = re.compile(r"^#{1,%d}\s" % level)
            for i in range(start_idx + 1, len(lines)):
                if fence_flags[i]:
                    continue
                if next_heading_re.match(lines[i]):
                    end_idx = i
                    break

            new_lines = content.splitlines()
            if action == "append":
                updated = lines[:end_idx] + new_lines + lines[end_idx:]
            elif action == "prepend":
                updated = lines[:start_idx + 1] + new_lines + lines[start_idx + 1:]
            else:
                updated = lines[:start_idx + 1] + new_lines + lines[end_idx:]

            post.content = "\n".join(updated)
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            note = load_note(p, self.root)
            self._after_write(norm, "modify", note)
            return note

        # ---- daily notes -------------------------------------------------

    def daily_note_path(self, date: str) -> Path:
        """Resolve the path for a daily note, validating the date format."""
        if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", date):
            raise ValueError(f"Invalid date format: {date}")
        datetime.date.fromisoformat(date)
        daily_dir = os.environ.get("OBSIDIAN_DAILY_DIR", "Daily")
        if Path(daily_dir).is_absolute():
            raise PermissionError(f"Absolute daily dir not allowed: {daily_dir}")
        return self._resolve(f"{daily_dir}/{date}.md")[0]

    def archive(self, rel_path: str, archive_dir: str = "06 Archive") -> tuple[str, list[str]]:
        """Move a note into the archive folder (the default removal action).

        Public API — callers no longer orchestrate private helpers
        (_resolve/_after_write) for archiving. Enforces lock + immutability
        policies, keeps write log, Whoosh index and wiki/index.json in sync,
        and reports backlinks so callers can update [[wikilinks]].

        Returns (archive_target, backlink_paths).
        """
        with self._rw_lock:
            policies.assert_no_foreign_lock(self.root)
            note = self.read(rel_path)
            incoming = [b.path for b in self.get_backlinks(note.title)]
            target = policies.archive_target(rel_path, archive_dir)
            src, norm = self._resolve(rel_path)
            dst, _ = self._resolve(target)
            policies.assert_mutable(norm)
            if dst.exists():
                raise FileExistsError(
                    f"Archive target already exists: {target}. Rename the note or "
                    "clear the archive target first."
                )
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            self._after_write(norm, "archive", note)
            policies.update_wiki_index(
                self.root, norm, note.title, "", "", action="archive"
            )
            return target, incoming

    # ---- search and replace ------------------------------------------

    # Max pattern length and max subject length for user-supplied regex
    # (S2 ReDoS hardening). A pathological pattern on a large note could
    # burn minutes of CPU inside re.subn; bounded inputs keep the worst
    # case in the low milliseconds even for catastrophic backtracking.
    _MAX_REGEX_LEN = 500
    _MAX_NOTE_BYTES_FOR_SR = 2 * 1024 * 1024  # 2 MiB

    def search_and_replace(
        self,
        rel_path: str,
        find: str,
        replace: str,
        use_regex: bool = False,
        case_sensitive: bool = True,
    ) -> tuple[str, int]:
        """Replace occurrences of `find` in the note body."""
        with self._rw_lock:
            if not find:
                raise ValueError("find must not be empty")
            if use_regex and len(find) > self._MAX_REGEX_LEN:
                raise ValueError(
                    f"Regex pattern too long (>{self._MAX_REGEX_LEN} chars); "
                    "keep patterns small and specific."
                )
            policies.assert_no_foreign_lock(self.root)
            p, norm = self._resolve(rel_path)
            policies.assert_mutable(norm)
            if not p.exists():
                raise FileNotFoundError(f"Note not found: {rel_path}")
            if not p.is_file():
                raise ValueError(f"Not a file: {rel_path}")
            post = _load_capped(p)
            if len(post.content.encode("utf-8", errors="replace")) > self._MAX_NOTE_BYTES_FOR_SR:
                raise ValueError(
                    "Note too large for search/replace "
                    f"(>{self._MAX_NOTE_BYTES_FOR_SR // (1024 * 1024)} MiB); "
                    "split the note or edit it directly."
                )
            flags_re = 0 if case_sensitive else re.IGNORECASE
            if use_regex:
                # S2: user-supplied patterns get a real execution timeout via the
                # `regex` module — catastrophic backtracking aborts after 2s
                # instead of hanging the event loop for minutes.
                import regex as _regex

                try:
                    new_content, count = _regex.subn(
                        find,
                        replace,
                        post.content,
                        flags=0 if case_sensitive else _regex.IGNORECASE,
                        timeout=2.0,
                    )
                except _regex.error as exc:
                    raise ValueError(f"Invalid regex pattern: {exc}") from exc
                except TimeoutError as exc:
                    # `regex` raises the BUILTIN TimeoutError (no regex.TimeoutError)
                    raise ValueError(
                        "Regex timed out after 2s; simplify the pattern "
                        "(avoid nested quantifiers)."
                    ) from exc
            else:
                # B4: literal mode must treat `replace` as plain text. A callable
                # repl bypasses template processing entirely (no \1 group refs,
                # no backslash escapes like 'C:\Users' crashing the call).
                pattern = re.escape(find)
                new_content, count = re.subn(
                    pattern, lambda _m: replace, post.content, flags=flags_re
                )
            post.content = new_content
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            self._after_write(norm, "modify", load_note(p, self.root))
            return new_content, count

    def get_backlinks(self, target_title: str) -> list[Note]:
        """Find notes linking to target via [[wikilinks]], including embeds."""
        escaped = re.escape(target_title)
        pattern = re.compile(
            r"!?\[\[" + escaped + r"(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]",
            re.IGNORECASE,
        )
        return [n for n in self.list_notes() if pattern.search(n.content)]

    # ---- graph analytics ------------------------------------------------

    def vault_graph(self) -> dict:
        """Return the complete wikilink graph for the vault.

        Returns a dict with:
          - nodes: list of {path, title, tags}
          - edges: list of {source, target, target_exists}
          - stats: {total_notes, total_edges, orphan_count, broken_link_count}
        """
        notes = self.list_notes()
        title_map: dict[str, list[str]] = defaultdict(list)
        for note in notes:
            title_map[note.title.lower()].append(note.path)

        nodes = [
            {"path": note.path, "title": note.title, "tags": note.tags}
            for note in notes
        ]
        edges: list[dict] = []
        for note in notes:
            for target in _extract_wikilinks(note.content):
                target_exists = target.lower() in title_map
                target_id = title_map[target.lower()][0] if target_exists else target
                edges.append(
                    {
                        "source": note.path,
                        "target": target_id,
                        "target_exists": target_exists,
                    }
                )

        sources = {edge["source"] for edge in edges}
        targets = {edge["target"] for edge in edges if edge["target_exists"]}
        orphan_count = sum(
            1 for note in notes if note.path not in sources and note.path not in targets
        )
        broken_link_count = sum(1 for edge in edges if not edge["target_exists"])

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_notes": len(notes),
                "total_edges": len(edges),
                "orphan_count": orphan_count,
                "broken_link_count": broken_link_count,
            },
        }

    def find_orphans(self) -> list[Note]:
        """Return notes with no outgoing or incoming wikilinks."""
        graph = self.vault_graph()
        sources = {edge["source"] for edge in graph["edges"]}
        targets = {edge["target"] for edge in graph["edges"] if edge["target_exists"]}
        return [
            note
            for note in self.list_notes()
            if note.path not in sources and note.path not in targets
        ]

    def get_outlinks(self, rel_path: str) -> list[str]:
        """Return outgoing wikilink targets for a single note."""
        p, _ = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        note = load_note(p, self.root)
        return _extract_wikilinks(note.content)

    # ---- health check ---------------------------------------------------

    def vault_health(self) -> dict:
        """Analyze vault health and return a structured report."""
        notes = self.list_notes()
        total_notes = len(notes)
        title_map: dict[str, list[str]] = defaultdict(list)
        for note in notes:
            title_map[note.title.lower()].append(note.path)

        broken_links: list[dict] = []
        untagged_notes: list[str] = []
        empty_notes: list[str] = []
        todos: list[dict] = []
        fixmes: list[dict] = []

        for note in notes:
            if not note.tags:
                untagged_notes.append(note.path)
            if not note.content.strip():
                empty_notes.append(note.path)

            todo_matches = [
                match.group(0)
                for match in re.finditer(r"\bTODO\b", note.content, re.IGNORECASE)
            ]
            if todo_matches:
                todos.append({"path": note.path, "matches": todo_matches})

            fixme_matches = [
                match.group(0)
                for match in re.finditer(r"\bFIXME\b", note.content, re.IGNORECASE)
            ]
            if fixme_matches:
                fixmes.append({"path": note.path, "matches": fixme_matches})

            for target in _extract_wikilinks(note.content):
                exists = target.lower() in title_map
                if not exists:
                    broken_links.append({"path": note.path, "target": target})

        duplicate_titles = [
            {"title": title, "paths": paths}
            for title, paths in title_map.items()
            if len(paths) > 1
        ]

        score = (
            100
            - 3 * len(broken_links)
            - 1 * len(untagged_notes)
            - 5 * len(empty_notes)
            - 0.5 * len(todos)
            - 1 * len(fixmes)
            - 2 * len(duplicate_titles)
        )
        score = max(0, min(100, score))

        return {
            "score": score,
            "total_notes": total_notes,
            "checks": {
                "broken_links": {
                    "count": len(broken_links),
                    "items": broken_links,
                },
                "untagged_notes": {
                    "count": len(untagged_notes),
                    "items": untagged_notes,
                },
                "empty_notes": {
                    "count": len(empty_notes),
                    "items": empty_notes,
                },
                "todos": {"count": len(todos), "items": todos},
                "fixmes": {"count": len(fixmes), "items": fixmes},
                "duplicate_titles": {
                    "count": len(duplicate_titles),
                    "items": duplicate_titles,
                },
            },
        }
