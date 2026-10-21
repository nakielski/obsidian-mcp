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
DEMOTE_WEIGHT = 0.5  # score factor for OBSIDIAN_SEARCH_DEMOTE folders
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


def _recency(path: str) -> tuple:
    """(version, date) from a file name: 'Entwurf v0.7 2026-10-20' -> ((0, 7), '2026-10-20')."""
    stem = path.rsplit("/", 1)[-1].lower()
    versions = [tuple(int(x) for x in m.group(0)[1:].split(".")) for m in _VERSION_RE.finditer(stem)]
    dates = _DATE_RE.findall(stem)
    return (max(versions) if versions else (), max(dates) if dates else "")


def _is_secondary(path: str, secondary: tuple[str, ...]) -> bool:
    low = path.lower()
    return any(low == f.lower().strip("/") or low.startswith(f.lower().strip("/") + "/") for f in secondary)


def _glob_regex(pattern: str) -> str:
    """Folder prefix or glob -> anchored regex on a casefolded vault path.

    '05 People' / '05 People/' -> everything below that folder;
    '*' matches within one path segment, '**' any number of folders
    ('raw/repos/*/*/' = everything at least two folders below raw/repos,
    '**/node_modules/' = every node_modules folder); a pattern ending in
    '.md' names one note.
    """
    p = pattern.strip().replace("\\", "/").lstrip("/").casefold()
    if not p:
        return ""
    is_file = p.endswith(".md")
    parts = [x for x in p.rstrip("/").split("/") if x]
    rx = ""
    for i, part in enumerate(parts):
        if part == "**":
            rx += "(?:[^/]+/)*"
            continue
        rx += "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in part)
        if i < len(parts) - 1 or not is_file:
            rx += "/"
    return rx + ("$" if is_file else "")


def path_matcher(patterns) -> re.Pattern | None:
    """One compiled matcher for a list of folder prefixes / globs (None if empty)."""
    parts = [r for r in (_glob_regex(x) for x in patterns or ()) if r]
    return re.compile("|".join(f"(?:{r})" for r in parts)) if parts else None


def rrf_fuse(fulltext: list[dict], semantic: list[dict], limit: int, k: int = RRF_K,
             reserve: int = RESERVE, secondary: tuple[str, ...] = SECONDARY_DEFAULT, *,
             all_terms: list[dict] | None = None, demote: tuple[str, ...] = (),
             demote_weight: float = DEMOTE_WEIGHT) -> list[dict]:
    """Merge ranked hit lists: Reciprocal Rank Fusion plus four corrections.

    1. RRF: score = sum of 1 / (k + rank) per retriever, rank 1-based. Needs no
       score calibration between BM25 and cosine similarity; a note found by
       both retrievers outranks one found by a single retriever at the same rank.
    2. All terms: ``all_terms`` is the full-text result with every query word
       required (AND). It counts as a third list, so for keyword queries
       ('Tailscale ACL') notes containing all words beat notes that repeat one
       word often. Natural-language questions rarely match with AND, then the
       list is empty and changes nothing.
    3. Demotion: hits in the ``demote`` folders (prefixes or globs, see
       ``path_matcher``), typically mirrored third-party documentation, get
       ``demote_weight`` times the score and never take a reserved slot. They
       are still found, just ranked behind comparable own notes.
    4. Variants: versions and copies of one note (see ``variant_key``) count as
       one hit. The newest live copy represents the group (highest version,
       then date in the file name; a copy outside the ``secondary`` folders
       such as the archive wins if there is one); the others are listed in
       ``variants``. Otherwise five drafts of one text fill five of ten slots.
    5. Reserve: the ``reserve`` best groups of each retriever are always in the
       result (full text: notes with all words first). With k=60 any note found
       by both lists (even at rank 30 of a loose OR full-text query) beats the
       best note found by only one list, so the semantic top hits of a
       natural-language question could drop out entirely.
    """
    demoted = path_matcher(demote)
    weight = min(max(float(demote_weight), 0.0), 1.0)

    def is_demoted(path: str) -> bool:
        return bool(demoted and demoted.match(path.replace("\\", "/").casefold()))

    merged: dict[str, dict] = {}
    lists = (("fulltext", fulltext), ("semantic", semantic), ("all_terms", all_terms or []))
    for source, hits in lists:
        for rank, hit in enumerate(hits, start=1):
            entry = merged.setdefault(hit["path"], {
                "path": hit["path"], "title": hit.get("title", ""), "heading": "", "text": "",
                "uid": hit.get("uid", ""), "tags": list(hit.get("tags") or []),
                "score": 0.0, "sources": [], "fulltext_rank": None, "semantic_rank": None,
                "all_terms_rank": None, "variants": [], "demoted": is_demoted(hit["path"]),
            })
            if entry[f"{source}_rank"] is not None:  # duplicate path within one list
                continue
            entry["score"] += (weight if entry["demoted"] else 1.0) / (k + rank)
            entry[f"{source}_rank"] = rank
            label = "fulltext" if source == "all_terms" else source
            if label not in entry["sources"]:
                entry["sources"].append(label)
            if source == "semantic":
                entry["heading"] = hit.get("heading", "")
                entry["text"] = hit.get("text", "")
            for key in ("title", "uid"):
                if not entry[key] and hit.get(key):
                    entry[key] = hit[key]
    for e in merged.values():  # stable output order of sources
        e["sources"].sort(key=("fulltext", "semantic").index)

    # full-text order for the reserve: notes with all words first, then the rest
    ft_order = [h["path"] for h in all_terms or []] + [h["path"] for h in fulltext]
    ft_pos: dict[str, int] = {}
    for p in ft_order:
        ft_pos.setdefault(p, len(ft_pos) + 1)

    def order(e: dict):
        return (-e["score"], -len(e["sources"]), e["path"])

    groups: dict[str, list[dict]] = {}
    for e in sorted(merged.values(), key=order):
        groups.setdefault(variant_key(e["path"]), []).append(e)

    reps: list[dict] = []
    best_rank: dict[int, dict[str, int]] = {}
    for members in groups.values():
        live = [m for m in members if not _is_secondary(m["path"], secondary)] or members
        # newest version (then date) represents the group; score breaks ties
        rep = dict(max(live, key=lambda m: (_recency(m["path"]), m["score"])))
        rep["score"] = members[0]["score"]  # best member decides the rank; copies add nothing
        rep["variants"] = [m["path"] for m in members if m["path"] != rep["path"]]
        reps.append(rep)
        best_rank[id(rep)] = {
            "fulltext": min((ft_pos[m["path"]] for m in members if m["path"] in ft_pos), default=0),
            "semantic": min((m["semantic_rank"] for m in members if m["semantic_rank"] is not None),
                            default=0),
        }
    reps.sort(key=order)

    chosen = reps[:limit]
    if reserve > 0 and limit > 1:
        keep: list[dict] = []
        for src in ("semantic", "fulltext"):
            ranked = sorted((r for r in reps if best_rank[id(r)][src] and not r["demoted"]),
                            key=lambda r: best_rank[id(r)][src])
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


