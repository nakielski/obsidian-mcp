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
- Did-you-mean: corrected(query) via Whoosh spell checker on the body field

Controlled by VAULT_INDEX (default on).
"""
from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

try:
    from whoosh import fields as w_fields
    from whoosh import index as w_index
    from whoosh.analysis import StemmingAnalyzer
    from whoosh.qparser import FuzzyTermPlugin, MultifieldParser

    WHOOSH_AVAILABLE = True
except ImportError:  # pragma: no cover — whoosh is a hard dependency
    WHOOSH_AVAILABLE = False


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def default_index_dir(vault_root: Path) -> Path:
    """Resolve the index directory: OBSIDIAN_INDEX_DIR env or vault default."""
    env = os.environ.get("OBSIDIAN_INDEX_DIR", "").strip()
    return Path(env).expanduser() if env else vault_root / ".obsidian-mcp-index"


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
        folder = str(Path(path).parent) if path and str(Path(path).parent) != "." else ""
        mtime = meta.get("updated") or meta.get("datum") or meta.get("date")
        if isinstance(mtime, str):
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", mtime)
            if m:
                try:
                    mtime = datetime.datetime(*map(int, m.groups()))
                except ValueError:
                    mtime = None
            else:
                mtime = None
        if not isinstance(mtime, datetime.datetime):
            mtime = datetime.datetime.now()
        return {
            "path": path,
            "title": getattr(note, "title", "") or "",
            "body": getattr(note, "content", "") or "",
            "tags": ",".join(getattr(note, "tags", []) or []),
            "folder": folder,
            "typ": str(meta.get("typ", meta.get("type", "")) or "").lower(),
            "status": str(meta.get("status", "") or "").lower(),
            "uid": str(meta.get("uid", "") or ""),
            "mtime": mtime,
        }

    def index_note(self, note) -> None:
        """Add or replace a note in the index."""
        writer = self._ix.writer(timeout=5.0)
        writer.update_document(**self._note_fields(note))
        writer.commit()

    def remove_note(self, rel_path: str) -> None:
        """Remove a note from the index by path."""
        writer = self._ix.writer(timeout=5.0)
        writer.delete_by_term("path", rel_path)
        writer.commit()

    def rebuild(self, notes) -> dict:
        """Reindex all given notes from scratch. Returns stats."""
        self._ix = w_index.create_in(self.index_dir, build_schema())
        writer = self._ix.writer(timeout=30.0)
        for n in notes:
            writer.add_document(**self._note_fields(n))
        writer.commit()
        return {"indexed": len(notes), "index_dir": str(self.index_dir)}

    def count(self) -> int:
        return self._ix.doc_count()

    # ---- read path -------------------------------------------------------

    def search(self, query: str, limit: int = 20, default_fields=None) -> list[dict]:
        """Run a query string. Plain terms search title+body; field queries,
        booleans, phrases, wildcards, fuzzy and ranges per Whoosh syntax."""
        fields = default_fields or ["title", "body"]
        parser = MultifieldParser(fields, schema=self._ix.schema)
        parser.add_plugin(FuzzyTermPlugin())
        parsed = parser.parse(query)
        out: list[dict] = []
        with self._ix.searcher() as s:
            for hit in s.search(parsed, limit=limit):
                out.append({
                    "path": hit["path"],
                    "title": hit.get("title", ""),
                    "score": round(hit.score, 3),
                    "tags": (hit.get("tags") or "").split(",") if hit.get("tags") else [],
                    "folder": hit.get("folder", ""),
                    "typ": hit.get("typ", ""),
                    "status": hit.get("status", ""),
                    "uid": hit.get("uid", ""),
                    "snippet": s.highlight(hit, "body") if hasattr(s, "highlight") else "",
                })
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
