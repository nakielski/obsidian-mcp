"""Semantic search pieces that need no Qdrant: chunking, fusion, embedder,
config, pending store, and the guarantee that the server is unchanged
without the feature (no tools, no qdrant import, writes unaffected)."""
import json
import os
import pathlib
import subprocess
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import semantic_index as si
from obsidian_mcp.hybrid import hybrid_query, make_snippet, path_matcher, rrf_fuse, variant_key
from obsidian_mcp.semantic_index import (
    Embedder,
    EmbeddingError,
    PendingStore,
    SemanticConfig,
    chunk_note,
    folder_prefixes,
    is_under,
    normalize_prefix,
    point_id,
)
from obsidian_mcp.vault import Vault

SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")


# ---- prefixes and config ----------------------------------------------


@pytest.mark.parametrize("raw", ["05 People", "/05 People/", "05 People\\", " 05 PEOPLE "])
def test_normalize_prefix(raw):
    assert normalize_prefix(raw) == "05 people/"


def test_normalize_prefix_empty():
    assert normalize_prefix(" / ") == ""


def test_folder_prefixes():
    assert folder_prefixes("05 People/Ewa/Note.md") == ["05 people/", "05 people/ewa/"]
    assert folder_prefixes("Root.md") == []


def test_is_under_case_insensitive():
    assert is_under("05 PEOPLE/x.md", ("05 people/",))
    assert not is_under("05 Peoplex/x.md", ("05 people/",))


def test_config_defaults(monkeypatch):
    for k in list(os.environ):
        if k.startswith("OBSIDIAN_EMBED") or k.startswith("OBSIDIAN_QDRANT") or k.startswith("OBSIDIAN_SEM"):
            monkeypatch.delenv(k, raising=False)
    cfg = SemanticConfig.from_env()
    assert cfg.collection == "obsidian_mcp_bge_m3"
    assert cfg.embed_url == "http://localhost:11434"
    assert cfg.restricted == () and cfg.exclude == ()


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_EMBED_MODEL", "qwen3-embedding:0.6b")
    monkeypatch.setenv("OBSIDIAN_SEARCH_RESTRICTED", "05 People, Private/Health")
    monkeypatch.setenv("OBSIDIAN_SEMANTIC_EXCLUDE", ".trash")
    monkeypatch.setenv("OBSIDIAN_EMBED_API", "openai")
    cfg = SemanticConfig.from_env()
    assert cfg.collection == "obsidian_mcp_qwen3_embedding_0_6b"
    assert cfg.restricted == ("05 people/", "private/health/")
    assert cfg.exclude == (".trash/",)
    assert cfg.embed_url.endswith("/v1")


def test_config_ranking_settings(monkeypatch):
    for k in ("OBSIDIAN_SEARCH_DEMOTE", "OBSIDIAN_SEARCH_DEMOTE_WEIGHT", "OBSIDIAN_HYBRID_CANDIDATES"):
        monkeypatch.delenv(k, raising=False)
    cfg = SemanticConfig.from_env()
    assert cfg.demote == ("**/node_modules/",) and cfg.demote_weight == 0.5 and cfg.candidates == 30
    monkeypatch.setenv("OBSIDIAN_SEARCH_DEMOTE", " raw/repos/*/*/ , Vendor ")
    monkeypatch.setenv("OBSIDIAN_SEARCH_DEMOTE_WEIGHT", "3")
    monkeypatch.setenv("OBSIDIAN_HYBRID_CANDIDATES", "60")
    cfg = SemanticConfig.from_env()
    assert cfg.demote == ("raw/repos/*/*/", "Vendor") and cfg.demote_weight == 1.0 and cfg.candidates == 60
    monkeypatch.setenv("OBSIDIAN_SEARCH_DEMOTE", "")  # empty switches it off, default included
    assert SemanticConfig.from_env().demote == ()


