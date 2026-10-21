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


RESERVE = 3  # best hits of each retriever that always make it into the result
SECONDARY_DEFAULT = ("06 Archive", "raw")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_VERSION_RE = re.compile(r"\bv\d+(?:\.\d+)*\b")
_NOISE_RE = re.compile(r"\b(?:final|kopie|copy)\b")
_SPLIT_RE = re.compile(r"[\W_]+")


def variant_key(path: str) -> str:
    """Group key for versions and copies of one note.

    'MCP Artikel 2 Entwurf v0.6 2026-10-19.md' in a project folder and
    '06 Archive/MCP Artikel 2 Entwurf v0.3 (2026-10-18).md' share the key
    'mcp artikel 2 entwurf': folder, dates, version numbers and brackets are
    ignored. Generic file names (index.md, SKILL.md, a bare date) keep their
    path as key, so unrelated notes are never merged.
    """
    stem = path.rsplit("/", 1)[-1]
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    s = _NOISE_RE.sub(" ", _VERSION_RE.sub(" ", _DATE_RE.sub(" ", stem.lower())))
    words = [w for w in _SPLIT_RE.split(s) if w]
    if len(words) < 2 or len("".join(words)) < 8:
        return path
    return " ".join(words)


def _is_secondary(path: str, secondary: tuple[str, ...]) -> bool:
    low = path.lower()
    return any(low == f.lower().strip("/") or low.startswith(f.lower().strip("/") + "/") for f in secondary)


def rrf_fuse(fulltext: list[dict], semantic: list[dict], limit: int, k: int = RRF_K,
             reserve: int = RESERVE, secondary: tuple[str, ...] = SECONDARY_DEFAULT) -> list[dict]:
    """Merge two ranked hit lists: Reciprocal Rank Fusion plus two corrections.

    1. RRF: score = sum of 1 / (k + rank) per retriever, rank 1-based. Needs no
       score calibration between BM25 and cosine similarity; a note found by
       both retrievers outranks one found by a single retriever at the same rank.
    2. Variants: versions and copies of one note (see ``variant_key``) count as
       one hit. The live copy represents the group (not one in a ``secondary``
       folder such as the archive, if there is one); the others are listed in
       ``variants``. Otherwise five drafts of one text fill five of ten slots.
    3. Reserve: the ``reserve`` best groups of each retriever are always in the
       result. With k=60 any note found by both lists (even at rank 30 of a
       loose OR full-text query) beats the best note found by only one list,
       so the semantic top hits of a natural-language question could drop out
       entirely.
    """
    merged: dict[str, dict] = {}
    for source, hits in (("fulltext", fulltext), ("semantic", semantic)):
        for rank, hit in enumerate(hits, start=1):
            entry = merged.setdefault(hit["path"], {
                "path": hit["path"], "title": hit.get("title", ""), "heading": "", "text": "",
                "uid": hit.get("uid", ""), "tags": list(hit.get("tags") or []),
                "score": 0.0, "sources": [], "fulltext_rank": None, "semantic_rank": None,
                "variants": [],
            })
            if entry[f"{source}_rank"] is not None:  # duplicate path within one list
                continue
            entry["score"] += 1.0 / (k + rank)
            entry["sources"].append(source)
            entry[f"{source}_rank"] = rank
            if source == "semantic":
                entry["heading"] = hit.get("heading", "")
                entry["text"] = hit.get("text", "")
            for key in ("title", "uid"):
                if not entry[key] and hit.get(key):
                    entry[key] = hit[key]

    def order(e: dict):
        return (-e["score"], -len(e["sources"]), e["path"])

    groups: dict[str, list[dict]] = {}
    for e in sorted(merged.values(), key=order):
        groups.setdefault(variant_key(e["path"]), []).append(e)

    reps: list[dict] = []
    best_rank: dict[int, dict[str, int]] = {}
    for members in groups.values():
        live = [m for m in members if not _is_secondary(m["path"], secondary)]
        rep = dict((live or members)[0])
        rep["score"] = members[0]["score"]  # best member decides the rank; copies add nothing
        rep["variants"] = [m["path"] for m in members if m["path"] != rep["path"]]
        reps.append(rep)
        best_rank[id(rep)] = {
            src: min((m[f"{src}_rank"] for m in members if m[f"{src}_rank"] is not None), default=0)
            for src in ("fulltext", "semantic")
        }
    reps.sort(key=order)

    chosen = reps[:limit]
    if reserve > 0 and limit > 1:
        keep: list[dict] = []
        for src in ("semantic", "fulltext"):
            ranked = sorted((r for r in reps if best_rank[id(r)][src]), key=lambda r: best_rank[id(r)][src])
            keep += [r for r in ranked[: min(reserve, limit // 2)] if all(r is not x for x in keep)]
        for r in keep:
            if any(r is c for c in chosen):
                continue
            drop = next((c for c in reversed(chosen) if all(c is not x for x in keep)), None)
            if drop is None:
                break
            chosen = [c for c in chosen if c is not drop] + [r]
        chosen.sort(key=order)

    for e in chosen:
        e["score"] = round(e["score"], 6)
    return chosen


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

    results = rrf_fuse(fulltext, semantic, limit, secondary=cfg.secondary)
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
