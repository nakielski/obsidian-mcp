"""Compare full-text search and hybrid search on a set of real questions.

The question set is YAML (or JSON), either as a file of its own or as the first
```yaml block of a Markdown note, so it can live in the vault:

    - frage: Wie verhindere ich, dass ein Agent manipulierte Anweisungen ausfuehrt?
      erwartet: [agent-safety, Phase 0 - Sicherheitsarchitektur]
    - question: sovereign cloud requirements for public sector
      expect: ["04 Resources/STACKIT/"]

`erwartet`/`expect` lists path fragments: a hit counts when one of them is
contained in the hit's path (case-insensitive; a folder ending in '/' counts
every note below it). For hybrid hits the folded `variants` count as well.

Two retrievers are compared under the same folder policy:
  - fulltext: the `search_query` tool (Whoosh BM25, plain terms combined with AND)
  - hybrid:   the `hybrid_search` tool (full text OR + vectors, fused)
Reported per question: rank of the first expected note (1-based, None = not in
the top k), plus hit@1/@5/@k and the mean reciprocal rank per retriever.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_YAML_BLOCK = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)


def load_questions(path: str | Path) -> list[dict]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".md":
        m = _YAML_BLOCK.search(text)
        if not m:
            raise ValueError(f"{p}: no ```yaml block found")
        text = m.group(1)
    if p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    if isinstance(data, dict):
        data = data.get("fragen") or data.get("questions") or []
    out = []
    for i, item in enumerate(data or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"entry {i}: expected a mapping with frage/erwartet")
        q = str(item.get("frage") or item.get("question") or item.get("q") or "").strip()
        exp = item.get("erwartet", item.get("expect", []))
        exp = [exp] if isinstance(exp, str) else list(exp or [])
        exp = [e for e in exp if str(e or "").strip()]
        if not q and not exp:
            continue  # empty template row
        if not q or not exp:
            raise ValueError(f"entry {i}: frage and erwartet are required")
        out.append({"id": len(out) + 1, "question": q, "expect": [str(e) for e in exp],
                    "note": str(item.get("notiz") or item.get("note") or "")})
    if not out:
        raise ValueError(f"{p}: no questions")
    return out


def _matches(path: str, expect: list[str]) -> bool:
    low = path.casefold()
    return any(e.casefold().strip() in low for e in expect if e.strip())


def first_rank(hits: list[dict], expect: list[str]) -> int | None:
    for rank, h in enumerate(hits, start=1):
        if _matches(h.get("path", ""), expect) or any(_matches(v, expect) for v in h.get("variants") or []):
            return rank
    return None


DUMP_CANDIDATES = 100  # list length stored by --dump, enough to replay larger settings


def _hit_rank(results: list[dict], expect: list[str]) -> tuple[int | None, list[str]]:
    hit = next((h for h in results if _matches(h["path"], expect)
                or any(_matches(v, expect) for v in h.get("variants") or [])), None)
    return first_rank(results, expect), (hit["sources"] if hit else [])


def _slim(hits: list[dict]) -> list[str]:
    return [h["path"] for h in hits]  # paths are all the fusion needs


def run_eval(vault, questions: list[dict], k: int = 10, include_restricted: bool = False,
             exclude_paths: tuple[str, ...] = (), dump: bool = False) -> dict:
    """``exclude_paths``: vault-relative paths never counted as hits, typically the
    question note itself (it contains every question, so it matches all of them).

    ``dump``: also store the raw ranked lists of every retriever per question
    (``lists``), so ``replay_eval`` can re-rank them offline with other settings.
    """
    from . import policies
    from .hybrid import candidate_count, collect, fuse
    from .semantic_index import SemanticConfig

    cfg = SemanticConfig.from_env()
    hidden = () if include_restricted else policies.restricted_prefixes()
    ix = vault._init_index()
    skip = {p.replace("\\", "/").casefold() for p in exclude_paths}

    def keep(hits):
        return [h for h in hits if h.get("path", "").casefold() not in skip][:k]

    extra = len(skip)
    limit = k + extra
    rows = []
    for q in questions:
        row = dict(q)
        try:
            ft = keep(ix.search(q["question"], limit=limit, exclude_prefixes=hidden)) if ix is not None else []
            row["fulltext_rank"] = first_rank(ft, q["expect"])
            row["fulltext_top"] = [h["path"] for h in ft[:3]]
        except Exception as e:  # e.g. query syntax
            row["fulltext_rank"], row["fulltext_top"], row["fulltext_error"] = None, [], str(e)
        fetch = max(DUMP_CANDIDATES, candidate_count(limit, cfg)) if dump else None
        lists = collect(vault, q["question"], limit, "", include_restricted, cfg, candidates=fetch,
                        all_terms=True if dump else None)  # the dump keeps the AND list for replays
        if not lists["ft_ok"] and not lists["sem_ok"]:
            raise RuntimeError("No search backend available: " + "; ".join(lists["warnings"]))
        results = keep(fuse(lists, limit, cfg))
        row["hybrid_mode"] = ("hybrid" if lists["ft_ok"] and lists["sem_ok"]
                              else "semantic" if lists["sem_ok"] else "fulltext")
        row["hybrid_warning"] = "; ".join(lists["warnings"])
        row["hybrid_rank"], row["hybrid_sources"] = _hit_rank(results, q["expect"])
        row["hybrid_top"] = [h["path"] for h in results[:3]]
        if dump:
            row["lists"] = {name: _slim(lists[name]) for name in ("fulltext", "all_terms", "semantic")}
        rows.append(row)
    out = {"k": k, "questions": len(rows), "summary": summarize(rows, k), "rows": rows,
           "settings": settings(cfg)}
    if dump:
        out["dump"] = {"exclude_paths": sorted(skip)}
    return out


def settings(cfg) -> dict:
    """The ranking settings a run used (printed with every report)."""
    return {"demote": list(cfg.demote), "demote_weight": cfg.demote_weight,
            "candidates": cfg.candidates, "all_terms_weight": cfg.all_terms_weight,
            "secondary": list(cfg.secondary)}


def replay_eval(dump: dict, k: int | None = None) -> dict:
    """Re-rank the stored lists of an ``eval --dump`` file with the current
    settings (OBSIDIAN_SEARCH_DEMOTE, ..._WEIGHT, OBSIDIAN_HYBRID_CANDIDATES,
    OBSIDIAN_HYBRID_ALL_TERMS_WEIGHT, OBSIDIAN_SEARCH_SECONDARY) - no index, no Qdrant, no Ollama needed.
    Full-text ranks are taken over from the dump unchanged."""
    from .hybrid import fuse
    from .semantic_index import SemanticConfig

    if "dump" not in dump or not all("lists" in r for r in dump.get("rows", [])):
        raise ValueError("not an eval dump (run eval with --dump first)")
    cfg = SemanticConfig.from_env()
    k = k or dump["k"]
    skip = set(dump["dump"].get("exclude_paths") or [])
    limit = k + len(skip)
    rows = []
    for old in dump["rows"]:
        row = {key: v for key, v in old.items() if key != "lists"}
        if k != dump["k"] and row.get("fulltext_rank") and row["fulltext_rank"] > k:
            row["fulltext_rank"] = None
        lists = {name: [{"path": p} for p in paths] for name, paths in old["lists"].items()}
        results = [h for h in fuse(lists, limit, cfg) if h["path"].casefold() not in skip][:k]
        row["hybrid_rank"], row["hybrid_sources"] = _hit_rank(results, row["expect"])
        row["hybrid_top"] = [h["path"] for h in results[:3]]
        rows.append(row)
    return {"k": k, "questions": len(rows), "summary": summarize(rows, k), "rows": rows,
            "settings": settings(cfg), "replay": {"dump_settings": dump.get("settings", {})}}


def summarize(rows: list[dict], k: int) -> dict:
    out = {}
    for name in ("fulltext", "hybrid"):
        ranks = [r[f"{name}_rank"] for r in rows]
        n = len(ranks) or 1
        out[name] = {
            "hit@1": sum(1 for x in ranks if x == 1) / n,
            "hit@5": sum(1 for x in ranks if x and x <= 5) / n,
            f"hit@{k}": sum(1 for x in ranks if x) / n,
            "mrr": round(sum(1 / x for x in ranks if x) / n, 3),
        }
    out["only_hybrid"] = sum(1 for r in rows if r["hybrid_rank"] and not r["fulltext_rank"])
    out["only_fulltext"] = sum(1 for r in rows if r["fulltext_rank"] and not r["hybrid_rank"])
    return out


def _settings_line(result: dict) -> list[str]:
    st = result.get("settings")
    if not st:
        return []
    demote = ", ".join(f"`{d}`" for d in st["demote"]) or "–"
    label = "Nachgerechnet mit" if "replay" in result else "Einstellungen"
    return [f"{label}: zurückgestuft {demote} (Faktor {st['demote_weight']}), "
            f"Kandidaten je Liste ≥ {st['candidates']}, "
            f"UND-Liste Gewicht {st.get('all_terms_weight', 0)}", ""]


def to_markdown(result: dict) -> str:
    k, s = result["k"], result["summary"]

    def pct(x):
        return f"{round(x * 100)} %"

    lines = [
        "| | Volltext (`search_query`) | Hybrid (`hybrid_search`) |",
        "|---|---|---|",
        f"| Treffer auf Platz 1 | {pct(s['fulltext']['hit@1'])} | {pct(s['hybrid']['hit@1'])} |",
        f"| Treffer unter den ersten 5 | {pct(s['fulltext']['hit@5'])} | {pct(s['hybrid']['hit@5'])} |",
        f"| Treffer unter den ersten {k} | {pct(s['fulltext'][f'hit@{k}'])} | {pct(s['hybrid'][f'hit@{k}'])} |",
        f"| MRR | {s['fulltext']['mrr']} | {s['hybrid']['mrr']} |",
        "",
        f"Nur Hybrid findet: {s['only_hybrid']} · nur Volltext findet: {s['only_fulltext']} "
        f"· Fragen: {result['questions']}",
        "",
        *_settings_line(result),
        "| # | Frage | Volltext | Hybrid | Quellen (Hybrid) |",
        "|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        q = r["question"].replace("|", "\\|")
        ft = r["fulltext_rank"] or "–"
        hy = r["hybrid_rank"] or "–"
        src = ", ".join(r["hybrid_sources"]) or "–"
        lines.append(f"| {r['id']} | {q} | {ft} | {hy} | {src} |")
    modes = {r["hybrid_mode"] for r in result["rows"]}
    if modes != {"hybrid"}:
        warns = sorted({r.get("hybrid_warning", "") for r in result["rows"]} - {""})
        lines += ["", f"Achtung: hybrid_search lief nicht immer hybrid (mode: {', '.join(sorted(modes))})."]
        lines += [f"Grund: {w}" for w in warns]
    return "\n".join(lines) + "\n"
