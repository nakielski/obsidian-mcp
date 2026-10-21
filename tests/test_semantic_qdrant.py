"""Semantic index against a real Qdrant (in-process local mode) and a
deterministic fake embedder. Skipped when the 'semantic' extra is missing."""
import asyncio
import hashlib
import json
import math
import pathlib
import re
import sys

import pytest

pytest.importorskip("qdrant_client")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qdrant_client import QdrantClient

from obsidian_mcp.hybrid import hybrid_query
from obsidian_mcp.semantic_index import EmbeddingError, SemanticConfig, SemanticIndex
from obsidian_mcp.server import mcp, register_semantic_tools, set_vault
from obsidian_mcp.vault import Vault

DIM = 64
SYNONYMS = {"fahrzeug": "auto", "pkw": "auto", "wagen": "auto", "kfz": "auto",
            "geburtstag": "feier", "party": "feier"}


class FakeEmbedder:
    """Bag-of-words vectors; synonyms share a bucket, so 'pkw' finds 'auto'."""

    def __init__(self):
        self.texts: list[str] = []
        self.fail = False

    def embed(self, texts):
        if self.fail:
            raise EmbeddingError("ollama down")
        self.texts.extend(texts)
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in re.findall(r"\w+", t.lower()):
                w = SYNONYMS.get(w, w)
                v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_SEMANTIC", "1")
    monkeypatch.setenv("OBSIDIAN_SEARCH_RESTRICTED", "05 People")
    monkeypatch.setenv("OBSIDIAN_SEMANTIC_EXCLUDE", "Private")
    vault = Vault(tmp_path)
    emb = FakeEmbedder()
    sx = SemanticIndex(vault.root, SemanticConfig.from_env(), client=QdrantClient(location=":memory:"),
                       embedder=emb, state_dir=tmp_path / ".obsidian-mcp-index")
    vault._semantic, vault._semantic_checked, vault.semantic_error = sx, True, ""
    return vault, sx, emb


def points(sx, path=None):
    if not sx._exists():
        return []
    pts, _ = sx.client.scroll(sx.collection, limit=1000, with_payload=True,
                              scroll_filter=sx._path_filter(path) if path else None)
    return pts


# ---- write path ------------------------------------------------------------


def test_create_indexes_chunks_with_policy_payload(env):
    v, sx, emb = env
    v.create("wiki/concepts/Auto.md", "---\nuid: cpt-auto\ntags: [mobil]\n---\n# Auto\n\nEin Auto fährt.")
    pts = points(sx, "wiki/concepts/Auto.md")
    assert len(pts) == 1
    p = pts[0].payload
    assert p["prefixes"] == ["wiki/", "wiki/concepts/"]
    assert p["layer"] == "wiki" and p["uid"] == "cpt-auto" and p["tags"] == ["mobil"]


def test_edit_reembeds_only_changed_section(env):
    v, sx, emb = env
    body = "# Plan\n\n## Eins\n\n" + "Erster Abschnitt mit genug Text für einen Chunk. " * 3 + \
           "\n\n## Zwei\n\n" + "Zweiter Abschnitt mit genug Text für einen Chunk. " * 3
    v.create("Notes/Plan.md", body)
    assert len(points(sx, "Notes/Plan.md")) == 2
    emb.texts.clear()
    v.update("Notes/Plan.md", body.replace("Zweiter Abschnitt", "Geänderter Abschnitt"))
    assert len(emb.texts) == 1 and "Geänderter" in emb.texts[0]
    assert len(points(sx, "Notes/Plan.md")) == 2  # stale chunk removed


def test_metadata_change_relabels_without_embedding(env):
    v, sx, emb = env
    v.create("Notes/Tagged.md", "# Tagged\n\nInhalt der getaggten Notiz bleibt gleich.")
    emb.texts.clear()
    v.add_tags("Notes/Tagged.md", ["neu"])
    assert emb.texts == []
    assert points(sx, "Notes/Tagged.md")[0].payload["tags"] == ["neu"]


