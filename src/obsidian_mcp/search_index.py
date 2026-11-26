"""Full-text index over the vault backed by Whoosh (whoosh3 package).

Provides tokenized, ranked search with a Lucene-style query syntax and
incremental updates driven by the Vault write choke point.

- Index location: env OBSIDIAN_INDEX_DIR, default <vault>/.obsidian-mcp-index/
  (a dotfile directory that Syncthing/Obsidian ignore; set the env to move it
  outside the vault, e.g. ~/.cache/obsidian-mcp-index)
- Incremental: create/update/delete/patch funnel through index_note /
  remove_note; rebuild() reindexes everything
- Query syntax: field queries (title:, tags:, folder:, typ:, status:),
  boolean AND/OR/NOT, phrases \"…\", wildcards *, fuzzy term~N, ranges
  mtime:[2026-01-01 to 2026-12-31]
- Note date (field ``mtime``): frontmatter updated/datum/date/created, else
  the newest date in the file name, else the file's modification time. See
  ``note_date``. Hits carry it as ``date`` with ``date_source``.
- Hidden by default: notes with a status from OBSIDIAN_SEARCH_HIDE_STATUS
  (default: superseded) and Syncthing conflict copies
  (OBSIDIAN_SEARCH_HIDE_CONFLICTS, default on)
- Did-you-mean: corrected(query) via Whoosh spell checker on the body field

Controlled by VAULT_INDEX (default on).
"""
from __future__ import annotations

import datetime
import os
import re
import time
from pathlib import Path, PurePosixPath

try:
    from whoosh import fields as w_fields
    from whoosh import index as w_index
    from whoosh.analysis import StemmingAnalyzer
    from whoosh.qparser import AndGroup, FuzzyTermPlugin, MultifieldParser, OrGroup

    WHOOSH_AVAILABLE = True
except ImportError:  # pragma: no cover — whoosh is a hard dependency
    WHOOSH_AVAILABLE = False


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def default_index_dir(vault_root: Path) -> Path:
    """Resolve the index directory: OBSIDIAN_INDEX_DIR env or vault default."""
    env = os.environ.get("OBSIDIAN_INDEX_DIR", "").strip()
    return Path(env).expanduser() if env else vault_root / ".obsidian-mcp-index"


_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DE_DATE_RE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)")
_CONFLICT_RE = re.compile(r"\.sync-conflict-", re.IGNORECASE)
DATE_KEYS = ("updated", "datum", "date", "created")
EPOCH = datetime.datetime(1970, 1, 1)
SORTS = ("relevance", "newest")


def _as_datetime(value) -> datetime.datetime | None:
    """Frontmatter value -> naive datetime. YAML turns an unquoted
    ``2026-11-03`` into a ``datetime.date``, a quoted one stays a string."""
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        m = _ISO_DATE_RE.search(value[:32])
        if m:
            try:
                return datetime.datetime(*map(int, m.groups()))
            except ValueError:
                return None
    return None


def _dates_in_name(name: str) -> list[datetime.datetime]:
    found = [tuple(map(int, m.groups())) for m in _ISO_DATE_RE.finditer(name)]
    found += [tuple(map(int, reversed(m.groups()))) for m in _DE_DATE_RE.finditer(name)]
    dates = []
    for y, mo, d in found:
        try:
            dates.append(datetime.datetime(y, mo, d))
        except ValueError:
            continue
    return dates


def _file_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def note_date(meta: dict, path: str, file_mtime: float | None = None,
              now: datetime.datetime | None = None) -> tuple[datetime.datetime, str]:
    """The date a note counts as last changed, and where it came from.

    1. ``frontmatter``: updated, datum, date, created (first one that parses).
    2. ``filename``: the newest date in the file name (2026-11-03 or 03.11.2026).
    3. ``file``: the modification time of the file.
    4. ``none``: nothing usable; the note sorts last.

    Dates more than a day in the future are ignored (typos). The time of
    indexing is never used: until 2026-11 every note whose date was not a
    quoted string got "now" and looked freshly changed in every date range.
    """
    limit = (now or datetime.datetime.now()) + datetime.timedelta(days=1)
    for key in DATE_KEYS:
        dt = _as_datetime((meta or {}).get(key))
        if dt is not None and dt <= limit:
            return dt, "frontmatter"
    named = [d for d in _dates_in_name(_CONFLICT_RE.split(_file_name(path))[0]) if d <= limit]
    if named:
        return max(named), "filename"
    if file_mtime:
        try:
            return datetime.datetime.fromtimestamp(file_mtime).replace(microsecond=0), "file"
        except (OverflowError, OSError, ValueError):
            pass
    return EPOCH, "none"


