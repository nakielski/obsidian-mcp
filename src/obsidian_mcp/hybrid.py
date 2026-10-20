"""Hybrid retrieval: Whoosh full text + semantic vectors, fused by Reciprocal Rank Fusion.

Kept free of the optional Qdrant dependency: when the semantic side is off,
unreachable or not installed, ``hybrid_query`` degrades to full text and says
so in ``mode`` and ``warning`` instead of failing.
"""
from __future__ import annotations

import re

RRF_K = 60
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def rrf_fuse(fulltext: list[dict], semantic: list[dict], limit: int, k: int = RRF_K) -> list[dict]:
    """Merge two ranked hit lists by path: score = sum of 1 / (k + rank), rank 1-based.

    Rank fusion needs no score calibration between BM25 and cosine similarity.
    A note found by both retrievers outranks one found by a single retriever
    at the same rank.
    """
    merged: dict[str, dict] = {}
    for source, hits in (("fulltext", fulltext), ("semantic", semantic)):
        for rank, hit in enumerate(hits, start=1):
            entry = merged.setdefault(hit["path"], {
                "path": hit["path"], "title": hit.get("title", ""), "heading": "", "text": "",
                "uid": hit.get("uid", ""), "tags": list(hit.get("tags") or []),
                "score": 0.0, "sources": [], "fulltext_rank": None, "semantic_rank": None,
            })
            entry["score"] += 1.0 / (k + rank)
            entry["sources"].append(source)
            entry[f"{source}_rank"] = rank
            if source == "semantic":
                entry["heading"] = hit.get("heading", "")
                entry["text"] = hit.get("text", "")
            for key in ("title", "uid"):
                if not entry[key] and hit.get(key):
                    entry[key] = hit[key]
    ranked = sorted(merged.values(), key=lambda e: (-e["score"], -len(e["sources"]), e["path"]))
    for e in ranked:
        e["score"] = round(e["score"], 6)
    return ranked[:limit]


def make_snippet(text: str, max_chars: int = 280) -> str:
    flat = _WS_RE.sub(" ", _TAG_RE.sub("", text or "")).strip()
    return flat if len(flat) <= max_chars else flat[: max_chars - 1].rstrip() + "…"


def hybrid_query(vault, query: str, limit: int = 10, folder: str = "",
                 include_restricted: bool = False) -> dict:
    """Run both retrievers with the same folder policy and fuse the results."""
    from .semantic_index import INSTALL_HINT, QDRANT_AVAILABLE, SemanticConfig, normalize_prefix

    if limit <= 0:
        raise ValueError("limit must be positive")
    limit = min(limit, 50)
    cfg = SemanticConfig.from_env()
    restricted = () if include_restricted else cfg.restricted
    folder_prefix = normalize_prefix(folder)
    candidates = max(limit * 3, 30)
    warnings: list[str] = []

    fulltext: list[dict] = []
    ft_ok = False
    ix = vault._init_index()
    if ix is None:
        warnings.append("full-text index unavailable (VAULT_INDEX=0 or whoosh3 missing)")
    else:
        try:
            if query.strip():
                fulltext = ix.search(query, limit=candidates, any_term=True,
                                     folder=folder_prefix, exclude_prefixes=restricted)
            ft_ok = True
        except Exception as e:  # e.g. unbalanced query syntax
            warnings.append(f"full-text query failed: {e}")

    semantic: list[dict] = []
    sem_ok = False
    sx = vault._init_semantic()
    if sx is None:
        reason = getattr(vault, "semantic_error", "") or (
            f"qdrant-client not installed ({INSTALL_HINT})" if not QDRANT_AVAILABLE
            else "semantic index disabled (OBSIDIAN_SEMANTIC=1 enables it)")
        warnings.append(f"semantic search unavailable: {reason}")
    elif sx.backend_down():
        warnings.append(f"semantic search paused after an error: {sx.last_error}")
    else:
        try:
            if query.strip():
                semantic = sx.search(query, limit=candidates, folder=folder,
                                     include_restricted=include_restricted)
            sem_ok = True
        except Exception as e:
            sx._mark_down(e)
            warnings.append(f"semantic search failed: {type(e).__name__}: {e}")

    if not ft_ok and not sem_ok:
        raise RuntimeError("No search backend available: " + "; ".join(warnings))

    results = rrf_fuse(fulltext, semantic, limit)
    for r in results:
        snippet = r.pop("text", "")
        if not snippet:
            try:
                snippet = vault.read(r["path"]).content
            except Exception:
                snippet = ""
        r["snippet"] = make_snippet(snippet)

    mode = "hybrid" if ft_ok and sem_ok else ("semantic" if sem_ok else "fulltext")
    return {
        "query": query,
        "mode": mode,
        "warning": "; ".join(warnings),
        "restricted_hidden": bool(restricted),
        "total": len(results),
        "results": results,
    }
