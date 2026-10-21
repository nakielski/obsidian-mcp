"""Optional semantic index: note chunks as vectors in Qdrant.

The semantic index is the second retrieval path behind ``hybrid_search``. It is
OFF by default and needs the ``semantic`` extra (``pip install
'obsidian-mcp[semantic]'``). Without it the server runs exactly as before:
no extra tools, no extra dependencies, no network calls.

Design:

- **Same choke point as the Whoosh index.** Every write that goes through the
  Vault (create/update/append/patch/frontmatter/tags/replace/delete/archive)
  updates the vectors of that note in the same call. Out-of-band changes are
  caught up by ``sync`` (tool ``semantic_sync`` or CLI ``obsidian-mcp-semantic``).
- **Content-addressed chunks.** A note is split at its H1-H3 headings (fence
  aware); each chunk's point id is ``uuid5(path + hash(embedded text))``. An
  edit re-embeds only the chunks whose text changed, removes vanished chunks
  and keeps the rest untouched. A move (archive) reuses the stored vectors.
- **Policy in the index, not after it.** Every point carries the casefolded
  folder prefixes of its note. ``search`` filters inside Qdrant: restricted
  folders (``OBSIDIAN_SEARCH_RESTRICTED``) are excluded unless the caller asks
  for them explicitly; excluded folders (``OBSIDIAN_SEMANTIC_EXCLUDE``) are never
  embedded at all.
- **Fail-open for writes.** Embedding or Qdrant errors never fail a completed
  write. The note is recorded as pending (``semantic-pending.json`` in the index
  directory, machine-local like the Whoosh index) and a short back-off avoids
  stalling every following write while the backend is down.
- **One collection per embedding model.** The default collection name contains
  the model slug, so switching models never mixes vector spaces.

Embeddings come from Ollama (``/api/embed``) or any OpenAI-compatible endpoint
(``/embeddings``), over httpx, which the mcp package already depends on.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import policies
from .vault import _fence_flags

try:  # optional extra
    from qdrant_client import QdrantClient
    from qdrant_client import models as qm

    QDRANT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in the no-extra CI job
    QdrantClient = None
    qm = None
    QDRANT_AVAILABLE = False

INSTALL_HINT = "pip install 'obsidian-mcp[semantic]'"
PENDING_FILE = "semantic-pending.json"
# Fixed namespace so point ids are stable across processes and machines.
_POINT_NS = uuid.UUID("8f3c2a5e-6b1d-4f7a-9c0e-2d4b6a8e1f37")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_MAX_CHUNKS_PER_NOTE = 400


def semantic_enabled() -> bool:
    """OBSIDIAN_SEMANTIC=1 turns the semantic index on (default off)."""
    return policies._flag("OBSIDIAN_SEMANTIC", "0")


# ---- configuration -------------------------------------------------------


def normalize_prefix(value: str) -> str:
    """'05 People' / '/05 People/' / '05 People\\' -> '05 people/' (casefolded)."""
    p = value.strip().replace("\\", "/").strip("/")
    return f"{p.casefold()}/" if p else ""


def _prefix_list(env: str) -> tuple[str, ...]:
    raw = os.environ.get(env, "")
    return tuple(p for p in (normalize_prefix(x) for x in raw.split(",")) if p)


def _model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model.casefold()).strip("_") or "default"


@dataclass(frozen=True)
class SemanticConfig:
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_timeout: float = 10.0
    collection: str = "obsidian_mcp_bge_m3"
    embed_api: str = "ollama"  # "ollama" | "openai"
    embed_url: str = "http://localhost:11434"
    embed_model: str = "bge-m3"
    embed_api_key: str | None = None
    embed_timeout: float = 20.0
    embed_batch: int = 8
    query_prefix: str = ""
    doc_prefix: str = ""
    exclude: tuple[str, ...] = ()
    restricted: tuple[str, ...] = ()
    secondary: tuple[str, ...] = ("06 archive", "raw")
    demote: tuple[str, ...] = ("**/node_modules/",)
    demote_weight: float = 0.5
    candidates: int = 30
    max_chunk_chars: int = 2000
    backoff_seconds: float = 60.0
    min_score: float = 0.0
    inline_max_chunks: int = 32

    @classmethod
    def from_env(cls) -> SemanticConfig:
        env = os.environ.get
        model = env("OBSIDIAN_EMBED_MODEL", "").strip() or "bge-m3"
        api = (env("OBSIDIAN_EMBED_API", "").strip() or "ollama").lower()
        if api not in {"ollama", "openai"}:
            raise ValueError(f"OBSIDIAN_EMBED_API must be 'ollama' or 'openai', got {api!r}")
        default_url = "http://localhost:11434" if api == "ollama" else "http://localhost:11434/v1"
        return cls(
            qdrant_url=env("OBSIDIAN_QDRANT_URL", "").strip() or "http://localhost:6333",
            qdrant_api_key=env("OBSIDIAN_QDRANT_API_KEY") or None,
            qdrant_timeout=float(env("OBSIDIAN_QDRANT_TIMEOUT", "10")),
            collection=env("OBSIDIAN_QDRANT_COLLECTION", "").strip() or f"obsidian_mcp_{_model_slug(model)}",
            embed_api=api,
            embed_url=(env("OBSIDIAN_EMBED_URL", "").strip() or default_url).rstrip("/"),
            embed_model=model,
            embed_api_key=env("OBSIDIAN_EMBED_API_KEY") or None,
            embed_timeout=float(env("OBSIDIAN_EMBED_TIMEOUT", "20")),
            embed_batch=max(1, int(env("OBSIDIAN_EMBED_BATCH", "8"))),
            query_prefix=env("OBSIDIAN_EMBED_QUERY_PREFIX", ""),
            doc_prefix=env("OBSIDIAN_EMBED_DOC_PREFIX", ""),
            exclude=_prefix_list("OBSIDIAN_SEMANTIC_EXCLUDE"),
            restricted=_prefix_list("OBSIDIAN_SEARCH_RESTRICTED"),
            secondary=(_prefix_list("OBSIDIAN_SEARCH_SECONDARY")
                       if "OBSIDIAN_SEARCH_SECONDARY" in os.environ else ("06 archive", "raw")),
            demote=(tuple(x.strip() for x in env("OBSIDIAN_SEARCH_DEMOTE", "").split(",") if x.strip())
                    if "OBSIDIAN_SEARCH_DEMOTE" in os.environ else ("**/node_modules/",)),
            demote_weight=min(max(float(env("OBSIDIAN_SEARCH_DEMOTE_WEIGHT", "0.5")), 0.0), 1.0),
            candidates=max(10, int(env("OBSIDIAN_HYBRID_CANDIDATES", "30"))),
            max_chunk_chars=max(200, int(env("OBSIDIAN_SEMANTIC_CHUNK_CHARS", "2000"))),
            backoff_seconds=float(env("OBSIDIAN_SEMANTIC_BACKOFF", "60")),
            min_score=float(env("OBSIDIAN_SEMANTIC_MIN_SCORE", "0")),
            inline_max_chunks=max(1, int(env("OBSIDIAN_SEMANTIC_INLINE_MAX", "32"))),
        )


def folder_prefixes(rel_path: str) -> list[str]:
    """All casefolded ancestor folders of a note: 'A/B/x.md' -> ['a/', 'a/b/']."""
    parts = rel_path.replace("\\", "/").strip("/").casefold().split("/")[:-1]
    return ["/".join(parts[: i + 1]) + "/" for i in range(len(parts))]


def is_under(rel_path: str, prefixes) -> bool:
    p = rel_path.replace("\\", "/").lstrip("/").casefold()
    return any(p.startswith(x) for x in prefixes)


# ---- chunking ------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    heading: str  # heading path, e.g. 'Bauplan > Tests' ('' for the preamble)
    text: str  # section text without the heading line
    embed_text: str  # what gets embedded (title + heading + text)
    hash: str


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split at blank lines into pieces <= max_chars; hard-cut oversize paragraphs."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    cur = ""
    for para in paras:
        while len(para) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:max_chars])
            para = para[max_chars:].lstrip()
        if not para:
            continue
        if cur and len(cur) + 2 + len(para) > max_chars:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def chunk_note(title: str, content: str, max_chars: int = 2000, doc_prefix: str = "",
               min_chars: int = 80) -> list[Chunk]:
    """Split a note body into heading sections (H1-H3, '#' inside code fences ignored).

    Long sections are split at paragraph boundaries; very short sections are
    merged into the previous chunk so status lines do not become chunks of
    their own. Title and heading path are part of the embedded text.
    """
    lines = content.splitlines()
    fences = _fence_flags(lines)
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = []
    heading = ""
    for line, in_fence in zip(lines, fences):
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            sections.append((heading, current))
            level = len(m.group(1))
            stack = [s for s in stack if s[0] < level] + [(level, m.group(2).strip())]
            heading = " > ".join(s[1] for s in stack)
            current = []
        else:
            current.append(line)
    sections.append((heading, current))

    pieces: list[tuple[str, str]] = []
    for head, body in sections:
        text = "\n".join(body).strip()
        if not text:
            continue
        for part in _split_long(text, max_chars):
            if pieces and len(part) < min_chars and len(pieces[-1][1]) + len(part) + 2 <= max_chars:
                prev_head, prev_text = pieces[-1]
                label = f"{head}: " if head and head != prev_head else ""
                pieces[-1] = (prev_head, f"{prev_text}\n\n{label}{part}")
            else:
                pieces.append((head, part))

    chunks: list[Chunk] = []
    for head, text in pieces[:_MAX_CHUNKS_PER_NOTE]:
        header = "\n".join(x for x in (title, head) if x)
        embed_text = f"{doc_prefix}{header}\n\n{text}" if header else f"{doc_prefix}{text}"
        digest = hashlib.sha256(embed_text.encode("utf-8")).hexdigest()
        chunks.append(Chunk(heading=head, text=text, embed_text=embed_text, hash=digest))
    return chunks


def point_id(rel_path: str, chunk_hash: str) -> str:
    return str(uuid.uuid5(_POINT_NS, f"{rel_path}\x00{chunk_hash}"))


def note_meta(note) -> dict:
    """Note-level payload fields; a change re-labels points without re-embedding."""
    meta = getattr(note, "metadata", {}) or {}
    return {
        "title": getattr(note, "title", "") or "",
        "uid": str(meta.get("uid", "") or ""),
        "tags": list(getattr(note, "tags", []) or []),
        "typ": str(meta.get("typ", meta.get("type", "")) or "").lower(),
        "status": str(meta.get("status", "") or "").lower(),
    }


def _meta_hash(meta: dict) -> str:
    return hashlib.sha256(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---- embeddings ----------------------------------------------------------


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    """Batch embeddings from Ollama (/api/embed) or an OpenAI-compatible API."""

    def __init__(self, cfg: SemanticConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        headers = {"Authorization": f"Bearer {cfg.embed_api_key}"} if cfg.embed_api_key else {}
        self._http = httpx.Client(timeout=cfg.embed_timeout, headers=headers, transport=transport)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.cfg.embed_batch):
            out.extend(self._embed_batch(texts[i : i + self.cfg.embed_batch]))
        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """One request; on a read timeout split the batch and retry the halves.

        Ollama embeds the inputs of one request one after another, so on a busy
        CPU a batch of long sections can outlast the timeout although every
        single input is fast enough. Splitting keeps large notes indexable
        without raising the timeout for every request. Connection failures
        are not split: the backend is down and retrying halves only multiplies
        the wait.
        """
        try:
            return self._request(batch)
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            if len(batch) == 1:
                raise EmbeddingError(
                    f"embedding request to {self.cfg.embed_url} timed out after {self.cfg.embed_timeout:g}s "
                    f"for a single input ({len(batch[0])} chars); raise OBSIDIAN_EMBED_TIMEOUT"
                ) from e
            mid = len(batch) // 2
            return self._embed_batch(batch[:mid]) + self._embed_batch(batch[mid:])

    def _request(self, batch: list[str]) -> list[list[float]]:
        try:
            if self.cfg.embed_api == "ollama":
                r = self._http.post(f"{self.cfg.embed_url}/api/embed",
                                    json={"model": self.cfg.embed_model, "input": batch})
                r.raise_for_status()
                vecs = r.json().get("embeddings") or []
            else:
                r = self._http.post(f"{self.cfg.embed_url}/embeddings",
                                    json={"model": self.cfg.embed_model, "input": batch})
                r.raise_for_status()
                data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
                vecs = [d.get("embedding") for d in data]
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            raise  # handled by _embed_batch
        except httpx.HTTPError as e:
            raise EmbeddingError(f"embedding request to {self.cfg.embed_url} failed: {e}") from e
        except ValueError as e:
            raise EmbeddingError(f"embedding endpoint returned invalid JSON: {e}") from e
        if len(vecs) != len(batch) or not all(isinstance(v, list) and v for v in vecs):
            raise EmbeddingError(
                f"embedding endpoint returned {len(vecs)} vectors for {len(batch)} inputs "
                f"(model {self.cfg.embed_model!r} pulled?)"
            )
        return vecs


# ---- pending store -------------------------------------------------------


class PendingStore:
    """Notes whose vectors could not be updated; drained by sync.

    Machine-local JSON file next to the Whoosh index, updated under an
    exclusive lock because every MCP client runs its own server process.
    """

    def __init__(self, path: Path):
        self.path = path

    def all(self) -> list[str]:
        try:
            return list(json.loads(self.path.read_text(encoding="utf-8")).get("paths", []))
        except (OSError, ValueError):
            return []

    def _update(self, fn) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a+") as fh:
            policies._lock_exclusive(fh)
            try:
                paths = set(self.all())
                new = fn(paths)
                if new == paths:
                    return
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps({"paths": sorted(new)}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            finally:
                policies._unlock(fh)

    def add(self, rel_path: str) -> None:
        self._update(lambda s: s | {rel_path})

    def discard(self, *rel_paths: str) -> None:
        if not rel_paths or not self.path.exists():
            return
        current = set(self.all())
        if current.isdisjoint(rel_paths):
            return  # common case: no lock, no write
        self._update(lambda s: s - set(rel_paths))


# ---- the index -------------------------------------------------------------


def _is_internal_url(url: str) -> bool:
    """Loopback, private, CGNAT/Tailscale (100.64.0.0/10) or link-local host."""
    host = httpx.URL(url).host
    if host in {"localhost", ""}:
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return host.endswith(".ts.net")


def _make_client(cfg: SemanticConfig):
    """QdrantClient without per-start noise.

    - Version check off: only stable REST calls are used (collections,
      payload index, upsert, scroll, set_payload, delete, count and
      query_points), available since Qdrant server 1.10.
    - The 'API key over insecure connection' warning is kept for public
      hosts and silenced for loopback/private/Tailscale addresses.
    """
    with warnings.catch_warnings():
        if _is_internal_url(cfg.qdrant_url):
            warnings.filterwarnings("ignore", message="Api key is used with an insecure connection")
        return QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key,
                            timeout=int(cfg.qdrant_timeout), check_compatibility=False)


def _quiet_http_logs() -> None:
    """FastMCP configures INFO logging; without this every embedding and Qdrant
    request would be logged to the client's stderr log."""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.WARNING)