def test_delete_removes_points(env):
    v, sx, emb = env
    v.create("Notes/Weg.md", "# Weg\n\nGleich wieder gelöscht.")
    v.delete("Notes/Weg.md")
    assert points(sx, "Notes/Weg.md") == []


def test_archive_moves_vectors_without_reembedding(env):
    v, sx, emb = env
    v.create("Inbox/Alt.md", "# Alt\n\nArchivierter Inhalt über Autos.")
    emb.texts.clear()
    v.archive("Inbox/Alt.md")
    assert emb.texts == []
    assert points(sx, "Inbox/Alt.md") == []
    moved = points(sx, "06 Archive/Alt.md")
    assert len(moved) == 1 and moved[0].payload["prefixes"] == ["06 archive/"]
    # full-text index follows the move as well
    assert [h["path"] for h in v._init_index().search("archivierter")] == ["06 Archive/Alt.md"]


def test_excluded_folder_is_never_embedded(env):
    v, sx, emb = env
    v.create("Private/Diary.md", "# Diary\n\nGeheimes Tagebuch.")
    assert points(sx, "Private/Diary.md") == []
    assert not any("Tagebuch" in t for t in emb.texts)


# ---- read path ---------------------------------------------------------------


def test_semantic_finds_synonym_fulltext_misses(env):
    v, sx, emb = env
    v.create("Notes/Fuhrpark.md", "# Fuhrpark\n\nUnser Auto braucht neue Reifen.")
    v.create("Notes/Garten.md", "# Garten\n\nTomaten und Basilikum gießen.")
    out = hybrid_query(v, "pkw reifen")
    assert out["mode"] == "hybrid" and out["warning"] == ""
    top = out["results"][0]
    assert top["path"] == "Notes/Fuhrpark.md"
    assert "semantic" in top["sources"]
    assert top["snippet"].startswith("Unser Auto")
    # the synonym alone is invisible to full text
    assert v._init_index().search("pkw", any_term=True) == []
    assert hybrid_query(v, "pkw")["results"][0]["sources"] == ["semantic"]


def test_restricted_hidden_unless_requested(env):
    v, sx, emb = env
    v.create("05 People/Ewa.md", "# Ewa\n\nGeburtstag im Mai, Party planen.")
    v.create("Notes/Feier.md", "# Feier\n\nFeier im Büro.")
    paths = [r["path"] for r in hybrid_query(v, "geburtstag party")["results"]]
    assert paths == ["Notes/Feier.md"]
    paths = {r["path"] for r in hybrid_query(v, "geburtstag party", include_restricted=True)["results"]}
    assert paths == {"Notes/Feier.md", "05 People/Ewa.md"}


def test_restricted_filter_runs_inside_qdrant(env):
    """Many restricted notes closer to the query must not crowd out the allowed one."""
    v, sx, emb = env
    for i in range(30):
        v.create(f"05 People/P{i}.md", f"# P{i}\n\nfeier feier feier party geburtstag {i}")
    v.create("Notes/Weit.md", "# Weit\n\nEine kleine Feier, sonst ganz anderes Thema hier.")
    hits = sx.search("feier party geburtstag", limit=1)
    assert [h["path"] for h in hits] == ["Notes/Weit.md"]


def test_folder_scope(env):
    v, sx, emb = env
    v.create("wiki/concepts/Auto.md", "---\nuid: c1\n---\n# Auto\n\nAuto im Wiki.")
    v.create("Notes/Auto.md", "# Auto\n\nAuto in den Notizen.")
    out = hybrid_query(v, "fahrzeug", folder="wiki")
    assert [r["path"] for r in out["results"]] == ["wiki/concepts/Auto.md"]


# ---- failure and catch-up -------------------------------------------------


