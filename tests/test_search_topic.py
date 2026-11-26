"""scope='topic': notes that are about the query words, not notes that mention them."""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp.hybrid import hybrid_query
from obsidian_mcp.search_index import WHOOSH_AVAILABLE, is_about, topic_terms
from obsidian_mcp.server import HybridSearchOutput, mcp, set_vault
from obsidian_mcp.vault import Vault

pytestmark = pytest.mark.skipif(not WHOOSH_AVAILABLE, reason="whoosh3 not installed")

HANDOVER = "Projekte/Assistent/Uebergabe Vault-Suche 26.11.2026.md"
BY_FOLDER = "Areas/MIC DOCFLOW/Pruefbericht 03.11.2026.md"
BY_TITLE = "Inbox/Notiz 05.11.2026.md"
BY_TAG = "Inbox/Offene Punkte 06.11.2026.md"
OLD = "Areas/MIC DOCFLOW/Seite 03.11.2026.md"


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
    v.create(BY_FOLDER, "---\nuid: n1\nstatus: active\nupdated: 2026-11-03\n---\n# Prüfbericht\n\nDocflow Prüfung.")
    v.create(BY_TITLE, "---\nuid: n2\nupdated: 2026-11-05\n---\n# DocFlow Review\n\nDocflow Termin.")
    v.create(BY_TAG, "---\nuid: n3\nupdated: 2026-11-06\ntags: [mic-docflow]\n---\n# Offene Punkte\n\nDocflow Liste.")
    v.create(HANDOVER, "---\nuid: n4\nupdated: 2026-11-26\n---\n# Übergabe Vault-Suche\n\nMessfall: Docflow.")
    v.create(OLD, "---\nuid: n5\nstatus: superseded\nupdated: 2026-11-03\n---\n# Seite\n\nDocflow alt.")
    v.create("Projekte/Assistent/Alte Messung 01.11.2026.md",
             "---\nuid: n6\nstatus: superseded\nupdated: 2026-11-01\n---\n# Messung\n\nDocflow erwähnt.")
    ix = v._init_index()
    ix.rebuild(v.list_notes())
    set_vault(v)
    return v


class TestTopicTerms:
    def test_words_without_syntax(self):
        assert topic_terms("DocFlow") == ("docflow",)
        assert topic_terms("MIC DocFlow AND status:active NOT zu") == ("mic", "docflow")
        assert topic_terms("an in") == ()

    def test_is_about_path_title_or_tags(self):
        terms = ("docflow",)
        assert is_about(terms, BY_FOLDER, "Prüfbericht")
        assert is_about(terms, BY_TITLE, "DocFlow Review")
        assert is_about(terms, BY_TAG, "Offene Punkte", "mic-docflow,vocs")
        assert is_about(terms, BY_TAG, "Offene Punkte", ["mic-docflow"])
        assert not is_about(terms, HANDOVER, "Übergabe Vault-Suche", "ultron")

    def test_every_word_and_no_words(self):
        assert is_about(("mic", "docflow"), BY_FOLDER)
        assert not is_about(("mic", "docflow"), BY_TITLE, "DocFlow Review")
        assert is_about((), HANDOVER)


class TestSearchQuery:
    def test_all_is_the_default(self, vault):
        out = _call("search_query", query="docflow", limit=20)
        assert out["scope"] == "all" and out["mentions_hidden"] == 0
        assert HANDOVER in {r["path"] for r in out["results"]} and out["total"] == 4

    def test_topic_drops_mentions_and_counts_them(self, vault):
        out = _call("search_query", query="docflow", limit=20, scope="topic")
        assert {r["path"] for r in out["results"]} == {BY_FOLDER, BY_TITLE, BY_TAG}
        # the replaced mention is hidden as replaced, not counted as a mention
        assert out["scope"] == "topic" and out["mentions_hidden"] == 1 and out["superseded_hidden"] == 2

    def test_topic_counts_past_the_limit(self, vault):
        out = _call("search_query", query="docflow", limit=1, scope="topic", sort="newest")
        assert [r["path"] for r in out["results"]] == [BY_TAG] and out["mentions_hidden"] == 1

    def test_bad_scope(self, vault):
        with pytest.raises(Exception, match="scope must be one of"):
            _call("search_query", query="docflow", scope="some")


class TestHybrid:
    def test_all_keeps_mentions(self, vault):
        out = hybrid_query(vault, "docflow", limit=10, sort="newest")
        assert out["scope"] == "all" and out["mentions_hidden"] == 0
        assert out["results"][0]["path"] == HANDOVER

    def test_topic_newest(self, vault):
        out = hybrid_query(vault, "docflow", limit=10, sort="newest", scope="topic")
        shaped = HybridSearchOutput(**out)
        assert [r.path for r in shaped.results] == [BY_TAG, BY_TITLE, BY_FOLDER]
        assert shaped.scope == "topic" and shaped.mentions_hidden == 1 and shaped.superseded_hidden == 1

    def test_topic_with_old_versions(self, vault):
        out = hybrid_query(vault, "docflow", limit=10, scope="topic", include_superseded=True)
        assert OLD in {r["path"] for r in out["results"]}
        assert out["mentions_hidden"] == 2 and out["superseded_hidden"] == 0

    def test_topic_since(self, vault):
        out = hybrid_query(vault, "docflow", limit=10, scope="topic", since="2026-11-10")
        assert out["results"] == [] and out["mentions_hidden"] == 1

    def test_bad_scope(self, vault):
        with pytest.raises(ValueError):
            hybrid_query(vault, "docflow", scope="some")