def collect(vault, query: str, limit: int = 10, folder: str = "",
            include_restricted: bool = False, cfg=None, candidates: int | None = None) -> dict:
    """Run the retrievers with the same folder policy; return the raw ranked lists.

    {"fulltext": OR full text, "all_terms": AND full text (only for queries of
    two or more words), "semantic": vectors, "ft_ok", "sem_ok", "warnings"}.
    ``candidates`` overrides the list length (the eval dump fetches more).
    """
    from .semantic_index import INSTALL_HINT, QDRANT_AVAILABLE, SemanticConfig, normalize_prefix

    cfg = cfg or SemanticConfig.from_env()
    restricted = () if include_restricted else cfg.restricted
    folder_prefix = normalize_prefix(folder)
    n = candidates or candidate_count(limit, cfg)
    out = {"fulltext": [], "all_terms": [], "semantic": [], "ft_ok": False, "sem_ok": False,
           "warnings": []}
    warnings = out["warnings"]

    ix = vault._init_index()
    if ix is None:
        warnings.append("full-text index unavailable (VAULT_INDEX=0 or whoosh3 missing)")
    else:
        try:
            if query.strip():
                out["fulltext"] = ix.search(query, limit=n, any_term=True,
                                            folder=folder_prefix, exclude_prefixes=restricted)
                if len(query.split()) > 1:
                    out["all_terms"] = ix.search(query, limit=n, any_term=False,
                                                 folder=folder_prefix, exclude_prefixes=restricted)
            out["ft_ok"] = True
        except Exception as e:  # e.g. unbalanced query syntax
            warnings.append(f"full-text query failed: {e}")

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
                out["semantic"] = sx.search(query, limit=n, folder=folder,
                                            include_restricted=include_restricted)
            out["sem_ok"] = True
        except Exception as e:
            sx._mark_down(e)
            warnings.append(f"semantic search failed: {type(e).__name__}: {e}")
    return out


def candidate_count(limit: int, cfg) -> int:
    """List length per retriever: three times the result size, at least cfg.candidates."""
    return max(limit * 3, cfg.candidates)


def fuse(lists: dict, limit: int, cfg, candidates: int | None = None) -> list[dict]:
    """``rrf_fuse`` with the configured policy; lists are cut to ``candidates``."""
    n = candidates or candidate_count(limit, cfg)
    return rrf_fuse(lists.get("fulltext", [])[:n], lists.get("semantic", [])[:n], limit,
                    secondary=cfg.secondary, all_terms=lists.get("all_terms", [])[:n],
                    demote=cfg.demote, demote_weight=cfg.demote_weight)


def hybrid_query(vault, query: str, limit: int = 10, folder: str = "",
                 include_restricted: bool = False) -> dict:
    """Run both retrievers with the same folder policy and fuse the results."""
    from .semantic_index import SemanticConfig

    if limit <= 0:
        raise ValueError("limit must be positive")
    limit = min(limit, 50)
    cfg = SemanticConfig.from_env()
    restricted = () if include_restricted else cfg.restricted
    lists = collect(vault, query, limit, folder, include_restricted, cfg)
    ft_ok, sem_ok, warnings = lists["ft_ok"], lists["sem_ok"], lists["warnings"]
    if not ft_ok and not sem_ok:
        raise RuntimeError("No search backend available: " + "; ".join(warnings))

    results = fuse(lists, limit, cfg)
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