def test_config_rejects_unknown_api(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_EMBED_API", "cohere")
    with pytest.raises(ValueError):
        SemanticConfig.from_env()


# ---- chunking ------------------------------------------------------------


NOTE = """Intro paragraph that is long enough to stand on its own as a chunk of text here.

## Bauplan

Hybrid search combines full text and vectors in one ranked list for the agent.

```python
# not a heading inside a fence
x = 1
```

### Tests

Policy tests make sure restricted folders never show up without explicit consent.

## Status

done
"""


def test_chunks_follow_headings_and_ignore_fences():
    chunks = chunk_note("Plan", NOTE)
    headings = [c.heading for c in chunks]
    assert headings == ["", "Bauplan", "Bauplan > Tests"]
    assert "# not a heading inside a fence" in chunks[1].text
    # the tiny 'Status: done' section is merged into the previous chunk
    assert "Status: done" in chunks[2].text


def test_embed_text_carries_title_and_heading():
    c = chunk_note("Plan", NOTE)[1]
    assert c.embed_text.startswith("Plan\nBauplan\n\n")


def test_long_sections_are_split():
    body = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(20))
    chunks = chunk_note("T", body, max_chars=1000)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1000 for c in chunks)


def test_oversize_paragraph_hard_cut():
    chunks = chunk_note("T", "y" * 5000, max_chars=1000)
    assert [len(c.text) for c in chunks] == [1000] * 5


def test_editing_one_section_changes_one_hash():
    before = {c.heading: c.hash for c in chunk_note("Plan", NOTE)}
    edited = NOTE.replace("Policy tests make sure", "Policy tests now make sure")
    after = {c.heading: c.hash for c in chunk_note("Plan", edited)}
    changed = [h for h in before if before[h] != after[h]]
    assert changed == ["Bauplan > Tests"]


def test_point_id_is_stable_and_path_scoped():
    assert point_id("a.md", "h") == point_id("a.md", "h")
    assert point_id("a.md", "h") != point_id("b.md", "h")


def test_empty_note_has_no_chunks():
    assert chunk_note("T", "   \n\n") == []


# ---- fusion --------------------------------------------------------------


def test_rrf_prefers_notes_found_by_both():
    ft = [{"path": "a.md", "title": "A"}, {"path": "b.md", "title": "B"}]
    sem = [{"path": "c.md", "title": "C", "heading": "H", "text": "t"}, {"path": "b.md", "title": "B"}]
    fused = rrf_fuse(ft, sem, limit=10)
    assert fused[0]["path"] == "b.md"
    assert fused[0]["sources"] == ["fulltext", "semantic"]
    assert (fused[0]["fulltext_rank"], fused[0]["semantic_rank"]) == (2, 2)
    assert {f["path"] for f in fused} == {"a.md", "b.md", "c.md"}


def test_rrf_limit_and_semantic_heading():
    sem = [{"path": f"{i}.md", "heading": f"h{i}", "text": "t"} for i in range(5)]
    fused = rrf_fuse([], sem, limit=2)
    assert [f["path"] for f in fused] == ["0.md", "1.md"]
    assert fused[0]["heading"] == "h0"


def test_variant_key_groups_versions_and_copies_only():
    k = variant_key("04 Resources/LinkedIn/MCP Artikel 2/MCP Artikel 2 Entwurf v0.6 2026-10-19.md")
    assert k == "mcp artikel 2 entwurf"
    assert variant_key("06 Archive/MCP Artikel 2 Entwurf v0.3 (2026-10-18).md") == k
    assert variant_key("00 Inbox/MCP Artikel 2 Entwurf v0.7 2026-10-20.md") == k
    assert (variant_key("raw/conversations/2026-05-19 Hermes Agent Update Anleitung.md")
            == variant_key("02 Areas/KI/2026-05-19 Hermes Agent Update Anleitung.md"))
    # generic names never merge unrelated notes
    for path in ("raw/repos/a/SKILL.md", "raw/repos/a/docs/index.md", "Daily/2026-10-20.md", "x/v1.md"):
        assert variant_key(path) == path
    assert variant_key("a/Second Brain Artikel.md") != variant_key("a/Second Brain Artikel Humanized.md")


def test_fusion_folds_variants_and_prefers_live_copy():
    ft = [{"path": "06 Archive/Plan v0.2 Notiz.md"}, {"path": "Projekte/Plan v0.3 Notiz.md"},
          {"path": "Other.md"}]
    fused = rrf_fuse(ft, [], limit=10)
    assert [f["path"] for f in fused] == ["Projekte/Plan v0.3 Notiz.md", "Other.md"]
    assert fused[0]["variants"] == ["06 Archive/Plan v0.2 Notiz.md"]
    assert fused[0]["score"] == round(1 / 61, 6)  # best member's score, copies add nothing