def test_backend_failure_never_breaks_writes(env):
    v, sx, emb = env
    emb.fail = True
    note = v.create("Notes/Offline.md", "# Offline\n\nGeschrieben während Ollama weg war.")
    assert note.path == "Notes/Offline.md" and pathlib.Path(v.root, note.path).exists()
    assert sx.pending.all() == ["Notes/Offline.md"]
    assert sx.backend_down()
    # back-off: the next write does not even try the embedder
    emb.fail = False
    v.create("Notes/Offline2.md", "# Offline2\n\nAuch dieser wartet.")
    assert emb.texts == []
    assert sorted(sx.pending.all()) == ["Notes/Offline.md", "Notes/Offline2.md"]
    # search degrades to full text while paused
    out = hybrid_query(v, "ollama")
    assert out["mode"] == "fulltext" and "paused" in out["warning"]
    # catch-up after recovery
    sx._down_until = 0
    stats = sx.sync(v.list_notes())
    assert stats["complete"] and sx.pending.all() == []
    assert points(sx, "Notes/Offline.md") and points(sx, "Notes/Offline2.md")


def test_sync_catches_out_of_band_changes(env):
    v, sx, emb = env
    v.create("Notes/A.md", "# A\n\nErster Stand der Notiz A.")
    v.create("Notes/B.md", "# B\n\nNotiz B wird extern gelöscht.")
    root = pathlib.Path(v.root)
    (root / "Notes/A.md").write_text("# A\n\nExtern geänderter Stand.", encoding="utf-8")
    (root / "Notes/B.md").unlink()
    (root / "Notes/C.md").write_text("# C\n\nExtern angelegt.", encoding="utf-8")
    emb.texts.clear()
    stats = sx.sync(v.list_notes())
    assert stats["changed"] == 2 and stats["removed_points"] == 1 and stats["complete"]
    assert points(sx, "Notes/B.md") == []
    assert "Extern geänderter" in points(sx, "Notes/A.md")[0].payload["text"]
    emb.texts.clear()
    again = sx.sync(v.list_notes())
    assert again["changed"] == 0 and emb.texts == []  # idempotent, no embedding


def test_sync_time_budget_reports_remaining(env):
    v, sx, emb = env
    for i in range(5):
        v.create(f"Notes/N{i}.md", f"# N{i}\n\nText {i}")
    stats = sx.sync(v.list_notes(), max_seconds=0)
    assert stats["remaining"] == 5 and not stats["complete"]


def test_sync_stops_early_when_backend_is_down(env):
    v, sx, emb = env
    for i in range(10):
        (pathlib.Path(v.root) / f"N{i}.md").write_text(f"# N{i}\n\nExtern {i}", encoding="utf-8")
    calls = []

    def down(texts):
        calls.append(texts)
        raise EmbeddingError("connection refused")

    emb.embed = down
    stats = sx.sync(v.list_notes())
    # three notes, then one probe that confirms the outage
    assert len(calls) == 4 and calls[-1] == ["ping"]
    assert stats["failed"] == 3 and stats["remaining"] == 7
    assert not stats["complete"] and sx.backend_down()
    assert "connection refused" in stats["last_error"]


def test_sync_continues_past_note_failures_when_backend_is_healthy(env):
    v, sx, emb = env
    root = pathlib.Path(v.root)
    for i in range(4):
        (root / f"Bad{i}.md").write_text(f"# Bad{i}\n\nKAPUTT {i}", encoding="utf-8")
    for i in range(3):
        (root / f"Good{i}.md").write_text(f"# Good{i}\n\nGut {i}", encoding="utf-8")
    real = emb.embed

    def picky(texts):
        if any("KAPUTT" in t for t in texts):
            raise EmbeddingError("timed out for a single input")
        return real(texts)

    emb.embed = picky
    stats = sx.sync(v.list_notes())
    assert stats["failed"] == 4 and stats["changed"] == 3 and stats["remaining"] == 0
    assert not stats["complete"] and not sx.backend_down()
    assert "single input" in stats["last_error"]
    assert all(points(sx, f"Good{i}.md") for i in range(3))


def test_successful_sync_resumes_inline_indexing(env):
    v, sx, emb = env
    emb.fail = True
    v.create("Notes/P.md", "# P\n\nWartet.")
    assert sx.backend_down()
    emb.fail = False
    assert sx.sync(v.list_notes())["complete"]
    assert not sx.backend_down()
    v.create("Notes/Q.md", "# Q\n\nWieder direkt indexiert.")
    assert points(sx, "Notes/Q.md") and sx.pending.all() == []


