"""Tests for the Whoosh-backed search index and the indexed search tools."""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp.search_index import WHOOSH_AVAILABLE, VaultIndex, default_index_dir
from obsidian_mcp.server import mcp, set_vault
from obsidian_mcp.vault import Vault

pytestmark = pytest.mark.skipif(not WHOOSH_AVAILABLE, reason="whoosh3 not installed")


# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
def indexed_vault(tmp_path):
    v = Vault(tmp_path)
    v.create(
        "wiki/concepts/Mayer Screener.md",
        "---\ntitel: Mayer 100-Bagger\ntyp: konzept\nstatus: aktiv\nuid: cpt-mayer\ntags: [aktien, screener]\n---\n"
        "# Mayer 100-Bagger\n\nDer Screener findet Qualitätsfirmen. Twin Engines: EPS plus P/E.",
    )
    v.create(
        "04 Resources/Technik/Honcho Doku.md",
        "---\ntitel: Honcho Memory\ntyp: anleitung\nstatus: aktiv\ntags: [honcho]\n---\n"
        "# Honcho\n\nHoncho ist das Mittelgedächtnis mit Pointern.",
    )
    v.create("Notes/random.md", "# Random\n\nNichts Relevantes hier.")
    ix = VaultIndex(tmp_path)
    ix.rebuild(v.list_notes())
    set_vault(v)
    return v, ix, tmp_path


# ---- index basics ------------------------------------------------------


class TestIndex:
    def test_default_index_dir_in_vault(self, tmp_path):
        assert default_index_dir(tmp_path) == tmp_path / ".obsidian-mcp-index"

    def test_env_overrides_index_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_INDEX_DIR", str(tmp_path / "elsewhere"))
        assert default_index_dir(tmp_path) == tmp_path / "elsewhere"

    def test_rebuild_counts(self, indexed_vault):
        v, ix, tmp = indexed_vault
        assert ix.count() == 3

    def test_incremental_via_create(self, indexed_vault):
        v, ix, tmp = indexed_vault
        v.create("Notes/neu.md", "# Quantencomputer\n\nGanz neue Begriffe.")
        # _after_write already indexed it
        assert len(ix.search("quantencomputer")) == 1

    def test_remove_note(self, indexed_vault):
        v, ix, tmp = indexed_vault
        ix.remove_note("Notes/random.md")
        assert ix.count() == 2


# ---- query syntax ------------------------------------------------------


class TestQuerySyntax:
    def test_multiword_now_works(self, indexed_vault):
        v, ix, tmp = indexed_vault
        assert len(ix.search("Mayer Screener")) == 1  # the old substring search returned 0

    def test_field_tags(self, indexed_vault):
        v, ix, tmp = indexed_vault
        titles = [r["title"] for r in ix.search("tags:screener")]
        assert "Mayer 100-Bagger" in titles

    def test_field_typ(self, indexed_vault):
        v, ix, tmp = indexed_vault
        assert len(ix.search("typ:konzept")) == 1

    def test_boolean_not(self, indexed_vault):
        v, ix, tmp = indexed_vault
        assert len(ix.search("mayer AND screener NOT honcho")) == 1

    def test_phrase(self, indexed_vault):
        v, ix, tmp = indexed_vault
        assert len(ix.search('"twin engines"')) == 1

    def test_wildcard(self, indexed_vault):
        v, ix, tmp = indexed_vault
        assert len(ix.search("screener*")) == 1

    def test_fuzzy(self, indexed_vault):
        v, ix, tmp = indexed_vault
        results = ix.search("maier~1")
        titles = [r["title"] for r in results]
        assert "Mayer 100-Bagger" in titles  # distance 1 match
        assert len(results) < 3  # but not everything (maxdist=1 excludes distant words)

    def test_range_mtime(self, indexed_vault):
        v, ix, tmp = indexed_vault
        assert len(ix.search("mtime:[2020-01-01 to 2030-01-01]")) == 3

    def test_ranking_score(self, indexed_vault):
        v, ix, tmp = indexed_vault
        results = ix.search("mayer")
        assert results[0]["score"] > 0

    def test_did_you_mean(self, indexed_vault):
        v, ix, tmp = indexed_vault
        corrected = ix.corrected("mayr")
        assert "mayer" in corrected.lower()


# ---- server tools ------------------------------------------------------


class TestIndexedTools:
    def test_search_query_tool(self, indexed_vault):
        result = asyncio.run(mcp.call_tool("search_query", {"query": "tags:screener"}))
        data = json.loads(result[0][0].text)
        assert data["total"] >= 1
        assert data["results"][0]["path"] == "wiki/concepts/Mayer Screener.md"

    def test_search_query_with_correction(self, indexed_vault):
        result = asyncio.run(mcp.call_tool("search_query", {"query": "mayr screener"}))
        data = json.loads(result[0][0].text)
        assert "mayer" in data["corrected"].lower()

    def test_rebuild_index_tool(self, indexed_vault):
        result = asyncio.run(mcp.call_tool("rebuild_index", {}))
        data = json.loads(result[0][0].text)
        assert data["indexed"] == 3

    def test_index_status_tool(self, indexed_vault):
        result = asyncio.run(mcp.call_tool("index_status", {}))
        data = json.loads(result[0][0].text)
        assert data["in_sync"] is True
        assert data["enabled"] is True

    def test_search_notes_still_works(self, indexed_vault):
        # old plain tool unaffected
        result = asyncio.run(mcp.call_tool("search_notes", {"query": "Mayer"}))
        data = json.loads(result[0][0].text)
        assert len(data) >= 1
