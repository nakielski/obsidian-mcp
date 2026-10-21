"""OBSIDIAN_SEARCH_RESTRICTED applies to every search, listing and graph tool."""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp.search_index import WHOOSH_AVAILABLE
from obsidian_mcp.server import mcp, set_vault
from obsidian_mcp.vault import Vault

pytestmark = pytest.mark.skipif(not WHOOSH_AVAILABLE, reason="whoosh3 not installed")


def call(tool, **args):
    result = asyncio.run(mcp.call_tool(tool, args))
    return json.loads(result[0][0].text)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_SEARCH_RESTRICTED", "05 People")
    v = Vault(tmp_path)
    v.create("05 People/Anna Beispiel.md", "# Anna Beispiel\n\nAnna mag Kaffee. Siehe [[Projekt Kaffee]].",
             tags=["person"])
    v.create("Notes/Projekt Kaffee.md", "# Projekt Kaffee\n\nMit [[Anna Beispiel]] besprochen. Kaffee TODO.",
             tags=["projekt"])
    v.create("05 Peoplex/Kein Treffer.md", "# Kein Treffer\n\nKaffee am Rand.")  # similar name, not restricted
    set_vault(v)
    return v


PEOPLE = "05 People/Anna Beispiel.md"


def paths(rows):
    return {r["path"] if isinstance(r, dict) else r for r in rows}


def test_search_tools_hide_restricted_folder(vault):
    for tool, args in (("search_notes", {"query": "Kaffee"}), ("search_query", {"query": "Kaffee"})):
        data = call(tool, **args)
        assert PEOPLE not in paths(data["results"]), tool
        assert "Notes/Projekt Kaffee.md" in paths(data["results"])
        assert "05 Peoplex/Kein Treffer.md" in paths(data["results"])  # whole folders only
        assert data["restricted_hidden"] is True
        full = call(tool, include_restricted=True, **args)
        assert PEOPLE in paths(full["results"]) and full["restricted_hidden"] is False


def test_listing_hides_unless_folder_named_explicitly(vault):
    for tool in ("list_notes", "list_note_paths"):
        assert PEOPLE not in paths(call(tool)["results"])
        assert PEOPLE in paths(call(tool, include_restricted=True)["results"])
        assert paths(call(tool, folder="05 People")["results"]) == {PEOPLE}  # explicit request


def test_graph_tools_hide_nodes_without_breaking_links(vault):
    g = call("vault_graph")
    assert PEOPLE not in {n["path"] for n in g["nodes"]}
    assert all(e["source"] != PEOPLE for e in g["edges"])
    edge = next(e for e in g["edges"] if e["source"] == "Notes/Projekt Kaffee.md")
    assert edge == {"source": "Notes/Projekt Kaffee.md", "target": "Anna Beispiel", "target_exists": True}
    assert g["stats"]["broken_link_count"] == 0
    assert PEOPLE in {n["path"] for n in call("vault_graph", include_restricted=True)["nodes"]}

    assert PEOPLE not in paths(call("get_backlinks", title="Projekt Kaffee")["results"])
    assert PEOPLE in paths(call("get_backlinks", title="Projekt Kaffee", include_restricted=True)["results"])
    assert PEOPLE not in paths(call("find_orphans")["results"])

    health = call("vault_health")
    assert PEOPLE not in json.dumps(health)
    assert health["checks"]["broken_links"]["count"] == 0
    assert health["total_notes"] == 2


def test_explicit_read_is_never_restricted(vault):
    assert call("read_note", path=PEOPLE)["title"] == "Anna Beispiel"
    assert call("get_outlinks", path=PEOPLE)["results"] == ["Projekt Kaffee"]


def test_unset_restriction_changes_nothing(vault, monkeypatch):
    monkeypatch.delenv("OBSIDIAN_SEARCH_RESTRICTED")
    data = call("search_notes", query="Kaffee")
    assert PEOPLE in paths(data["results"]) and data["restricted_hidden"] is False
    assert PEOPLE in paths(call("list_note_paths")["results"])
    assert PEOPLE in {n["path"] for n in call("vault_graph")["nodes"]}