def test_long_new_note_is_deferred_to_sync(env):
    v, sx, emb = env
    sx.cfg = SemanticConfig(**{**sx.cfg.__dict__, "inline_max_chunks": 2})
    body = "\n\n".join(f"## Abschnitt {i}\n\n" + f"Inhalt {i} " * 20 for i in range(5))
    v.create("Notes/Lang.md", "# Lang\n\n" + body)
    assert emb.texts == [] and sx.pending.all() == ["Notes/Lang.md"]
    assert sx.sync(v.list_notes())["complete"]
    assert len(points(sx, "Notes/Lang.md")) == 5 and sx.pending.all() == []
    # small edits to an indexed long note stay inline
    v.update("Notes/Lang.md", ("# Lang\n\n" + body).replace("Inhalt 4", "Neuer Inhalt 4"))
    assert sx.pending.all() == []


def test_concurrent_collection_creation_is_tolerated(env, monkeypatch):
    v, sx, emb = env
    v.create("Notes/First.md", "# First\n\nLegt die Collection an.")
    sx._collection_ready = False
    real_exists = sx.client.collection_exists
    calls = {"n": 0}

    def exists(name):
        calls["n"] += 1
        return False if calls["n"] == 1 else real_exists(name)  # 'not there yet' at check time

    monkeypatch.setattr(sx.client, "collection_exists", exists)
    sx._ensure_collection(DIM)  # create_collection fails: another process won the race
    assert sx._collection_ready and calls["n"] == 2


def test_dimension_mismatch_is_explained(env):
    v, sx, emb = env
    v.create("Notes/X.md", "# X\n\nIrgendwas.")
    sx._collection_ready = False
    with pytest.raises(RuntimeError, match="separate collection"):
        sx._ensure_collection(DIM + 1)


# ---- MCP tools -----------------------------------------------------------


