"""Vault filesystem operations: read, search, create, update, and manage notes."""
from __future__ import annotations

import datetime
import os
import re
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


def load_note(full_path: Path, vault_root: Path) -> Note:
    """Parse a .md file into a Note object."""
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

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Vault root not found: {self.root}")

    # ---- helpers -----------------------------------------------------

    def _resolve(self, rel_path: str) -> Path:
        """Resolve a file path safely (no traversal escapes, no absolute)."""
        rel_path = rel_path.strip()
        if not rel_path:
            raise ValueError("Path must not be empty")
        if Path(rel_path).is_absolute():
            raise PermissionError(f"Absolute paths not allowed: {rel_path}")
        candidate = (self.root / rel_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Path escapes vault root: {rel_path}")
        if not candidate.suffix:
            candidate = candidate.with_suffix(".md")
        return candidate

    def _resolve_folder(self, folder: str) -> Path:
        """Resolve a folder path safely (no traversal escapes)."""
        folder = folder.strip()
        if not folder:
            return self.root
        if Path(folder).is_absolute():
            raise PermissionError(f"Absolute folder paths not allowed: {folder}")
        candidate = (self.root / folder).resolve()
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
            if not self._is_in_trash(p, self.root)
        ]

    # ---- full API ----------------------------------------------------

    def read(self, rel_path: str) -> Note:
        p = self._resolve(rel_path)
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
            try:
                results.append(load_note(p, self.root))
            except Exception:
                continue
        return results

    def search(self, query: str) -> list[Note]:
        q = query.lower()
        return [
            n for n in self.list_notes()
            if q in n.title.lower() or q in n.content.lower()
        ]

    def create(self, rel_path: str, content: str, tags: list[str] | None = None) -> Note:
        policies.assert_no_foreign_lock(self.root)
        p = self._resolve(rel_path)
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
        _require_wiki_uid(rel_path, dict(post.metadata))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        note = load_note(p, self.root)
        self._after_write(rel_path, "create", note)
        return note

    def update(self, rel_path: str, content: str) -> Note:
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        post = frontmatter.load(p)
        post.content = content
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        note = load_note(p, self.root)
        self._after_write(rel_path, "modify", note)
        return note

    def delete(self, rel_path: str) -> str:
        """Permanently delete a note. Returns the vault-relative path."""
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        p.unlink()
        self._after_write(rel_path, "delete", None)
        return str(p.relative_to(self.root))

    def append(self, rel_path: str, content: str) -> tuple[Note, bool]:
        """Append content to an existing note, or create it if missing."""
        policies.assert_no_foreign_lock(self.root)
        p = self._resolve(rel_path)
        created = False
        if p.exists():
            if not p.is_file():
                raise ValueError(f"Not a file: {rel_path}")
            post = frontmatter.load(p)
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
            _require_wiki_uid(rel_path, dict(post.metadata))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            created = True
        note = load_note(p, self.root)
        self._after_write(rel_path, "create" if created else "modify", note)
        return note, created

    # ---- frontmatter management --------------------------------------

    def get_frontmatter(self, rel_path: str, key: str):
        """Return the value of a frontmatter key, or None if not set."""
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        post = frontmatter.load(p)
        return _serialize_value(post.metadata.get(key))

    def set_frontmatter(self, rel_path: str, key: str, value) -> None:
        """Set a frontmatter key while preserving body and other metadata."""
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        post = frontmatter.load(p)
        post[key] = value
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        self._after_write(rel_path, "modify", self.read(rel_path))

    def delete_frontmatter(self, rel_path: str, key: str) -> None:
        """Delete a frontmatter key if present."""
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        post = frontmatter.load(p)
        if key in post.metadata:
            del post.metadata[key]
            p.write_text(frontmatter.dumps(post), encoding="utf-8")
            self._after_write(rel_path, "modify", self.read(rel_path))

    # ---- tag management ----------------------------------------------

    def add_tags(self, rel_path: str, tags: list[str]) -> list[str]:
        """Add tags to the frontmatter tags array (deduplicated, lowercase)."""
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        post = frontmatter.load(p)
        current = self._normalize_tags(post.metadata.get("tags", []))
        for t in tags:
            tl = t.lower()
            if tl not in current:
                current.append(tl)
        post.metadata["tags"] = current
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        self._after_write(rel_path, "modify", self.read(rel_path))
        return current

    def remove_tags(self, rel_path: str, tags: list[str]) -> list[str]:
        """Remove tags from the frontmatter tags array case-insensitively."""
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        post = frontmatter.load(p)
        current = self._normalize_tags(post.metadata.get("tags", []))
        remove_set = {t.lower() for t in tags}
        current = [t for t in current if t not in remove_set]
        post.metadata["tags"] = current
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        self._after_write(rel_path, "modify", self.read(rel_path))
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
        result["log"] = policies.log_write(self.root, rel_path, action, layer)
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
        """Surgically edit the content under a heading."""
        if action not in {"append", "prepend", "replace"}:
            raise ValueError(f"Invalid patch action: {action}")
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")

        post = frontmatter.load(p)
        lines = post.content.splitlines()
        heading_re = re.compile(r"^(#{1,6})\s+" + re.escape(heading) + r"\s*$", re.IGNORECASE)
        start_idx = None
        level = None
        for i, line in enumerate(lines):
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
        self._after_write(rel_path, "modify", note)
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
        return self._resolve(f"{daily_dir}/{date}.md")

    # ---- search and replace ------------------------------------------

    def search_and_replace(
        self,
        rel_path: str,
        find: str,
        replace: str,
        use_regex: bool = False,
        case_sensitive: bool = True,
    ) -> tuple[str, int]:
        """Replace occurrences of `find` in the note body."""
        if not find:
            raise ValueError("find must not be empty")
        policies.assert_no_foreign_lock(self.root)
        policies.assert_mutable(rel_path)
        p = self._resolve(rel_path)
        if not p.exists():
            raise FileNotFoundError(f"Note not found: {rel_path}")
        if not p.is_file():
            raise ValueError(f"Not a file: {rel_path}")
        post = frontmatter.load(p)
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = find if use_regex else re.escape(find)
        new_content, count = re.subn(pattern, replace, post.content, flags=flags)
        post.content = new_content
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        self._after_write(rel_path, "modify", None)
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
        p = self._resolve(rel_path)
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
