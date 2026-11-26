"""Note dates, sorting by date and hiding replaced notes in the searches."""
import asyncio
import datetime
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import search_index
from obsidian_mcp.hybrid import hybrid_query, rrf_fuse, variant_key
from obsidian_mcp.search_index import (
    WHOOSH_AVAILABLE,
    VaultIndex,
    is_conflict_copy,
    is_hidden,
    note_date,
    parse_since,
)
from obsidian_mcp.server import mcp, set_vault
from obsidian_mcp.vault import Vault

pytestmark = pytest.mark.skipif(not WHOOSH_AVAILABLE, reason="whoosh3 not installed")

NOW = datetime.datetime(2026, 11, 26, 12, 0)
CLASH = "Projekt/Seite 03.11.2026 (# Name clash 2026-11-03 mz12c7C #).md"
CLASH2 = "Projekt/Seite 03.11.2026 (# Name clash 2026-11-03 mz12c7C #) (# Name clash 2026-11-03 m8wmhwC #).md"


def _call(tool: str, **args) -> dict:
    result = asyncio.run(mcp.call_tool(tool, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


@pytest.fixture(autouse=True)
def _plain_env(monkeypatch):
    for name in ("OBSIDIAN_SEARCH_HIDE_STATUS", "OBSIDIAN_SEARCH_HIDE_CONFLICTS",
                 "OBSIDIAN_SEMANTIC", "OBSIDIAN_SEARCH_RESTRICTED"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path)
    # unquoted date: YAML makes a datetime.date of it
    v.create("Projekt/Pruefbericht 03.11.2026.md",
             "---\nuid: n1\nstatus: active\nupdated: 2026-11-03\n---\n# Prüfbericht\n\nDocflow Prüfung.")
    v.create("Projekt/Seite 03.11.2026.md",
             "---\nuid: n2\nstatus: superseded\nupdated: 2026-11-03\n---\n# Seite\n\nDocflow Schnittstellen.")
    v.create(CLASH, "---\nuid: n2\nstatus: active\nupdated: 2026-11-03\n---\n# Seite\n\nDocflow Schnittstellen.")
    v.create(CLASH2, "---\nuid: n2\nstatus: superseded\nupdated: 2026-11-03\n---\n# Seite\n\nDocflow Schnittstellen.")
    # no date in the frontmatter: the file name decides
    v.create("raw/conversations/2026-09-09_docflow-kernfragen.md",
             "---\nuid: r1\nstatus: immutable\n---\n# Kernfragen\n\nDocflow Kernfragen.")
    v.create("Projekt/Fragekatalog 04.09.2026.md",
             "---\nuid: n3\nstatus: fertig\nupdated: '2026-09-04'\n---\n# Fragekatalog\n\nDocflow Fragen.")
    v.create("Projekt/Fragekatalog 04.09.2026.sync-conflict-20260909-224424-5IMFKZP.md",
             "---\nuid: n3\nstatus: fertig\nupdated: '2026-09-04'\n---\n# Fragekatalog\n\nDocflow Fragen.")
    # neither frontmatter date nor a date in the name: file time
    v.create("Projekt/Ohne Datum.md", "# Ohne Datum\n\nDocflow ohne Datum.")
    old = datetime.datetime(2026, 8, 1, 10, 0).timestamp()
    os.utime(tmp_path / "Projekt/Ohne Datum.md", (old, old))
    ix = v._init_index()
    ix.rebuild(v.list_notes())
    set_vault(v)
    return v


class TestNoteDate:
    def test_yaml_date_object_counts(self):
        assert note_date({"updated": datetime.date(2026, 11, 3)}, "a.md", now=NOW) == (
            datetime.datetime(2026, 11, 3), "frontmatter")

    def test_string_and_datetime(self):
        assert note_date({"updated": "2026-11-03T08:15:00"}, "a.md", now=NOW)[0] == datetime.datetime(2026, 11, 3)
        aware = datetime.datetime(2026, 11, 3, 8, 15, tzinfo=datetime.timezone.utc)
        assert note_date({"updated": aware}, "a.md", now=NOW) == (
            datetime.datetime(2026, 11, 3, 8, 15), "frontmatter")

    def test_order_of_keys(self):
        meta = {"created": "2026-01-01", "date": "2026-02-02", "updated": "2026-03-03"}
        assert note_date(meta, "a.md", now=NOW)[0] == datetime.datetime(2026, 3, 3)
        assert note_date({"created": "2026-01-01"}, "a.md", now=NOW) == (
            datetime.datetime(2026, 1, 1), "frontmatter")

    def test_file_name_iso_and_german(self):
        assert note_date({}, "raw/conversations/2026-09-09_x.md", now=NOW) == (
            datetime.datetime(2026, 9, 9), "filename")
        assert note_date({}, "P/Bericht 03.11.2026.md", now=NOW) == (
            datetime.datetime(2026, 11, 3), "filename")
        # the newest date in the name wins, the conflict stamp does not count
        assert note_date({}, "P/v1 2026-01-05 bis 2026-02-07.md", now=NOW)[0] == datetime.datetime(2026, 2, 7)
        assert note_date({}, "P/N 04.09.2026.sync-conflict-20260909-224424-ABC.md", now=NOW)[0] == \
            datetime.datetime(2026, 9, 4)

    def test_invalid_and_future_dates_are_skipped(self):
        assert note_date({"updated": "2026-13-45"}, "P/2026-02-30 x.md", 1_700_000_000, now=NOW)[1] == "file"
        assert note_date({"updated": "2031-01-01"}, "P/Bericht 03.11.2026.md", now=NOW) == (
            datetime.datetime(2026, 11, 3), "filename")

    def test_file_time_then_none_never_now(self):
        dt, src = note_date({}, "n.md", 1_700_000_000, now=NOW)
        assert src == "file" and dt.year == 2023
        assert note_date({}, "n.md", None, now=NOW) == (search_index.EPOCH, "none")
        assert note_date({"updated": 20261103}, "n.md", None, now=NOW)[1] == "none"

    def test_parse_since(self):
        assert parse_since("") is None
        assert parse_since(" 2026-11-01 ") == datetime.datetime(2026, 11, 1)
        with pytest.raises(ValueError):
            parse_since("November")


class TestHiddenAndVariants:
    def test_conflict_copy(self):
        assert is_conflict_copy("P/N.sync-conflict-20260909-224424-5IMFKZP.md")
        assert not is_conflict_copy("P/sync-conflict notes.md")
        assert not is_conflict_copy(CLASH)

    def test_is_hidden_and_env(self, monkeypatch):
        assert is_hidden("a.md", "Superseded")
        assert not is_hidden("a.md", "active")
        assert is_hidden("a.sync-conflict-20260909-224424-ABC.md", "active")
        monkeypatch.setenv("OBSIDIAN_SEARCH_HIDE_STATUS", "legacy, archiviert")
        assert is_hidden("a.md", "legacy") and not is_hidden("a.md", "superseded")
        monkeypatch.setenv("OBSIDIAN_SEARCH_HIDE_STATUS", "")
        monkeypatch.setenv("OBSIDIAN_SEARCH_HIDE_CONFLICTS", "0")
        assert not is_hidden("a.sync-conflict-20260909-224424-ABC.md", "superseded")

    def test_variant_key_ignores_sync_suffixes(self):
        plain = variant_key("Projekt/Seite 03.11.2026.md")
        assert variant_key(CLASH) == plain == variant_key(CLASH2)
        assert variant_key("P/Fragekatalog KI.sync-conflict-20260909-224424-5IMFKZP.md") == \
            variant_key("P/Fragekatalog KI.md")
        assert variant_key("P/Name clash Analyse.md") != variant_key("P/Analyse der Lage.md")

    def test_valid_copy_represents_the_group(self):
        hits = [{"path": CLASH2, "status": "superseded"},
                {"path": "Projekt/Seite 03.11.2026.md", "status": "superseded"},
                {"path": CLASH, "status": "active"}]
        fused = rrf_fuse(hits, [], limit=5)
        assert len(fused) == 1
        assert fused[0]["path"] == CLASH and fused[0]["status"] == "active"
        assert sorted(fused[0]["variants"]) == sorted([CLASH2, "Projekt/Seite 03.11.2026.md"])


class TestIndexSearch:
    def test_hits_carry_date_and_source(self, vault):
        hits = {h["path"]: h for h in vault._init_index().search("docflow", limit=20)}
        assert hits["Projekt/Pruefbericht 03.11.2026.md"]["date"] == "2026-11-03"
        assert hits["Projekt/Pruefbericht 03.11.2026.md"]["date_source"] == "frontmatter"
        raw = hits["raw/conversations/2026-09-09_docflow-kernfragen.md"]
        assert (raw["date"], raw["date_source"]) == ("2026-09-09", "filename")
        assert (hits["Projekt/Ohne Datum.md"]["date"], hits["Projekt/Ohne Datum.md"]["date_source"]) == (
            "2026-08-01", "file")

    def test_date_range_no_longer_catches_undated_notes(self, vault):
        ix = vault._init_index()
        paths = {h["path"] for h in ix.search("docflow AND mtime:[2026-10-15 to 2026-11-30]", limit=20)}
        assert "raw/conversations/2026-09-09_docflow-kernfragen.md" not in paths
        assert "Projekt/Ohne Datum.md" not in paths
        assert "Projekt/Pruefbericht 03.11.2026.md" in paths

    def test_newest_first_and_since(self, vault):
        ix = vault._init_index()
        dates = [h["date"] for h in ix.search("docflow", limit=20, sort="newest")]
        assert dates == sorted(dates, reverse=True) and dates[0] == "2026-11-03"
        recent = ix.search("docflow", limit=20, since=datetime.datetime(2026, 10, 1))
        assert recent and all(h["date"] >= "2026-10-01" for h in recent)
        with pytest.raises(ValueError):
            ix.search("docflow", sort="oldest")

    def test_hidden_notes_and_count(self, vault):
        ix = vault._init_index()
        stats: dict = {}
        hits = ix.search("docflow", limit=2, hide_status=("superseded",), hide_conflict_copies=True,
                         stats=stats)
        assert len(hits) == 2 and stats["hidden"] == 3
        everything = ix.search("docflow", limit=20, hide_status=("superseded",),
                               hide_conflict_copies=True)
        paths = {h["path"] for h in everything}
        assert CLASH in paths and CLASH2 not in paths and "Projekt/Seite 03.11.2026.md" not in paths
        assert not any(is_conflict_copy(p) for p in paths)
        assert len(ix.search("docflow", limit=20)) == 8

    def test_meta_lookup(self, vault):
        meta = vault._init_index().meta([CLASH, "gibt/es/nicht.md"])
        assert meta == {CLASH: {"status": "active", "typ": "", "uid": "n2", "title": meta[CLASH]["title"],
                                "date": "2026-11-03", "date_source": "frontmatter"}}


class TestOldIndex:
    def test_index_without_date_source_is_rebuilt(self, tmp_path):
        from whoosh import fields as f
        from whoosh import index as w

        (tmp_path / "vault").mkdir()
        v = Vault(tmp_path / "vault")
        v.create("P/Bericht 03.11.2026.md", "---\nupdated: 2026-11-03\n---\n# Bericht\n\nDocflow.")
        index_dir = search_index.default_index_dir(v.root)
        index_dir.mkdir(parents=True, exist_ok=True)
        old = f.Schema(path=f.ID(stored=True, unique=True), title=f.TEXT(stored=True), body=f.TEXT,
                       tags=f.KEYWORD(stored=True), folder=f.ID(stored=True), typ=f.KEYWORD(stored=True),
                       status=f.KEYWORD(stored=True), uid=f.ID(stored=True),
                       mtime=f.DATETIME(stored=True, sortable=True), fsig=f.STORED())
        w.create_in(index_dir, old)
        ix = VaultIndex(v.root, index_dir)
        assert ix.outdated() and not ix.reconciled_within(10_000)
        ix.index_note(v.read("P/Bericht 03.11.2026.md"))  # a write before the rebuild must not fail
        assert ix.search("docflow")[0]["date_source"] == ""
        stats = ix.reconcile(v.list_notes())
        assert stats["rebuilt"] and not ix.outdated()
        hit = ix.search("docflow")[0]
        assert (hit["date"], hit["date_source"]) == ("2026-11-03", "frontmatter")


class TestTools:
    def test_search_query_defaults_hide_replaced(self, vault):
        out = _call("search_query", query="docflow", limit=20)
        paths = [r["path"] for r in out["results"]]
        assert out["superseded_hidden"] == 3 and out["sort"] == "relevance"
        assert CLASH in paths and CLASH2 not in paths
        assert all("date" in r and "date_source" in r for r in out["results"])

    def test_search_query_newest_since_and_all_versions(self, vault):
        out = _call("search_query", query="docflow", limit=3, sort="newest")
        assert [r["date"] for r in out["results"]][:2] == ["2026-11-03", "2026-11-03"]
        since = _call("search_query", query="docflow", since="2026-10-01")
        assert {r["path"] for r in since["results"]} == {"Projekt/Pruefbericht 03.11.2026.md", CLASH}
        full = _call("search_query", query="docflow", limit=20, include_superseded=True)
        assert full["total"] == 8 and full["superseded_hidden"] == 0

    def test_hybrid_search_fulltext_only(self, vault):
        from obsidian_mcp.server import HybridSearchOutput

        out = hybrid_query(vault, "docflow", limit=10, sort="newest")
        shaped = HybridSearchOutput(**out)  # the tool's output model accepts the new fields
        assert shaped.results[0].date == "2026-11-03" and shaped.superseded_hidden == 3
        assert out["mode"] == "fulltext" and out["sort"] == "newest"
        assert out["superseded_hidden"] == 3
        dates = [r["date"] for r in out["results"]]
        assert dates == sorted(dates, reverse=True)
        first = {r["path"]: r for r in out["results"]}
        assert first[CLASH]["status"] == "active" and first[CLASH]["date_source"] == "frontmatter"
        assert CLASH2 not in first and "Projekt/Seite 03.11.2026.md" not in first

    def test_hybrid_search_with_old_versions_folds_copies(self, vault):
        out = hybrid_query(vault, "docflow", limit=10, include_superseded=True)
        seite = [r for r in out["results"] if variant_key(r["path"]) == variant_key(CLASH)]
        assert len(seite) == 1 and seite[0]["path"] == CLASH
        assert len(seite[0]["variants"]) == 2 and out["superseded_hidden"] == 0

    def test_hybrid_search_since_and_bad_sort(self, vault):
        out = hybrid_query(vault, "docflow", limit=10, since="2026-10-01")
        assert {r["path"] for r in out["results"]} == {"Projekt/Pruefbericht 03.11.2026.md", CLASH}
        with pytest.raises(ValueError):
            hybrid_query(vault, "docflow", sort="oldest")