def _call(name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    return json.loads(result[0][0].text)


def test_mcp_tools(env):
    v, sx, emb = env
    register_semantic_tools()
    set_vault(v)
    v.create("Notes/Fuhrpark.md", "# Fuhrpark\n\nUnser Auto braucht neue Reifen.")
    data = _call("hybrid_search", {"query": "kfz", "limit": 5})
    assert data["mode"] == "hybrid" and data["results"][0]["path"] == "Notes/Fuhrpark.md"
    status = _call("semantic_sync", {"status_only": True})
    assert status["ran"] is False and status["points"] >= 1 and status["reachable"]
    run = _call("semantic_sync", {"max_seconds": 30})
    assert run["ran"] and run["complete"] and run["changed"] == 0


def test_recovery_clears_last_error(env):
    v, sx, emb = env
    emb.fail = True
    v.create("Notes/E.md", "# E\n\nFehlerfall.")
    assert sx.last_error
    emb.fail, sx._down_until = False, 0
    sx.sync(v.list_notes())
    assert sx.last_error == "" and sx.status()["last_error"] == ""


def test_min_score_drops_weak_neighbours(env, monkeypatch):
    v, sx, emb = env
    v.create("Notes/Fuhrpark.md", "# Fuhrpark\n\nUnser Auto braucht neue Reifen.")
    v.create("Notes/Garten.md", "# Garten\n\nTomaten und Basilikum gießen.")
    assert {h["path"] for h in sx.search("pkw reifen")} == {"Notes/Fuhrpark.md", "Notes/Garten.md"}
    sx.cfg = SemanticConfig(**{**sx.cfg.__dict__, "min_score": 0.3})
    assert [h["path"] for h in sx.search("pkw reifen")] == ["Notes/Fuhrpark.md"]


# ---- evaluation ----------------------------------------------------------


def test_eval_compares_fulltext_and_hybrid(env, tmp_path):
    from obsidian_mcp.evaluation import load_questions, run_eval, to_markdown

    v, sx, emb = env
    v.create("Notes/Auto.md", "# Auto\n\nMein Auto steht in der Garage.")
    v.create("Notes/Feier.md", "# Feier\n\nDie Feier war am Samstag.")
    v.create("Notes/Entwurf v0.1 2026-10-01.md", "# Entwurf\n\nGeburtstag planen.")
    v.create("Notes/Entwurf v0.2 2026-10-02.md", "# Entwurf\n\nGeburtstag planen, zweite Fassung.")
    v.create("05 People/Anna.md", "# Anna\n\nAnna hat einen Wagen.")
    qfile = tmp_path / "Fragen.md"
    qfile.write_text(
        "# Fragenset\n\nText davor.\n\n```yaml\n"
        "- frage: Auto Garage\n  erwartet: [Notes/Auto]\n"
        "- frage: wo steht der pkw\n  erwartet: [Auto.md]\n"
        "- question: Geburtstag party\n  expect: Entwurf v0.1\n"
        "- frage: wer hat einen wagen\n  erwartet: [05 People/]\n"
        "- frage: \n  erwartet: []\n"
        "```\n", encoding="utf-8")
    qs = load_questions(qfile)
    assert [q["id"] for q in qs] == [1, 2, 3, 4]
    res = run_eval(v, qs, k=5)
    rows = {r["id"]: r for r in res["rows"]}
    assert rows[1]["fulltext_rank"] == 1 and rows[1]["hybrid_rank"] == 1
    assert rows[2]["fulltext_rank"] is None and rows[2]["hybrid_rank"] is not None  # synonym only
    assert rows[3]["hybrid_rank"] is not None  # found via the folded variant
    assert rows[4]["fulltext_rank"] is None and rows[4]["hybrid_rank"] is None  # restricted stays hidden
    assert res["summary"]["only_hybrid"] >= 1
    md = to_markdown(res)
    assert "| Treffer unter den ersten 5 |" in md and "wo steht der pkw" in md
    assert run_eval(v, qs[3:], k=5, include_restricted=True)["rows"][0]["hybrid_rank"] is not None


def test_load_questions_rejects_incomplete_entries(tmp_path):
    from obsidian_mcp.evaluation import load_questions

    f = tmp_path / "q.yaml"
    f.write_text("- frage: ohne Erwartung\n", encoding="utf-8")
    with pytest.raises(ValueError, match="erwartet"):
        load_questions(f)


def test_eval_report_names_the_fallback_reason(env, tmp_path):
    from obsidian_mcp.evaluation import run_eval, to_markdown

    v, sx, emb = env
    v.create("Notes/Auto.md", "# Auto\n\nMein Auto.")
    emb.fail = True
    v.create("Notes/B.md", "# B\n\nB.")  # write fails -> backend marked down
    md = to_markdown(run_eval(v, [{"id": 1, "question": "Auto", "expect": ["Auto"], "note": ""}], k=5))
    assert "lief nicht immer hybrid" in md and "Grund: semantic search paused" in md


def test_eval_never_counts_the_question_note(env):
    from obsidian_mcp.evaluation import run_eval

    v, sx, emb = env
    v.create("Notes/Antwort.md", "# Antwort\n\nDie Garage steht hinten.")
    v.create("Eval/Fragen.md", "# Fragen\n\n- frage: Garage hinten\n  erwartet: [Notes/Antwort, Eval/]\n")
    q = [{"id": 1, "question": "Garage hinten", "expect": ["Eval/", "Notes/Antwort"], "note": ""}]
    with_note = run_eval(v, q, k=5)["rows"][0]
    without = run_eval(v, q, k=5, exclude_paths=("Eval/Fragen.md",))["rows"][0]
    assert any(p.startswith("Eval/") for p in with_note["hybrid_top"])
    assert not any(p.startswith("Eval/") for p in without["hybrid_top"] + without["fulltext_top"])
    assert without["hybrid_rank"] == 1


def test_hybrid_ranks_notes_with_all_words_first_and_marks_demoted(env, monkeypatch):
    v, sx, emb = env
    monkeypatch.setenv("OBSIDIAN_SEARCH_DEMOTE", "Vendor/*/")
    for i in range(4):
        v.create(f"Notes/Netz {i}.md", f"# Netz {i}\n\nTailscale Tailscale Tailscale Notiz {i}.")
    v.create("Notes/Phase 0.md", "# Phase 0\n\nDie Tailscale ACL regelt den Zugriff.")
    v.create("Vendor/tool/ACL.md", "# ACL\n\nTailscale ACL reference, Tailscale ACL syntax.")
    res = hybrid_query(v, "Tailscale ACL", limit=6)
    paths = [r["path"] for r in res["results"]]
    assert paths[0] == "Notes/Phase 0.md"
    first = res["results"][0]
    assert first["all_terms_rank"] is not None and first["demoted"] is False
    vendor = next(r for r in res["results"] if r["path"] == "Vendor/tool/ACL.md")
    assert vendor["demoted"] is True and vendor["all_terms_rank"] is not None
    one_word = hybrid_query(v, "Tailscale", limit=6)["results"]  # single word: no AND list
    assert all(r["all_terms_rank"] is None for r in one_word)


def test_eval_dump_and_offline_replay(env, tmp_path, monkeypatch):
    from obsidian_mcp.evaluation import replay_eval, run_eval, to_markdown

    v, sx, emb = env
    monkeypatch.setenv("OBSIDIAN_SEARCH_DEMOTE", "")
    v.create("Mirror/tool/Garage.md", "# Garage\n\nGarage Tor Garage Tor Anleitung.")
    v.create("Notes/Garage.md", "# Unsere Garage\n\nDas Garage Tor klemmt.")
    q = [{"id": 1, "question": "Garage Tor", "expect": ["Notes/Garage"], "note": ""}]
    res = run_eval(v, q, k=5, dump=True)
    lists = res["rows"][0]["lists"]
    assert set(lists) == {"fulltext", "all_terms", "semantic"} and lists["all_terms"]
    assert set(lists["fulltext"][0]) == {"path", "title", "uid"}
    same = replay_eval(json.loads(json.dumps(res)))
    assert same["rows"][0]["hybrid_rank"] == res["rows"][0]["hybrid_rank"]
    assert same["rows"][0]["fulltext_rank"] == res["rows"][0]["fulltext_rank"]
    monkeypatch.setenv("OBSIDIAN_SEARCH_DEMOTE", "Mirror/")
    monkeypatch.setenv("OBSIDIAN_SEARCH_DEMOTE_WEIGHT", "0")
    demoted = replay_eval(res)
    assert demoted["rows"][0]["hybrid_rank"] == 1
    assert demoted["settings"]["demote"] == ["Mirror/"]
    md = to_markdown(demoted)
    assert "Nachgerechnet mit: zurückgestuft `Mirror/` (Faktor 0.0)" in md
    with pytest.raises(ValueError, match="dump"):
        replay_eval(run_eval(v, q, k=5))


def test_eval_cli_dump_then_replay(env, tmp_path, monkeypatch, capsys):
    from obsidian_mcp.semantic_index import cli

    v, sx, emb = env
    v.create("Notes/Garage.md", "# Garage\n\nDas Garage Tor klemmt.")
    qfile = tmp_path / "q.yaml"
    qfile.write_text("- frage: Garage Tor\n  erwartet: [Notes/Garage]\n", encoding="utf-8")
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("OBSIDIAN_VAULT_ROOT", str(v.root))
    import obsidian_mcp.vault as vault_mod

    monkeypatch.setattr(vault_mod.Vault, "_init_semantic", lambda self: sx)
    assert cli(["eval", str(qfile), "--dump", str(dump)]) == 0
    out = capsys.readouterr().out
    assert "Einstellungen: zurückgestuft" in out and dump.is_file()
    assert "lists" in json.loads(dump.read_text(encoding="utf-8"))["rows"][0]
    monkeypatch.delenv("OBSIDIAN_VAULT_ROOT")  # replay needs no vault
    assert cli(["eval", str(dump), "--replay", "--json"]) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["rows"][0]["hybrid_rank"] == 1 and "replay" in replayed
    assert cli(["eval", str(qfile), "--replay"]) == 2
