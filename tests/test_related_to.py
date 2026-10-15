"""related_to on write tools: the server owns the write log, clients only link work items."""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import policies
from obsidian_mcp.server import SERVER_INSTRUCTIONS, mcp, set_vault
from obsidian_mcp.vault import Vault

WRITE_TOOLS = {
    "create_note", "update_note", "delete_note", "archive_note", "append_to_note",
    "manage_frontmatter", "manage_tags", "patch_note", "daily_note", "search_and_replace",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _log_lines(root: pathlib.Path, actor: str = "agent") -> list[dict]:
    shard = root / ".vault-write-log.d" / f"{actor}.jsonl"
    if not shard.exists():
        return []
    return [json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ACTOR", "agent")
    monkeypatch.setenv("OBSIDIAN_DAILY_DIR", "Daily")
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "a.md").write_text(
        "---\ntitle: A\nuid: note-a\n---\n# A\n\n## Section\nalt\n", encoding="utf-8")
    v = Vault(tmp_path)
    set_vault(v)
    yield v
    set_vault(None)


class TestPolicyContext:
    def test_context_sets_and_resets(self, vault, tmp_path):
        with policies.related_context("ent-20261005-belegarchiv"):
            vault.create("Notes/b.md", "# B")
        vault.create("Notes/c.md", "# C")
        lines = _log_lines(tmp_path)
        assert [(e["path"], e["related_to"]) for e in lines] == [
            ("Notes/b.md", "ent-20261005-belegarchiv"), ("Notes/c.md", "")]

    def test_reset_after_exception(self, vault, tmp_path):
        with pytest.raises(FileExistsError):
            with policies.related_context("task-1"):
                vault.create("Notes/a.md", "# exists")
        vault.create("Notes/d.md", "# D")
        assert _log_lines(tmp_path)[-1]["related_to"] == ""

    def test_explicit_argument_wins(self, tmp_path):
        with policies.related_context("from-context"):
            policies.log_write(tmp_path, "x.md", "modify", "para", actor="agent",
                               related_to="explicit")
            policies.log_write(tmp_path, "y.md", "modify", "para", actor="agent")
        assert [e["related_to"] for e in _log_lines(tmp_path)] == ["explicit", "from-context"]

    @pytest.mark.parametrize("bad", ["a b", "x" * 81, "-start", "ent/../x", "ä-uid", "a\nb"])
    def test_invalid_values(self, bad):
        with pytest.raises(ValueError, match="related_to"):
            policies.check_related_to(bad)

    @pytest.mark.parametrize("good", ["", "  ", "ent-20261005-belegarchiv", "raw-0451dc",
                                      "t_63d3493f", "concept:1.2"])
    def test_valid_values(self, good):
        assert policies.check_related_to(good) == good.strip()


class TestTools:
    def test_all_write_tools_offer_related_to(self):
        tools = {t.name: t for t in _run(mcp.list_tools())}
        assert WRITE_TOOLS <= set(tools)
        for name in WRITE_TOOLS:
            props = tools[name].inputSchema["properties"]
            assert "related_to" in props, name
            assert "related_to" not in tools[name].inputSchema.get("required", []), name
        for name in ("read_note", "search_notes", "list_notes", "vault_health"):
            assert "related_to" not in tools[name].inputSchema["properties"], name

    def test_create_and_append_log_related_to(self, vault, tmp_path):
        _run(mcp.call_tool("create_note", {"path": "Notes/e.md", "content": "# E",
                                           "related_to": "raw-20261015-x"}))
        _run(mcp.call_tool("append_to_note", {"path": "Notes/e.md", "content": "- mehr"}))
        _run(mcp.call_tool("patch_note", {"path": "Notes/a.md", "heading": "Section",
                                          "action": "append", "content": "neu",
                                          "related_to": "raw-20261015-x"}))
        _run(mcp.call_tool("manage_frontmatter", {"path": "Notes/a.md", "action": "set",
                                                  "key": "status", "value": "aktiv",
                                                  "related_to": "raw-20261015-x"}))
        _run(mcp.call_tool("manage_tags", {"path": "Notes/a.md", "action": "add",
                                           "tags": ["x"], "related_to": "raw-20261015-x"}))
        _run(mcp.call_tool("search_and_replace", {"path": "Notes/a.md", "find": "alt",
                                                  "replace": "neu",
                                                  "related_to": "raw-20261015-x"}))
        got = [(e["path"], e["related_to"]) for e in _log_lines(tmp_path)]
        assert got == [("Notes/e.md", "raw-20261015-x"), ("Notes/e.md", ""),
                       ("Notes/a.md", "raw-20261015-x"), ("Notes/a.md", "raw-20261015-x"),
                       ("Notes/a.md", "raw-20261015-x"), ("Notes/a.md", "raw-20261015-x")]

    def test_daily_and_archive(self, vault, tmp_path):
        (tmp_path / "Daily").mkdir()
        (tmp_path / "06 Archive").mkdir()
        _run(mcp.call_tool("daily_note", {"action": "append", "content": "- x",
                                          "date": "2026-10-16", "related_to": "task-7"}))
        _run(mcp.call_tool("archive_note", {"path": "Notes/a.md", "related_to": "task-7"}))
        assert {e["related_to"] for e in _log_lines(tmp_path)} == {"task-7"}

    def test_invalid_related_to_writes_nothing(self, vault, tmp_path):
        with pytest.raises(Exception, match="related_to"):
            _run(mcp.call_tool("create_note", {"path": "Notes/f.md", "content": "# F",
                                               "related_to": "nicht gültig"}))
        assert not (tmp_path / "Notes" / "f.md").exists()
        assert _log_lines(tmp_path) == []

    def test_server_instructions_and_denial_hint(self, vault):
        assert "Never append write-log lines" in SERVER_INSTRUCTIONS
        assert mcp.instructions == SERVER_INSTRUCTIONS
        for path in (".vault-write-log.jsonl", "wiki/index.json"):
            with pytest.raises(PermissionError, match="nothing to append by hand"):
                vault.append(path, "{}")
