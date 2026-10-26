"""Plugin hook: discovery by name, tools, events, fail-open."""

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP

from obsidian_mcp import plugins
from obsidian_mcp.vault import Vault


class FakeEP:
    def __init__(self, name, register):
        self.name = name
        self._register = register

    def load(self):
        return self._register


@pytest.fixture(autouse=True)
def clean():
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


def fresh_mcp():
    m = FastMCP("t")

    @m.tool()
    def read_note(path: str) -> str:
        return path

    return m


def test_enabled_names():
    assert plugins.enabled_names({}) == []
    assert plugins.enabled_names({"OBSIDIAN_PLUGINS": " oversight, x ,,oversight"}) == \
        ["oversight", "x"]


def test_nothing_enabled_loads_nothing():
    called = []
    eps = [FakeEP("oversight", lambda api: called.append(api))]
    assert plugins.load(fresh_mcp(), lambda: None, names=[], entry_points=eps) == {}
    assert called == []


def test_load_registers_tool_and_events(vault):
    seen = []

    def register(api):
        def oversight_status() -> str:
            """Open findings."""
            return "ok"
        api.add_tool(oversight_status)
        api.on("after_write", lambda **kw: seen.append(kw))
        api.on("startup", lambda **kw: seen.append(("startup", kw)))

    m = fresh_mcp()
    st = plugins.load(m, lambda: vault, names=["oversight"],
                      entry_points=[FakeEP("oversight", register)])
    assert st["oversight"]["loaded"] is True
    assert st["oversight"]["tools"] == ["oversight_status"]
    assert "oversight_status" in {t.name for t in m._tool_manager.list_tools()}
    assert seen[0][0] == "startup"
    vault.create("Inbox/n.md", "# n")
    ev = seen[-1]
    assert (ev["path"], ev["action"], ev["layer"]) == ("Inbox/n.md", "create", "para")
    assert "actor" in ev


def test_core_tool_name_reserved():
    def register(api):
        api.add_tool(lambda path: path, name="read_note")

    st = plugins.load(fresh_mcp(), lambda: None, names=["bad"],
                      entry_points=[FakeEP("bad", register)])
    assert st["bad"]["loaded"] is False
    assert "belongs to the server" in st["bad"]["last_error"]


def test_missing_plugin_does_not_fail():
    st = plugins.load(fresh_mcp(), lambda: None, names=["fehlt"], entry_points=[])
    assert st["fehlt"] == {"loaded": False, "last_error": "not installed"}


def test_register_exception_does_not_fail():
    def register(api):
        raise RuntimeError("kaputt")

    st = plugins.load(fresh_mcp(), lambda: None, names=["p"], entry_points=[FakeEP("p", register)])
    assert st["p"]["loaded"] is False
    assert "RuntimeError: kaputt" in st["p"]["last_error"]


def test_handler_error_is_fail_open(vault):
    def register(api):
        def boom(**kw):
            raise ValueError("handler kaputt")
        api.on("after_write", boom)

    plugins.load(fresh_mcp(), lambda: vault, names=["p"], entry_points=[FakeEP("p", register)])
    vault.create("Inbox/a.md", "# a")  # must not raise
    assert vault.read("Inbox/a.md").title
    st = plugins.status()["p"]
    assert st["errors"] == 1
    assert "after_write: ValueError" in st["last_error"]


def test_unknown_event_rejected():
    def register(api):
        api.on("before_everything", lambda **kw: None)

    st = plugins.load(fresh_mcp(), lambda: None, names=["p"], entry_points=[FakeEP("p", register)])
    assert "unknown event" in st["p"]["last_error"]


def test_policy_refusal_and_tool_error_events(vault):
    from obsidian_mcp import server

    events = []

    def register(api):
        api.on("policy_refusal", lambda **kw: events.append(("refusal", kw)))
        api.on("tool_error", lambda **kw: events.append(("error", kw)))

    plugins.load(fresh_mcp(), lambda: vault, names=["p"], entry_points=[FakeEP("p", register)])
    server.set_vault(vault)
    (vault.root / "raw").mkdir()
    (vault.root / "raw" / "q.md").write_text("# q\n\nquelle\n")
    with pytest.raises(Exception):
        asyncio.run(server.mcp.call_tool(
            "update_note", {"path": "raw/q.md", "content": "# q\n\nneu"}))
    with pytest.raises(Exception):
        asyncio.run(server.mcp.call_tool("read_note", {"path": "Inbox/fehlt.md"}))
    kinds = [k for k, _ in events]
    assert kinds == ["refusal", "error"]
    refusal = events[0][1]
    assert refusal["tool"] == "update_note"
    assert refusal["policy"] == "RAW_IMMUTABLE"
    err = events[1][1]
    assert (err["tool"], err["error_type"]) == ("read_note", "FileNotFoundError")


def test_load_is_idempotent():
    calls = []
    eps = [FakeEP("p", lambda api: calls.append(1))]
    plugins.load(fresh_mcp(), lambda: None, names=["p"], entry_points=eps)
    plugins.load(fresh_mcp(), lambda: None, names=["p"], entry_points=eps)
    assert calls == [1]
