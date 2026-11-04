"""Scope locks (.vault-lock.d/<actor>.json) and the empty-frontmatter fix."""
import asyncio
import datetime
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import policies
from obsidian_mcp.vault import Vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ACTOR", "claude")
    (tmp_path / "wiki" / "log").mkdir(parents=True)
    (tmp_path / "04 Projects" / "A").mkdir(parents=True)
    (tmp_path / "wiki" / "log" / "2026-11.md").write_text("- erster Eintrag\n", encoding="utf-8")
    (tmp_path / "04 Projects" / "A" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    return Vault(tmp_path)


def _expire(root, actor):
    path = root / ".vault-lock.d" / f"{actor}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat()
    for e in data["locks"]:
        e["expires"] = past
    path.write_text(json.dumps(data), encoding="utf-8")


class TestScopeHelpers:
    @pytest.mark.parametrize("scope,path,hit", [
        ("wiki/log", "wiki/log/2026-11.md", True),
        ("wiki/log", "wiki/logbuch/x.md", False),
        ("Wiki/Log", "wiki/log/2026-11.md", True),
        ("wiki/log/2026-11", "wiki/log/2026-11.md", True),
        ("wiki/log/2026-11.md", "wiki/log/2026-11.md", True),
        ("wiki/log/2026-11.md", "wiki/log/2026-10.md", False),
        ("*", "anything/at/all.md", True),
    ])
    def test_scope_covers(self, scope, path, hit):
        assert policies.scope_covers(scope, path) is hit

    @pytest.mark.parametrize("bad", ["", "  ", "wiki/../x", "./wiki", "a//b"])
    def test_invalid_scope(self, bad):
        with pytest.raises(ValueError):
            policies.normalize_scope(bad)

    def test_normalize(self):
        assert policies.normalize_scope("/wiki\\log/") == "wiki/log"
        assert policies.normalize_scope("*") == "*"

    @pytest.mark.parametrize("ttl", [0, 241, "x"])
    def test_ttl_bounds(self, vault, ttl):
        with pytest.raises(ValueError):
            policies.lock_scope(vault.root, "wiki/log", ttl_min=ttl)


class TestScopeLock:
    def test_foreign_scope_blocks_only_inside(self, vault):
        policies.lock_scope(vault.root, "wiki/log", "Monatswechsel", actor="codex")
        with pytest.raises(policies.PolicyViolationError, match="locked by 'codex'") as exc:
            vault.append("wiki/log/2026-11.md", "- zweiter Eintrag")
        assert exc.value.policy == "VAULT_LOCK"
        assert "Monatswechsel" in str(exc.value)
        # outside the scope everything works
        vault.append("04 Projects/A/plan.md", "weiter")
        vault.create("wiki/log-notes.md", "---\nuid: cpt-x\n---\n# andere Datei")

    def test_own_scope_does_not_block(self, vault):
        policies.lock_scope(vault.root, "wiki/log")
        vault.append("wiki/log/2026-11.md", "- eigener Eintrag")

    def test_all_write_paths_respect_scope(self, vault):
        policies.lock_scope(vault.root, "04 Projects/A", actor="codex")
        p = "04 Projects/A/plan.md"
        calls = [
            lambda: vault.create("04 Projects/A/neu.md", "x"),
            lambda: vault.update(p, "x"),
            lambda: vault.delete(p),
            lambda: vault.append(p, "x"),
            lambda: vault.set_frontmatter(p, "k", "v"),
            lambda: vault.delete_frontmatter(p, "k"),
            lambda: vault.update_frontmatter(p, {"k": "v"}),
            lambda: vault.add_tags(p, ["t"]),
            lambda: vault.remove_tags(p, ["t"]),
            lambda: vault.patch_section(p, "Plan", "append", "x"),
            lambda: vault.search_and_replace(p, "Plan", "Plan2"),
            lambda: vault.archive(p),
        ]
        for call in calls:
            with pytest.raises(policies.PolicyViolationError):
                call()
        assert (vault.root / p).read_text(encoding="utf-8") == "# Plan\n"

    def test_archive_target_in_foreign_scope_blocks(self, vault):
        policies.lock_scope(vault.root, "06 Archive", actor="codex")
        with pytest.raises(policies.PolicyViolationError):
            vault.archive("04 Projects/A/plan.md")
        assert (vault.root / "04 Projects/A/plan.md").exists()

    def test_overlapping_scopes_refused(self, vault):
        policies.lock_scope(vault.root, "wiki", actor="codex")
        with pytest.raises(policies.PolicyViolationError, match="overlaps"):
            policies.lock_scope(vault.root, "wiki/log")
        with pytest.raises(policies.PolicyViolationError):
            policies.lock_scope(vault.root, "*")
        policies.lock_scope(vault.root, "04 Projects/A")  # disjoint: fine

    def test_whole_vault_scope(self, vault):
        policies.lock_scope(vault.root, "*", actor="codex")
        with pytest.raises(policies.PolicyViolationError):
            vault.create("Inbox/x.md", "x")

    def test_expired_lock_is_ignored(self, vault):
        policies.lock_scope(vault.root, "wiki/log", actor="codex")
        _expire(vault.root, "codex")
        vault.append("wiki/log/2026-11.md", "- nach Ablauf")
        policies.lock_scope(vault.root, "wiki/log")  # can be taken again
        assert policies.lock_status(vault.root)["locks"][0]["agent"] == "claude"

    def test_relock_refreshes_and_release(self, vault):
        first = policies.lock_scope(vault.root, "wiki/log", ttl_min=5)
        second = policies.lock_scope(vault.root, "wiki/log", ttl_min=60)
        assert second["expires"] > first["expires"]
        locks = policies.lock_status(vault.root)["locks"]
        assert len(locks) == 1 and locks[0]["remaining_min"] > 50
        policies.lock_scope(vault.root, "04 Projects/A")
        assert policies.release_scope_lock(vault.root, "wiki/log") == ["wiki/log"]
        assert [lk["scope"] for lk in policies.lock_status(vault.root)["locks"]] == ["04 Projects/A"]
        assert policies.release_scope_lock(vault.root) == ["04 Projects/A"]
        assert not (vault.root / ".vault-lock.d" / "claude.json").exists()

    def test_release_only_own(self, vault):
        policies.lock_scope(vault.root, "wiki/log", actor="codex")
        assert policies.release_scope_lock(vault.root, "wiki/log") == []
        assert len(policies.lock_status(vault.root)["locks"]) == 1

    def test_refresh(self, vault):
        with pytest.raises(ValueError, match="No active lock"):
            policies.refresh_scope_lock(vault.root)
        policies.lock_scope(vault.root, "wiki/log", ttl_min=1)
        out = policies.refresh_scope_lock(vault.root, ttl_min=120)
        assert out[0]["scope"] == "wiki/log"
        assert policies.lock_status(vault.root)["locks"][0]["remaining_min"] > 100

    def test_legacy_lock_still_honoured(self, vault):
        policies.acquire_lock(vault.root, scope="Umbau", actor="hermes")
        with pytest.raises(policies.PolicyViolationError, match="Vault is locked by 'hermes'"):
            vault.append("04 Projects/A/plan.md", "x")
        with pytest.raises(policies.PolicyViolationError):
            policies.lock_scope(vault.root, "wiki/log")
        status = policies.lock_status(vault.root)["locks"]
        assert status[0]["legacy"] is True and status[0]["scope"] == "*"

    def test_disabled_by_flag(self, vault, monkeypatch):
        policies.lock_scope(vault.root, "wiki/log", actor="codex")
        monkeypatch.setenv("VAULT_LOCK", "0")
        vault.append("wiki/log/2026-11.md", "- trotz Sperre")

    def test_malformed_lock_file_is_ignored(self, vault):
        d = vault.root / ".vault-lock.d"
        d.mkdir()
        (d / "codex.json").write_text("{kaputt", encoding="utf-8")
        vault.append("wiki/log/2026-11.md", "- ok")
        assert policies.lock_status(vault.root)["locks"] == []

    def test_lock_dir_not_reachable_via_tools(self, vault):
        with pytest.raises(PermissionError):
            vault.create(".vault-lock.d/codex.md", "x")
        with pytest.raises(PermissionError):
            vault.read(".vault-lock.d/claude.json")


class TestLockToolsOverMcp:
    def _run(self, tool, **args):
        from obsidian_mcp.server import mcp

        res = asyncio.run(mcp.call_tool(tool, args))
        return res[1] if isinstance(res, tuple) else res

    def test_roundtrip(self, vault):
        from obsidian_mcp.server import set_vault

        set_vault(vault)
        out = self._run("lock_scope", scope="wiki/log", purpose="Merge")
        assert out["scope"] == "wiki/log" and out["agent"] == "claude"
        status = self._run("lock_status")
        assert status["locks"][0]["purpose"] == "Merge"
        assert self._run("refresh_lock", ttl_min=45)["refreshed"][0]["scope"] == "wiki/log"
        assert self._run("release_lock")["released"] == ["wiki/log"]
        assert self._run("lock_status")["locks"] == []

    def test_refusal_is_named(self, vault, monkeypatch):
        from mcp.shared.exceptions import McpError

        from obsidian_mcp.server import mcp, set_vault

        set_vault(vault)
        policies.lock_scope(vault.root, "wiki/log", actor="codex")
        with pytest.raises(Exception) as exc:
            asyncio.run(mcp.call_tool("append_to_note", {"path": "wiki/log/2026-11.md", "content": "x"}))
        cause = exc.value if isinstance(exc.value, McpError) else exc.value.__cause__
        assert cause.error.message.startswith("Blocked by VAULT_LOCK: 'wiki/log/2026-11.md' is locked by 'codex'")


class TestNoEmptyFrontmatter:
    def test_append_to_plain_note_keeps_it_plain(self, vault):
        vault.append("wiki/log/2026-11.md", "- zweiter Eintrag")
        text = (vault.root / "wiki/log/2026-11.md").read_text(encoding="utf-8")
        assert text == "- erster Eintrag\n- zweiter Eintrag"
        assert not text.startswith("---")

    def test_create_and_patch_without_metadata(self, vault):
        vault.create("Inbox/neu.md", "# Neu\n\nText")
        assert (vault.root / "Inbox/neu.md").read_text(encoding="utf-8") == "# Neu\n\nText"
        vault.patch_section("Inbox/neu.md", "Neu", "append", "mehr")
        assert not (vault.root / "Inbox/neu.md").read_text(encoding="utf-8").startswith("---")

    def test_metadata_still_written(self, vault):
        vault.create("Inbox/tag.md", "# T", tags=["a"])
        assert (vault.root / "Inbox/tag.md").read_text(encoding="utf-8").startswith("---\ntags:")