def is_conflict_copy(path: str) -> bool:
    """True for a Syncthing conflict copy ('note.sync-conflict-20260909-224424-ABC.md')."""
    return bool(_CONFLICT_RE.search(_file_name(path)))


def hidden_statuses() -> tuple[str, ...]:
    """Statuses the searches leave out unless asked (OBSIDIAN_SEARCH_HIDE_STATUS,
    comma separated, default 'superseded'; set but empty hides none)."""
    raw = os.environ.get("OBSIDIAN_SEARCH_HIDE_STATUS")
    if raw is None:
        return ("superseded",)
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


def hide_conflicts() -> bool:
    return _flag("OBSIDIAN_SEARCH_HIDE_CONFLICTS")


def is_hidden(path: str, status: str, statuses: tuple[str, ...] | None = None,
              conflicts: bool | None = None) -> bool:
    """A hit the searches leave out by default: replaced note or conflict copy."""
    statuses = hidden_statuses() if statuses is None else statuses
    conflicts = hide_conflicts() if conflicts is None else conflicts
    return (str(status or "").lower() in statuses) or bool(conflicts and is_conflict_copy(path))


def parse_since(value: str) -> datetime.datetime | None:
    """'2026-11-01' -> datetime; '' -> None; anything else raises ValueError."""
    value = (value or "").strip()
    if not value:
        return None
    m = _ISO_DATE_RE.fullmatch(value)
    if not m:
        raise ValueError(f"since must be a date like 2026-11-01, got {value!r}")
    return datetime.datetime(*map(int, m.groups()))


def _iso(dt) -> str:
    return dt.date().isoformat() if isinstance(dt, datetime.datetime) and dt > EPOCH else ""


def build_schema() -> "w_fields.Schema":
    stem = StemmingAnalyzer()
    return w_fields.Schema(
        path=w_fields.ID(stored=True, unique=True),
        title=w_fields.TEXT(stored=True, analyzer=stem, field_boost=3.0),
        body=w_fields.TEXT(analyzer=stem),
        tags=w_fields.KEYWORD(commas=True, lowercase=True, stored=True),
        folder=w_fields.ID(stored=True),
        typ=w_fields.KEYWORD(lowercase=True, stored=True),
        status=w_fields.KEYWORD(lowercase=True, stored=True),
        uid=w_fields.ID(stored=True),
        mtime=w_fields.DATETIME(stored=True, sortable=True),
        date_src=w_fields.STORED(),  # where mtime came from: frontmatter | filename | file | none
        fsig=w_fields.STORED(),  # file size + mtime: detects out-of-band changes
    )


