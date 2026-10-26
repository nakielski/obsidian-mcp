"""Plugins: optional extensions that add tools and react to server events.

A plugin is an installed Python package with an entry point in the group
``obsidian_mcp.plugins``::

    [project.entry-points."obsidian_mcp.plugins"]
    oversight = "agent_oversight.mcp_plugin:register"

Only plugins enabled by name are loaded: ``OBSIDIAN_PLUGINS=oversight`` (comma-separated).
Without the variable the server behaves exactly as before. ``register(api)`` receives a
:class:`PluginAPI` and can

- add tools (``api.add_tool(fn)``; core tool names are reserved),
- subscribe to events (``api.on("after_write", fn)``).

Events and their keyword arguments:

- ``startup``         ``actor``
- ``after_write``     ``path``, ``action``, ``layer``, ``actor``
- ``policy_refusal``  ``tool``, ``policy``, ``message``, ``actor``
- ``tool_error``      ``tool``, ``error_type``, ``message``, ``actor``

Fail-open: an error in a plugin (while loading or in a handler) is written to stderr and kept
in the plugin status, but never fails a write or a tool call. Plugins run in the server process
with the server's rights; that is why nothing is loaded automatically, only what is enabled by
name.
"""

from __future__ import annotations

import os
import sys
import time
from importlib import metadata
from typing import Any, Callable

GROUP = "obsidian_mcp.plugins"
EVENTS = ("startup", "after_write", "policy_refusal", "tool_error")
ERROR_LOG_INTERVAL = 60.0  # log the same plugin error at most once a minute

_handlers: dict[str, list[tuple[str, Callable[..., Any]]]] = {e: [] for e in EVENTS}
_status: dict[str, dict] = {}
_last_error_log: dict[tuple[str, str], float] = {}


def actor() -> str:
    return os.environ.get("VAULT_ACTOR", "unknown")


def enabled_names(env: dict | None = None) -> list[str]:
    raw = (env if env is not None else os.environ).get("OBSIDIAN_PLUGINS", "")
    names = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def _log(msg: str) -> None:
    print(f"[obsidian-mcp] {msg}", file=sys.stderr)


def _record_error(plugin: str, where: str, exc: BaseException) -> None:
    st = _status.setdefault(plugin, {"loaded": False})
    st["errors"] = st.get("errors", 0) + 1
    st["last_error"] = f"{where}: {type(exc).__name__}: {exc}"[:300]
    key = (plugin, where)
    now = time.monotonic()
    if now - _last_error_log.get(key, -ERROR_LOG_INTERVAL) >= ERROR_LOG_INTERVAL:
        _last_error_log[key] = now
        _log(f"plugin {plugin}: error in {where}: {type(exc).__name__}: {exc}")


class PluginAPI:
    """What a plugin sees of the server."""

    def __init__(self, name: str, mcp, vault_getter: Callable[[], Any], core_tools: set[str]):
        self.name = name
        self._mcp = mcp
        self._vault_getter = vault_getter
        self._core_tools = core_tools
        self.tools: list[str] = []

    @property
    def actor(self) -> str:
        return actor()

    def vault(self):
        return self._vault_getter()

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}, allowed: {', '.join(EVENTS)}")
        _handlers[event].append((self.name, handler))

    def add_tool(self, fn: Callable[..., Any], name: str | None = None, **kwargs) -> None:
        tool_name = name or fn.__name__
        existing = {t.name for t in self._mcp._tool_manager.list_tools()}
        if tool_name in self._core_tools:
            raise ValueError(f"tool name {tool_name!r} belongs to the server")
        if tool_name in existing:
            raise ValueError(f"tool name {tool_name!r} is already taken")
        self._mcp.tool(name=tool_name, **kwargs)(fn)
        self.tools.append(tool_name)


def load(mcp, vault_getter: Callable[[], Any], names: list[str] | None = None,
         entry_points: list | None = None) -> dict[str, dict]:
    """Load the enabled plugins. Returns the status per plugin; never raises."""
    names = enabled_names() if names is None else names
    if not names:
        return dict(_status)
    try:
        eps = list(entry_points) if entry_points is not None else \
            list(metadata.entry_points(group=GROUP))
    except Exception as exc:  # pragma: no cover - broken package metadata
        _log(f"plugins not discoverable: {exc}")
        eps = []
    core = {t.name for t in mcp._tool_manager.list_tools()}
    for name in names:
        if _status.get(name, {}).get("loaded"):
            continue
        ep = next((e for e in eps if e.name == name), None)
        if ep is None:
            _status[name] = {"loaded": False, "last_error": "not installed"}
            _log(f"plugin {name}: not installed (entry point {GROUP}:{name} missing)")
            continue
        api = PluginAPI(name, mcp, vault_getter, core)
        try:
            register = ep.load()
            register(api)
        except Exception as exc:
            _record_error(name, "register", exc)
            continue
        _status[name] = {"loaded": True, "tools": list(api.tools), "errors": 0}
    emit("startup", actor=actor())
    return dict(_status)


def emit(event: str, **kwargs) -> None:
    """Send an event to all handlers; errors are swallowed (fail-open)."""
    for plugin, handler in list(_handlers.get(event, ())):
        try:
            handler(**kwargs)
        except Exception as exc:
            _record_error(plugin, event, exc)


def status() -> dict[str, dict]:
    return {k: dict(v) for k, v in _status.items()}


def reset() -> None:
    """For tests only."""
    for e in EVENTS:
        _handlers[e].clear()
    _status.clear()
    _last_error_log.clear()