class SemanticIndex:
    """Chunk vectors of vault notes in one Qdrant collection."""

    def __init__(self, vault_root: Path, cfg: SemanticConfig | None = None, *,
                 client=None, embedder=None, state_dir: Path | None = None):
        self.cfg = cfg or SemanticConfig.from_env()
        if client is None:
            if not QDRANT_AVAILABLE:
                raise ImportError(f"qdrant-client is not installed. Run: {INSTALL_HINT}")
            client = _make_client(self.cfg)
        self.client = client
        _quiet_http_logs()
        self.embedder = embedder or Embedder(self.cfg)
        if state_dir is None:
            from .search_index import default_index_dir

            state_dir = default_index_dir(Path(vault_root))
        self.pending = PendingStore(Path(state_dir) / PENDING_FILE)
        self._collection_ready = False
        self._down_until = 0.0
        self.last_error = ""

    # -- helpers ------------------------------------------------------------

    @property
    def collection(self) -> str:
        return self.cfg.collection

    def excluded(self, rel_path: str) -> bool:
        return is_under(rel_path, self.cfg.exclude)

    def backend_down(self) -> bool:
        return time.monotonic() < self._down_until

    def _mark_down(self, err: Exception) -> None:
        self.last_error = f"{type(err).__name__}: {err}"
        self._down_until = time.monotonic() + self.cfg.backoff_seconds

    def _exists(self) -> bool:
        if self._collection_ready:
            return True
        return bool(self.client.collection_exists(self.collection))

    def _ensure_collection(self, dim: int) -> None:
        if self._collection_ready:
            return
        if self.client.collection_exists(self.collection):
            vectors = self.client.get_collection(self.collection).config.params.vectors
            size = getattr(vectors, "size", None)
            if size is not None and size != dim:
                raise RuntimeError(
                    f"Qdrant collection {self.collection!r} stores {size}-dim vectors but "
                    f"{self.cfg.embed_model!r} returns {dim} dims. Use a separate collection "
                    "(OBSIDIAN_QDRANT_COLLECTION) for a different embedding model."
                )
        else:
            try:
                self.client.create_collection(
                    self.collection,
                    vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
                )
            except Exception:
                # another server process created it a moment earlier
                if not self.client.collection_exists(self.collection):
                    raise
            for field_name in ("path", "prefixes"):
                # local (in-process) mode has no payload indexes and only warns
                with contextlib.suppress(Exception), warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self.client.create_payload_index(self.collection, field_name,
                                                     field_schema=qm.PayloadSchemaType.KEYWORD)
        self._collection_ready = True

    def _path_filter(self, rel_path: str):
        return qm.Filter(must=[qm.FieldCondition(key="path", match=qm.MatchValue(value=rel_path))])

    def _existing(self, rel_path: str, with_vectors: bool = False) -> dict:
        """{point_id: record} for one note."""
        if not self._exists():
            return {}
        out: dict = {}
        offset = None
        while True:
            points, offset = self.client.scroll(
                self.collection, scroll_filter=self._path_filter(rel_path), limit=512, offset=offset,
                with_payload=["meta_hash", "chunk_hash"], with_vectors=with_vectors,
            )
            for p in points:
                out[str(p.id)] = p
            if offset is None:
                return out

    def _scan(self) -> dict[str, dict[str, str]]:
        """{path: {point_id: meta_hash}} for the whole collection."""
        if not self._exists():
            return {}
        out: dict[str, dict[str, str]] = {}
        offset = None
        while True:
            points, offset = self.client.scroll(
                self.collection, limit=1024, offset=offset,
                with_payload=["path", "meta_hash"], with_vectors=False,
            )
            for p in points:
                payload = p.payload or {}
                out.setdefault(payload.get("path", ""), {})[str(p.id)] = payload.get("meta_hash", "")
            if offset is None:
                return out

    def _payload(self, rel_path: str, chunk: Chunk, meta: dict, mhash: str) -> dict:
        return {
            "path": rel_path,
            "prefixes": folder_prefixes(rel_path),
            "layer": policies.classify_layer(rel_path),
            "heading": chunk.heading,
            "text": chunk.text,
            "chunk_hash": chunk.hash,
            "meta_hash": mhash,
            "model": self.cfg.embed_model,
            **meta,
        }

    # -- write path ---------------------------------------------------------

    def index_note(self, note, existing: dict | None = None, max_new: int | None = None) -> dict:
        """Bring one note's points in line with its content (delta, idempotent).

        ``existing`` maps point id -> stored meta_hash (from a bulk scan); when
        omitted it is fetched from Qdrant. Unchanged notes cost no embedding call.
        With ``max_new``, a note needing more new embeddings is left untouched
        and reported as deferred (the write path hands it to sync instead).
        """
        rel_path = note.path
        if self.excluded(rel_path):
            return {"excluded": True, "removed": self.remove_note(rel_path)}
        meta = note_meta(note)
        mhash = _meta_hash(meta)
        chunks = chunk_note(meta["title"], getattr(note, "content", "") or "",
                            self.cfg.max_chunk_chars, self.cfg.doc_prefix)
        wanted = {point_id(rel_path, c.hash): c for c in chunks}
        if existing is None:
            existing = {pid: (p.payload or {}).get("meta_hash", "")
                        for pid, p in self._existing(rel_path).items()}
        new_ids = [i for i in wanted if i not in existing]
        stale = [i for i in existing if i not in wanted]
        relabel = [i for i in wanted if i in existing and existing[i] != mhash]
        if max_new is not None and len(new_ids) > max_new:
            return {"deferred": True, "chunks": len(wanted), "new": len(new_ids)}
        if new_ids:
            vectors = self.embedder.embed([wanted[i].embed_text for i in new_ids])
            self._ensure_collection(len(vectors[0]))
            self.client.upsert(self.collection, points=[
                qm.PointStruct(id=i, vector=v, payload=self._payload(rel_path, wanted[i], meta, mhash))
                for i, v in zip(new_ids, vectors)
            ])
        if relabel:
            self.client.set_payload(self.collection, payload={**meta, "meta_hash": mhash}, points=relabel)
        if stale:  # after the upsert: a note is never briefly without vectors
            self.client.delete(self.collection, points_selector=qm.PointIdsList(points=stale))
        return {"added": len(new_ids), "removed": len(stale), "relabeled": len(relabel),
                "chunks": len(wanted)}

    def remove_note(self, rel_path: str) -> int:
        if not self._exists():
            return 0
        n = self.client.count(self.collection, count_filter=self._path_filter(rel_path), exact=True).count
        if n:
            self.client.delete(self.collection, points_selector=qm.FilterSelector(filter=self._path_filter(rel_path)))
        return n

    def move_note(self, old_path: str, new_note) -> dict:
        """Re-key a moved note; reuses stored vectors for unchanged chunks."""
        if self.excluded(new_note.path):
            return {"removed": self.remove_note(old_path), "excluded": True}
        old = self._existing(old_path, with_vectors=True)
        by_hash = {(p.payload or {}).get("chunk_hash"): p.vector for p in old.values()}
        meta = note_meta(new_note)
        mhash = _meta_hash(meta)
        chunks = chunk_note(meta["title"], getattr(new_note, "content", "") or "",
                            self.cfg.max_chunk_chars, self.cfg.doc_prefix)
        missing = [c for c in chunks if not isinstance(by_hash.get(c.hash), list)]
        fresh = dict(zip((c.hash for c in missing), self.embedder.embed([c.embed_text for c in missing]))) \
            if missing else {}
        points = []
        for c in chunks:
            vec = by_hash.get(c.hash) if isinstance(by_hash.get(c.hash), list) else fresh[c.hash]
            points.append(qm.PointStruct(id=point_id(new_note.path, c.hash), vector=vec,
                                         payload=self._payload(new_note.path, c, meta, mhash)))
        if points:
            self._ensure_collection(len(points[0].vector))
            self.client.upsert(self.collection, points=points)
        if old:
            self.client.delete(self.collection, points_selector=qm.PointIdsList(points=list(old)))
        return {"moved": len(points), "reembedded": len(missing)}

    def on_write(self, rel_path: str, action: str, note) -> str:
        """Vault write hook. Never raises.

        Returns 'indexed', 'removed', 'skipped' (excluded folder), 'deferred'
        (too many new chunks for an inline update, queued for sync) or
        'pending' (backend error, queued for sync).

        For action 'archive', ``rel_path`` is the old path and ``note`` the moved note.
        """
        try:
            if self.backend_down():
                self.pending.add(rel_path if note is None else note.path)
                return "pending"
            if action == "delete" or note is None:
                self.remove_note(rel_path)
                self.pending.discard(rel_path)
                return "removed"
            if action == "archive" and note.path != rel_path:
                self.move_note(rel_path, note)
                self.pending.discard(rel_path, note.path)
                self.last_error = ""
                return "indexed"
            if self.excluded(rel_path):
                self.remove_note(rel_path)
                return "skipped"
            if self.index_note(note, max_new=self.cfg.inline_max_chunks).get("deferred"):
                # very long new note: embedding it now would stall the tool call
                self.pending.add(rel_path)
                return "deferred"
            self.pending.discard(rel_path)
            self.last_error = ""
            return "indexed"
        except Exception as e:  # fail-open: the write itself is done
            self._mark_down(e)
            with contextlib.suppress(OSError):
                self.pending.add(rel_path if note is None else note.path)
            return "pending"

    # -- read path ------------------------------------------------------------

    def search(self, query: str, limit: int = 10, folder: str = "",
               include_restricted: bool = False) -> list[dict]:
        """Nearest chunks, collapsed to one hit (best chunk) per note.

        Restricted folders are excluded by a Qdrant filter, i.e. before the
        top-k cut, so allowed notes are never crowded out by hidden ones.
        """
        if not query.strip() or not self._exists():
            return []
        vector = self.embedder.embed([f"{self.cfg.query_prefix}{query}"])[0]
        must, must_not = [], []
        folder_prefix = normalize_prefix(folder)
        if folder_prefix:
            must.append(qm.FieldCondition(key="prefixes", match=qm.MatchValue(value=folder_prefix)))
        if self.cfg.restricted and not include_restricted:
            must_not.append(qm.FieldCondition(key="prefixes", match=qm.MatchAny(any=list(self.cfg.restricted))))
        flt = qm.Filter(must=must or None, must_not=must_not or None) if (must or must_not) else None
        res = self.client.query_points(self.collection, query=vector, query_filter=flt,
                                       limit=max(limit * 4, 20), with_payload=True,
                                       score_threshold=self.cfg.min_score or None)
        hits: list[dict] = []
        seen: set[str] = set()
        for p in res.points:
            payload = p.payload or {}
            path = payload.get("path", "")
            if not path or path in seen:
                continue
            seen.add(path)
            hits.append({
                "path": path, "title": payload.get("title", ""), "heading": payload.get("heading", ""),
                "text": payload.get("text", ""), "uid": payload.get("uid", ""),
                "tags": payload.get("tags", []) or [], "score": round(float(p.score), 4),
            })
            if len(hits) >= limit:
                break
        return hits

    # -- maintenance ----------------------------------------------------------

    def status(self) -> dict:
        info = {"collection": self.collection, "model": self.cfg.embed_model,
                "qdrant_url": self.cfg.qdrant_url, "pending": len(self.pending.all()),
                "points": 0, "reachable": False, "last_error": self.last_error}
        try:
            if self._exists():
                info["points"] = self.client.count(self.collection, exact=True).count
            info["reachable"] = True
        except Exception as e:
            info["last_error"] = f"{type(e).__name__}: {e}"
        return info

    def _backend_ok(self) -> bool:
        """Tiny live probe of both backends (one short embedding, one Qdrant call)."""
        try:
            self.embedder.embed(["ping"])
            self.client.collection_exists(self.collection)
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def sync(self, notes, max_seconds: float | None = None, progress=None) -> dict:
        """Delta-sync all notes: embed what changed, drop what vanished.

        Pending notes go first. With ``max_seconds`` the run stops early and
        reports ``remaining``; calling again continues cheaply because
        unchanged notes need no embedding.
        """
        start = time.monotonic()
        existing_all = self._scan()
        notes = list(notes)
        present = {n.path for n in notes}
        removed = 0
        for path in list(existing_all):
            if path not in present or self.excluded(path):
                removed += self.remove_note(path)
                existing_all.pop(path, None)
        pending = set(self.pending.all())
        self.pending.discard(*(p for p in pending if p not in present))
        ordered = sorted(notes, key=lambda n: (n.path not in pending, n.path))
        stats = {"notes": len(ordered), "checked": 0, "changed": 0, "failed": 0,
                 "removed_points": removed, "remaining": 0}
        streak = 0
        reachable = outage = False  # backend answered at least once / run stopped by an outage
        for i, note in enumerate(ordered):
            if max_seconds is not None and time.monotonic() - start >= max_seconds:
                stats["remaining"] = len(ordered) - i
                break
            if streak >= 3:
                # Three failures in a row: stop only if the backend is really down.
                # Note-specific failures (an oversized section, a bad file) must not
                # stall the run - pending notes go first, so they would block every
                # later run as well.
                if not self._backend_ok():
                    outage = True
                    stats["remaining"] = len(ordered) - i
                    break
                reachable = True
                streak = 0
            try:
                r = self.index_note(note, existing=existing_all.get(note.path, {}))
                if r.get("added") or r.get("removed") or r.get("relabeled"):
                    stats["changed"] += 1
                if note.path in pending:
                    self.pending.discard(note.path)
                reachable = True
                streak = 0
            except Exception as e:
                stats["failed"] += 1
                streak += 1
                self.last_error = f"{type(e).__name__}: {e}"
                self.pending.add(note.path)
            stats["checked"] += 1
            if progress and stats["checked"] % 100 == 0:
                progress(stats)
        stats["complete"] = stats["remaining"] == 0 and stats["failed"] == 0
        stats["last_error"] = self.last_error if stats["failed"] else ""
        if stats["failed"] == 0:
            self.last_error = ""
        if stats["failed"] == 0 or (reachable and not outage):
            # backend proved reachable (failures were note-specific): writes index inline again
            self._down_until = 0.0
        else:
            self._down_until = time.monotonic() + self.cfg.backoff_seconds
        stats["seconds"] = round(time.monotonic() - start, 1)
        return stats