def test_fusion_newest_live_variant_represents_group():
    ft = [{"path": "Projekte/Artikel Entwurf v0.6 2026-10-19.md"},
          {"path": "06 Archive/Artikel Entwurf v0.7 2026-10-20.md"},
          {"path": "Projekte/Artikel Entwurf v0.7 2026-10-20.md"},
          {"path": "Projekte/Artikel Entwurf v0.10 2026-10-22.md"}]
    fused = rrf_fuse(ft, [], limit=5)
    assert len(fused) == 1
    assert fused[0]["path"] == "Projekte/Artikel Entwurf v0.10 2026-10-22.md"  # 0.10 > 0.7
    assert fused[0]["score"] == round(1 / 61, 6)
    assert len(fused[0]["variants"]) == 3


def test_fusion_reserves_top_hits_of_each_retriever():
    # 12 notes found by both lists (loose OR full text) vs. semantic-only top hits
    both = [{"path": f"both{i}.md"} for i in range(12)]
    sem_only = [{"path": f"sem{i}.md"} for i in range(3)]
    ft = list(both)
    sem = sem_only + both
    plain = rrf_fuse(ft, sem, limit=10, reserve=0)
    assert not any(f["path"].startswith("sem") for f in plain)  # the RRF crowding effect
    fused = rrf_fuse(ft, sem, limit=10)
    paths = [f["path"] for f in fused]
    assert len(paths) == 10 and {"sem0.md", "sem1.md", "sem2.md"} <= set(paths)
    scores = [f["score"] for f in fused]
    assert scores == sorted(scores, reverse=True)


def test_fusion_reserve_never_exceeds_limit():
    sem = [{"path": f"s{i}.md"} for i in range(5)]
    ft = [{"path": f"f{i}.md"} for i in range(5)]
    assert len(rrf_fuse(ft, sem, limit=1)) == 1
    two = rrf_fuse(ft, sem, limit=2)
    assert {f["path"] for f in two} == {"s0.md", "f0.md"}


@pytest.mark.parametrize("pattern,yes,no", [
    ("05 People", ["05 People/Anna.md", "05 people/x/y.md"], ["05 Peoplex/a.md", "x/05 People/a.md"]),
    ("raw/repos/*/*/", ["raw/repos/openclaw/docs/a.md", "raw/repos/x/y/z/a.md"],
     ["raw/repos/hermes-agent/Eigene Notiz.md", "raw/conversations/a/b.md"]),
    ("**/node_modules/", ["node_modules/a.md", "06 Archive/b/node_modules/zmq/README.md"],
     ["06 Archive/node_modules.md", "a/node_modules_x/b.md"]),
    ("raw/repos/*/docs", ["raw/repos/ollama/docs/api.md"], ["raw/repos/ollama/README.md"]),
    ("04 Resources/Fragen*.md", ["04 Resources/Fragenset.md"], ["04 Resources/x/Fragenset.md"]),
    ("a\\b\\", ["a/b/c.md"], ["a/bc.md"]),
])
def test_path_matcher_prefixes_and_globs(pattern, yes, no):
    m = path_matcher([pattern])
    assert all(m.match(p.casefold()) for p in yes)
    assert not any(m.match(p.casefold()) for p in no)


def test_path_matcher_empty():
    assert path_matcher([]) is None and path_matcher([" ", ""]) is None


def test_fusion_demotes_without_hiding():
    mirror = "raw/repos/tool/docs/setup.md"
    ft = [{"path": mirror}, {"path": "Notes/Eigenes Setup.md"}, {"path": "Notes/C.md"}]
    sem = [{"path": mirror}, {"path": "Notes/Eigenes Setup.md"}]
    plain = rrf_fuse(ft, sem, limit=10)
    assert plain[0]["path"] == mirror and plain[0]["demoted"] is False
    fused = rrf_fuse(ft, sem, limit=10, demote=("raw/repos/*/*/",))
    assert [f["path"] for f in fused][:2] == ["Notes/Eigenes Setup.md", mirror]
    assert fused[1]["demoted"] is True and fused[1]["score"] == round(0.5 * 2 / 61, 6)
    gone = rrf_fuse(ft, sem, limit=10, demote=("raw/repos/",), demote_weight=0)
    assert gone[-1]["path"] == mirror and gone[-1]["score"] == 0  # still listed, last


def test_fusion_demoted_hits_take_no_reserved_slot():
    both = [{"path": f"both{i}.md"} for i in range(12)]
    sem = [{"path": "vendor/x/a.md"}, {"path": "sem1.md"}] + both
    fused = rrf_fuse(both, sem, limit=10, demote=("vendor/",))
    paths = [f["path"] for f in fused]
    assert "sem1.md" in paths and "vendor/x/a.md" not in paths