class VaultIndex:
    """Incremental Whoosh index over vault notes."""

    def __init__(self, vault_root: Path, index_dir: Path | None = None):
        if not WHOOSH_AVAILABLE:
            raise ImportError(
                "whoosh3 is not installed. Run: pip install whoosh3 "
                "(or disable indexing with VAULT_INDEX=0)"
            )
        self.vault_root = Path(vault_root)
        self.index_dir = Path(index_dir) if index_dir else default_index_dir(self.vault_root)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._ix = w_index.create_in(self.index_dir, build_schema()) \
            if not w_index.exists_in(self.index_dir) \
            else w_index.open_dir(self.index_dir)

    # ---- write path ------------------------------------------------------

    def _note_fields(self, note) -> dict:
        meta = getattr(note, "metadata", {}) or {}
        path = getattr(note, "path", "")
        parent = PurePosixPath(path.replace("\\", "/")).parent.as_posix() if path else "."
        folder = parent if parent != "." else ""
        try:
            file_mtime = (self.vault_root / path).stat().st_mtime if path else None
        except OSError:
            file_mtime = None
        mtime, date_src = note_date(meta, path, file_mtime)
        fields = {
            "path": path,
            "title": getattr(note, "title", "") or "",
            "body": getattr(note, "content", "") or "",
            "tags": ",".join(getattr(note, "tags", []) or []),
            "folder": folder,
            "typ": str(meta.get("typ", meta.get("type", "")) or "").lower(),
            "status": str(meta.get("status", "") or "").lower(),
            "uid": str(meta.get("uid", "") or ""),
            "mtime": mtime,
            "date_src": date_src,
            "fsig": self._file_sig(path),
        }
        # An index from before a schema change stays usable until reconcile rebuilds it.
        known = set(self._ix.schema.names())
        return {k: v for k, v in fields.items() if k in known}

    def outdated(self) -> bool:
        """True while the index on disk predates the current schema."""
        return not {"fsig", "date_src"} <= set(self._ix.schema.names())

    def _file_sig(self, rel_path: str) -> str:
        try:
            st = (self.vault_root / rel_path).stat()
        except OSError:
            return ""
        return f"{st.st_size}:{st.st_mtime_ns}"

    def index_note(self, note) -> None:
        """Add or replace a note in the index."""
        writer = self._ix.writer(timeout=5.0)
        writer.update_document(**self._note_fields(note))
        writer.commit()

    def remove_note(self, rel_path: str) -> None:
        """Remove a note from the index by path."""
        writer = self._ix.writer(timeout=5.0)
        writer.delete_by_term("path", rel_path.replace("\\", "/"))
        writer.commit()

    def rebuild(self, notes) -> dict:
        """Reindex all given notes from scratch. Returns stats."""
        self._ix = w_index.create_in(self.index_dir, build_schema())
        writer = self._ix.writer(timeout=30.0)
        for n in notes:
            writer.add_document(**self._note_fields(n))
        writer.commit()
        return {"indexed": len(notes), "index_dir": str(self.index_dir)}

    def reconcile(self, notes) -> dict:
        """Bring the index in line with the files on disk.

        Writes through the server update the index directly. Changes that
        bypass it (Syncthing from another device, the Obsidian editor, a
        second MCP server with its own index, scripts) leave stale entries:
        moved notes stay findable under their old path and are missing under
        the new one. This compares path and file signature of every note and
        only touches what differs. An index from before the signature field
        or the date source field is rebuilt once (its dates were wrong).
        """
        if self.outdated():
            stats = self.rebuild(notes)
            self._touch_stamp()
            return {"rebuilt": True, "added": stats["indexed"], "updated": 0, "removed": 0}
        current = {n.path: n for n in notes}
        with self._ix.searcher() as s:
            stored = {f["path"]: f.get("fsig", "") for f in s.all_stored_fields()}
        removed = [p for p in stored if p not in current]
        added = [p for p in current if p not in stored]
        updated = [p for p in current if p in stored and stored[p] != self._file_sig(p)]
        if removed or added or updated:
            writer = self._ix.writer(timeout=30.0)
            for p in removed:
                writer.delete_by_term("path", p)
            for p in added + updated:
                writer.update_document(**self._note_fields(current[p]))
            writer.commit()
        self._touch_stamp()
        return {"rebuilt": False, "added": len(added), "updated": len(updated), "removed": len(removed)}

    STAMP = "reconcile.stamp"

    def _touch_stamp(self) -> None:
        try:
            (self.index_dir / self.STAMP).write_text(str(time.time()), encoding="ascii")
        except OSError:
            pass

    def reconciled_within(self, seconds: float) -> bool:
        """True if any process sharing this index reconciled it in the last ``seconds``."""
        if self.outdated():
            return False
        try:
            return time.time() - (self.index_dir / self.STAMP).stat().st_mtime < seconds
        except OSError:
            return False

    def count(self) -> int:
        return self._ix.doc_count()

    # ---- read path -------------------------------------------------------

    def search(self, query: str, limit: int = 20, default_fields=None, *,
               any_term: bool = False, folder: str = "", exclude_prefixes=(),
               sort: str = "relevance", since: datetime.datetime | None = None,
               hide_status: tuple[str, ...] = (), hide_conflict_copies: bool = False,
               stats: dict | None = None) -> list[dict]:
        """Run a query string. Plain terms search title+body; field queries,
        booleans, phrases, wildcards, fuzzy and ranges per Whoosh syntax.

        any_term: combine plain terms with OR (ranked by how many match)
            instead of AND - suited to natural-language questions.
        folder / exclude_prefixes: casefolded folder prefixes ('a/b/') the
            hit path must / must not start with.
        sort: 'relevance' (default) or 'newest' (note date, newest first).
        since: only notes dated on or after this day.
        hide_status / hide_conflict_copies: leave out notes with one of these
            statuses and Syncthing conflict copies; ``stats['hidden']`` counts
            the matching notes left out for that reason.
        """
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}, got {sort!r}")
        fields = default_fields or ["title", "body"]
        group = OrGroup.factory(0.9) if any_term else AndGroup
        parser = MultifieldParser(fields, schema=self._ix.schema, group=group)
        parser.add_plugin(FuzzyTermPlugin())
        parsed = parser.parse(query)
        folder_cf = folder.casefold()
        excluded = tuple(p.casefold() for p in exclude_prefixes)
        hide_status = tuple(x.lower() for x in hide_status)
        hiding = bool(hide_status or hide_conflict_copies)
        filtered = bool(folder_cf or excluded or hiding or since)

        def allowed(path: str) -> bool:
            p = path.casefold()
            return (not folder_cf or p.startswith(folder_cf)) and not any(p.startswith(x) for x in excluded)

        newest = sort == "newest" and "mtime" in self._ix.schema.names()
        kwargs = {"sortedby": "mtime", "reverse": True} if newest else {}
        hidden = 0
        out: list[dict] = []
        with self._ix.searcher() as s:
            # Over-fetch when filtering so excluded hits cannot crowd out allowed ones.
            # Over-fetch as well, because hits whose file is gone (moved or deleted
            # outside the server, not reconciled yet) are skipped.
            for hit in s.search(parsed, limit=None if filtered else limit * 2 + 10, **kwargs):
                if filtered and not allowed(hit["path"]):
                    continue
                if not (self.vault_root / hit["path"]).is_file():
                    continue
                dated = hit.get("mtime")
                if since is not None and (not isinstance(dated, datetime.datetime) or dated < since):
                    continue
                if hiding and is_hidden(hit["path"], hit.get("status", ""), hide_status,
                                        hide_conflict_copies):
                    hidden += 1
                    continue
                if len(out) >= limit:
                    if not hiding:
                        break
                    continue  # keep counting what is hidden
                score = hit.score if isinstance(hit.score, (int, float)) and not newest else 0.0
                out.append({
                    "path": hit["path"],
                    "title": hit.get("title", ""),
                    "score": round(score, 3),
                    "tags": (hit.get("tags") or "").split(",") if hit.get("tags") else [],
                    "folder": hit.get("folder", ""),
                    "typ": hit.get("typ", ""),
                    "status": hit.get("status", ""),
                    "uid": hit.get("uid", ""),
                    "date": _iso(dated),
                    "date_source": hit.get("date_src", "") or "",
                    "snippet": s.highlight(hit, "body") if hasattr(s, "highlight") else "",
                })
        if stats is not None:
            stats["hidden"] = hidden
        return out

    def meta(self, paths) -> dict[str, dict]:
        """Stored fields for the given paths: {path: {status, typ, uid, title, date, date_source}}.

        For hits that come from another retriever (semantic vectors) and
        carry no frontmatter fields. Unknown paths are left out.
        """
        out: dict[str, dict] = {}
        with self._ix.searcher() as s:
            for p in dict.fromkeys(paths):
                doc = s.document(path=p)
                if doc is None:
                    continue
                out[p] = {
                    "status": doc.get("status", "") or "",
                    "typ": doc.get("typ", "") or "",
                    "uid": doc.get("uid", "") or "",
                    "title": doc.get("title", "") or "",
                    "date": _iso(doc.get("mtime")),
                    "date_source": doc.get("date_src", "") or "",
                }
        return out

    def corrected(self, query: str) -> str:
        """Return a corrected query string ('did you mean') or ''."""
        try:
            parser = MultifieldParser(["title", "body"], schema=self._ix.schema)
            parsed = parser.parse(query)
            with self._ix.searcher() as s:
                corrected = s.correct_query(parsed, query)
            if corrected and corrected.query != parsed and corrected.string:
                return corrected.string
            return ""
        except Exception:
            return ""