# ---- CLI -------------------------------------------------------------------


def cli(argv: list[str] | None = None) -> int:
    """obsidian-mcp-semantic status | sync [--max-seconds N] | eval FILE [--k N] [--json]
    [--dump OUT.json] | eval DUMP.json --replay

    eval --dump stores the raw ranked lists of every retriever; eval --replay
    re-ranks such a dump offline with the current ranking settings
    (OBSIDIAN_SEARCH_DEMOTE, OBSIDIAN_SEARCH_DEMOTE_WEIGHT, ...), so settings
    can be compared without index, Qdrant or embedding backend.

    sync also reconciles the Whoosh full-text index with the files on disk
    (notes moved, edited or deleted outside the server).

    Run the initial bulk indexing here (on the server, in the background),
    not through the MCP tool: thousands of notes on a CPU-only Ollama take long.
    """
    parser = argparse.ArgumentParser(prog="obsidian-mcp-semantic", description=cli.__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show collection, point count and pending notes")
    p_sync = sub.add_parser("sync", help="embed new/changed notes, remove vanished ones")
    p_sync.add_argument("--max-seconds", type=float, default=None)
    p_eval = sub.add_parser("eval", help="compare search_query and hybrid_search on a question set "
                            "(YAML/JSON file or the first ```yaml block of a Markdown note)")
    p_eval.add_argument("file")
    p_eval.add_argument("--k", type=int, default=None, help="top k per retriever (default 10; replay: as dumped)")
    p_eval.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    p_eval.add_argument("--include-restricted", action="store_true",
                        help="also search OBSIDIAN_SEARCH_RESTRICTED folders")
    p_eval.add_argument("--dump", metavar="OUT", help="also write the raw ranked lists as JSON to OUT")
    p_eval.add_argument("--replay", action="store_true",
                        help="FILE is a --dump file: re-rank it with the current settings, offline")
    args = parser.parse_args(argv)

    if args.cmd == "eval" and args.replay:
        from .evaluation import replay_eval, to_markdown

        try:
            result = replay_eval(json.loads(Path(args.file).read_text(encoding="utf-8")),
                                 k=args.k)
        except (OSError, ValueError) as e:
            print(f"replay: {e}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else to_markdown(result))
        return 0

    root = os.environ.get("OBSIDIAN_VAULT_ROOT", "")
    if not root:
        print("OBSIDIAN_VAULT_ROOT is not set.", file=sys.stderr)
        return 2
    if not QDRANT_AVAILABLE:
        print(f"qdrant-client is not installed. Run: {INSTALL_HINT}", file=sys.stderr)
        return 2
    from .vault import Vault

    vault = Vault(root)
    if args.cmd == "eval":
        from .evaluation import load_questions, run_eval, to_markdown

        # This CLI is the semantic tool: hybrid_search must not silently fall
        # back to full text because the server flag is missing in the shell.
        os.environ.setdefault("OBSIDIAN_SEMANTIC", "1")

        qpath = Path(args.file).resolve()
        try:  # the question note lives in the vault: never count it as a hit
            exclude = (qpath.relative_to(Path(vault.root).resolve()).as_posix(),)
        except ValueError:
            exclude = ()
        result = run_eval(vault, load_questions(args.file), k=args.k or 10,
                          include_restricted=args.include_restricted, exclude_paths=exclude,
                          dump=bool(args.dump))
        if args.dump:
            Path(args.dump).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            result = {key: v for key, v in result.items() if key != "dump"}
            result["rows"] = [{key: v for key, v in r.items() if key != "lists"} for r in result["rows"]]
        print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else to_markdown(result))
        return 0
    index = SemanticIndex(vault.root)
    if args.cmd == "status":
        print(json.dumps(index.status(), indent=2, ensure_ascii=False))
        return 0
    notes = vault.list_notes()
    fulltext = None
    ix = vault._init_index()
    if ix is not None:  # keep the full-text index in step with out-of-band changes too
        try:
            fulltext = ix.reconcile(notes)
        except Exception as e:
            fulltext = {"error": f"{type(e).__name__}: {e}"}

    def progress(s):
        print(f"[obsidian-mcp-semantic] {s['checked']}/{s['notes']} checked, "
              f"{s['changed']} changed, {s['failed']} failed", file=sys.stderr, flush=True)

    stats = index.sync(notes, max_seconds=args.max_seconds, progress=progress)
    if fulltext is not None:
        stats["fulltext"] = fulltext
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli())