def test_fusion_all_terms_list_lifts_exact_matches():
    # OR full text: one note repeats 'tailscale' often; the note with both words is 6th
    ft = [{"path": f"tailscale{i}.md"} for i in range(5)] + [{"path": "Phase 0.md"}]
    sem = list(ft)
    before = [f["path"] for f in rrf_fuse(ft, sem, limit=10)]
    after_hits = rrf_fuse(ft, sem, limit=10, all_terms=[{"path": "Phase 0.md"}])
    after = [f["path"] for f in after_hits]
    assert before.index("Phase 0.md") > 0 and after[0] == "Phase 0.md"
    top = after_hits[0]
    assert top["all_terms_rank"] == 1 and top["fulltext_rank"] == 6
    assert top["sources"] == ["fulltext", "semantic"]


def test_fusion_all_terms_hit_beyond_or_list_counts_as_fulltext():
    fused = rrf_fuse([{"path": "a.md"}], [], limit=5, all_terms=[{"path": "b.md", "title": "B"}])
    b = next(f for f in fused if f["path"] == "b.md")
    assert b["sources"] == ["fulltext"] and b["fulltext_rank"] is None and b["title"] == "B"


def test_fusion_fulltext_reserve_prefers_all_terms():
    both = [{"path": f"both{i}.md"} for i in range(12)]
    ft = list(both) + [{"path": "exact.md"}]
    fused = rrf_fuse(ft, both, limit=10, reserve=1, all_terms=[{"path": "exact.md"}])
    assert "exact.md" in [f["path"] for f in fused]


def test_snippet_strips_markup_and_truncates():
    s = make_snippet("<b>Hallo</b>\n\n  Welt " + "z" * 400, max_chars=50)
    assert s.startswith("Hallo Welt") and len(s) == 50 and s.endswith("…")


# ---- embedder (HTTP mocked) ---------------------------------------------


def _ollama_transport(calls):
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append((str(request.url), body))
        return httpx.Response(200, json={"embeddings": [[float(len(t)), 1.0] for t in body["input"]]})
    return httpx.MockTransport(handler)


def test_ollama_embedder_batches():
    calls = []
    cfg = SemanticConfig(embed_batch=2, embed_model="bge-m3")
    vecs = Embedder(cfg, transport=_ollama_transport(calls)).embed(["a", "bb", "ccc", "dddd", "e"])
    assert [v[0] for v in vecs] == [1, 2, 3, 4, 1]
    assert len(calls) == 3
    assert calls[0][0] == "http://localhost:11434/api/embed"
    assert calls[0][1] == {"model": "bge-m3", "input": ["a", "bb"]}


def test_openai_embedder_orders_by_index_and_sends_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]})

    cfg = SemanticConfig(embed_api="openai", embed_url="https://llm.example/v1", embed_api_key="sk-test")
    vecs = Embedder(cfg, transport=httpx.MockTransport(handler)).embed(["x", "y"])
    assert vecs == [[1.0], [2.0]]
    assert seen == {"auth": "Bearer sk-test", "url": "https://llm.example/v1/embeddings"}


def test_embedder_splits_batch_on_timeout_and_keeps_order():
    sizes = []

    def handler(request):
        body = json.loads(request.content)
        sizes.append(len(body["input"]))
        if len(body["input"]) > 2:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"embeddings": [[float(len(t))] for t in body["input"]]})

    cfg = SemanticConfig(embed_batch=8)
    texts = ["a" * n for n in range(1, 8)]
    vecs = Embedder(cfg, transport=httpx.MockTransport(handler)).embed(texts)
    assert [v[0] for v in vecs] == [1, 2, 3, 4, 5, 6, 7]
    assert sizes == [7, 3, 1, 2, 4, 2, 2]  # 7 -> 3+4, 3 -> 1+2, 4 -> 2+2


def test_embedder_single_input_timeout_raises_with_hint():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(EmbeddingError, match="OBSIDIAN_EMBED_TIMEOUT"):
        Embedder(SemanticConfig(embed_batch=4), transport=httpx.MockTransport(handler)).embed(["x", "y"])


def test_embedder_connect_error_is_not_split():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EmbeddingError, match="failed"):
        Embedder(SemanticConfig(embed_batch=8), transport=httpx.MockTransport(handler)).embed(["a"] * 8)
    assert len(calls) == 1


def test_embedder_count_mismatch_raises():
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"embeddings": []}))
    with pytest.raises(EmbeddingError, match="pulled"):
        Embedder(SemanticConfig(), transport=t).embed(["x"])


def test_embedder_http_error_raises():
    t = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(EmbeddingError):
        Embedder(SemanticConfig(), transport=t).embed(["x"])


# ---- pending store -------------------------------------------------------


def test_pending_store_roundtrip(tmp_path):
    ps = PendingStore(tmp_path / "idx" / "semantic-pending.json")
    assert ps.all() == []
    ps.add("a.md")
    ps.add("b.md")
    ps.add("a.md")
    assert ps.all() == ["a.md", "b.md"]
    ps.discard("a.md", "zzz.md")
    assert ps.all() == ["b.md"]
    assert PendingStore(ps.path).all() == ["b.md"]  # persisted


def test_pending_discard_without_file_is_noop(tmp_path):
    ps = PendingStore(tmp_path / "missing.json")
    ps.discard("a.md")
    assert not ps.path.exists()


# ---- server unchanged without the feature --------------------------------


def _tool_names(env_extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OBSIDIAN_SEMANTIC")}
    env.update(env_extra)
    env["PYTHONPATH"] = SRC
    code = (
        "import json, sys; from obsidian_mcp.server import mcp; "
        "print(json.dumps({'tools': sorted(t.name for t in mcp._tool_manager.list_tools()), "
        "'qdrant_loaded': 'qdrant_client' in sys.modules}))"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_feature_off_keeps_tool_list_and_never_imports_qdrant():
    info = _tool_names({})
    assert "hybrid_search" not in info["tools"]
    assert "semantic_sync" not in info["tools"]
    assert "search_query" in info["tools"]
    assert info["qdrant_loaded"] is False


def test_feature_on_registers_tools():
    info = _tool_names({"OBSIDIAN_SEMANTIC": "1"})
    assert {"hybrid_search", "semantic_sync"} <= set(info["tools"])


def test_enabled_without_qdrant_degrades_to_fulltext(tmp_path, monkeypatch):
    """Flag on, extra missing: writes work, hybrid search answers from full text."""
    monkeypatch.setenv("OBSIDIAN_SEMANTIC", "1")
    monkeypatch.setattr(si, "QDRANT_AVAILABLE", False)
    v = Vault(tmp_path)
    v.create("Notes/Zebra.md", "# Zebra\n\nStreifen und Savanne.")
    assert v._init_semantic() is None
    assert "qdrant-client is not installed" in v.semantic_error
    out = hybrid_query(v, "savanne")
    assert out["mode"] == "fulltext"
    assert "obsidian-mcp[semantic]" in out["warning"]
    assert [r["path"] for r in out["results"]] == ["Notes/Zebra.md"]
    assert out["results"][0]["snippet"].startswith("# Zebra")


def test_fulltext_side_honours_restricted_and_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_SEARCH_RESTRICTED", "05 People")
    monkeypatch.delenv("OBSIDIAN_SEMANTIC", raising=False)
    v = Vault(tmp_path)
    v.create("05 People/Ewa.md", "# Ewa\n\nGeburtstag im Mai.")
    v.create("Notes/Plan.md", "# Plan\n\nGeburtstag planen.")
    out = hybrid_query(v, "geburtstag")
    assert [r["path"] for r in out["results"]] == ["Notes/Plan.md"]
    assert out["restricted_hidden"] is True
    out = hybrid_query(v, "geburtstag", include_restricted=True)
    assert {r["path"] for r in out["results"]} == {"Notes/Plan.md", "05 People/Ewa.md"}
    out = hybrid_query(v, "geburtstag", folder="05 People", include_restricted=True)
    assert [r["path"] for r in out["results"]] == ["05 People/Ewa.md"]


def test_natural_language_query_uses_or(tmp_path, monkeypatch):
    monkeypatch.delenv("OBSIDIAN_SEMANTIC", raising=False)
    v = Vault(tmp_path)
    v.create("Notes/Kill.md", "# Kill Switch\n\nDer Kill Switch stoppt alle Trades.")
    # AND semantics would need every word; hybrid uses OR ranking
    out = hybrid_query(v, "wie funktioniert der kill switch genau")
    assert out["results"] and out["results"][0]["path"] == "Notes/Kill.md"


def test_hybrid_query_limit_validation(tmp_path):
    with pytest.raises(ValueError):
        hybrid_query(Vault(tmp_path), "x", limit=0)
