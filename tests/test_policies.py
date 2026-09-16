"""Tests for vault write policies (policies.py) and Vault integration."""
import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import policies
from obsidian_mcp.vault import Vault

# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "concepts").mkdir()
    (tmp_path / "raw").mkdir()
    (tmp_path / "06 Archive").mkdir()
    (tmp_path / "wiki" / "concepts" / "Alpha.md").write_text(
        "---\ntitel: Alpha\ntyp: konzept\nstatus: aktiv\nuid: cpt-alpha\n---\n"
        "# Alpha\n\nLinks to [[Beta]].\n",
        encoding="utf-8",
    )
    (tmp_path / "raw" / "source.md").write_text(
        "---\ntitel: Raw Source\n---\nOriginal content.\n", encoding="utf-8"
    )
    return v


# ---- 1. write log ------------------------------------------------------


class TestWriteLog:
    def test_create_appends_json_line(self, vault, tmp_path):
        vault.create("Notes/new.md", "# Hello", tags=["t"])
        log = tmp_path / ".vault-write-log.d" / "unknown.jsonl"
        assert log.exists()
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["action"] == "create"
        assert entry["path"] == "Notes/new.md"
        assert entry["layer"] == "para"
        assert entry["actor"] == "unknown"

    def test_actor_from_env(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_ACTOR", "windows")
        vault.create("Notes/env.md", "# Env")
        shard = tmp_path / ".vault-write-log.d" / "windows.jsonl"
        entry = json.loads(shard.read_text().strip().splitlines()[-1])
        assert entry["actor"] == "windows"

    def test_legacy_mode_appends_single_file(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_WRITE_LOG_SHARDS", "0")
        vault.create("Notes/legacy1.md", "# L1")
        vault.create("Notes/legacy2.md", "# L2")
        log = tmp_path / ".vault-write-log.jsonl"
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(line)["path"] for line in lines] == ["Notes/legacy1.md", "Notes/legacy2.md"]

    def test_opt_out_disables_log(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_WRITE_LOG", "0")
        vault.create("Notes/nolog.md", "# NoLog")
        assert not (tmp_path / ".vault-write-log.jsonl").exists()

    def test_layers_classified(self, vault, tmp_path):
        vault.create("raw/articles/a.md", "# A")
        vault.create("wiki/concepts/b.md", "---\nuid: cpt-b\n---\n# B")
        lines = (tmp_path / ".vault-write-log.d" / "unknown.jsonl").read_text().strip().splitlines()
        layers = {json.loads(line)["layer"] for line in lines}
        assert layers == {"raw", "wiki"}

    def test_shard_per_actor(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_ACTOR", "career")
        vault.create("Notes/a.md", "# A")
        monkeypatch.setenv("VAULT_ACTOR", "infra")
        vault.create("Notes/b.md", "# B")
        shard_dir = tmp_path / ".vault-write-log.d"
        assert sorted(p.name for p in shard_dir.iterdir()) == ["career.jsonl", "infra.jsonl"]
        a = json.loads((shard_dir / "career.jsonl").read_text().strip())
        b = json.loads((shard_dir / "infra.jsonl").read_text().strip())
        assert a["actor"] == "career" and a["path"] == "Notes/a.md"
        assert b["actor"] == "infra" and b["path"] == "Notes/b.md"

    def test_shards_opt_out_writes_legacy_file(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_WRITE_LOG_SHARDS", "0")
        vault.create("Notes/legacy.md", "# Legacy")
        assert (tmp_path / ".vault-write-log.jsonl").exists()
        assert not (tmp_path / ".vault-write-log.d").exists()

    def test_shard_fallback_on_unwritable_dir(self, vault, tmp_path, monkeypatch):
        # shard dir exists as a FILE -> mkdir fails -> fallback to legacy file
        (tmp_path / ".vault-write-log.d").write_text("blocked", encoding="utf-8")
        vault.create("Notes/fb.md", "# FB")
        assert (tmp_path / ".vault-write-log.jsonl").exists()
        lines = (tmp_path / ".vault-write-log.jsonl").read_text().strip().splitlines()
        assert json.loads(lines[-1])["path"] == "Notes/fb.md"

    def test_actor_sanitized_for_filename(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_ACTOR", "windows/..\\evil name")
        vault.create("Notes/s.md", "# S")
        shard_dir = tmp_path / ".vault-write-log.d"
        names = [p.name for p in shard_dir.iterdir()]
        assert len(names) == 1
        # Security invariant: no path separators, no leading dot -> no traversal
        assert "/" not in names[0] and "\\" not in names[0] and not names[0].startswith(".")
        assert " " not in names[0]


# ---- 2. raw immutability -----------------------------------------------


class TestRawImmutable:
    def test_update_raw_blocked(self, vault):
        with pytest.raises(policies.PolicyViolationError, match="immutable"):
            vault.update("raw/source.md", "# changed")

    def test_delete_raw_blocked(self, vault):
        with pytest.raises(policies.PolicyViolationError, match="immutable"):
            vault.delete("raw/source.md")

    def test_create_in_raw_allowed(self, vault, tmp_path):
        vault.create("raw/articles/new.md", "# New Raw")
        assert (tmp_path / "raw" / "articles" / "new.md").exists()

    def test_opt_out_allows_raw_update(self, vault, monkeypatch):
        monkeypatch.setenv("RAW_IMMUTABLE", "0")
        n = vault.update("raw/source.md", "# changed")
        assert "changed" in n.content

    def test_wiki_still_mutable(self, vault):
        vault.update("wiki/concepts/Alpha.md", "# Alpha\n\nupdated")
        assert True  # no exception


# ---- 3. archive helper --------------------------------------------------


class TestArchiveHelper:
    def test_archive_target(self):
        assert policies.archive_target("wiki/concepts/x.md", "06 Archive") == "06 Archive/x.md"


# ---- 4. lock ------------------------------------------------------------


class TestLock:
    def test_foreign_fresh_lock_blocks(self, vault, tmp_path):
        policies.acquire_lock(tmp_path, scope="wiki", actor="server-agent")
        with pytest.raises(policies.PolicyViolationError, match="locked by"):
            vault.create("Notes/x.md", "# X")

    def test_own_lock_allows(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT_ACTOR", "me")
        policies.acquire_lock(tmp_path, actor="me")
        vault.create("Notes/y.md", "# Y")  # no exception

    def test_stale_lock_ignored(self, vault, tmp_path):
        lock = tmp_path / ".vault-lock"
        lock.write_text(json.dumps({"agent": "other", "started": "2020-01-01"}), encoding="utf-8")
        old = time.time() - 3600  # 1h
        import os as _os
        _os.utime(lock, (old, old))
        vault.create("Notes/z.md", "# Z")  # no exception

    def test_release_lock(self, tmp_path):
        policies.acquire_lock(tmp_path, actor="me")
        assert policies.release_lock(tmp_path, actor="me") is True
        assert not (tmp_path / ".vault-lock").exists()


# ---- 5. wiki index upkeep ----------------------------------------------


class TestWikiIndex:
    def test_wiki_create_updates_index(self, vault, tmp_path):
        vault.create("wiki/concepts/Beta.md", "---\ntitel: Beta\ntyp: konzept\nstatus: aktiv\nuid: cpt-beta\n---\n# Beta")
        idx = json.loads((tmp_path / "wiki" / "index.json").read_text(encoding="utf-8"))
        paths = [p["path"] for p in idx["pages"]]
        assert "concepts/Beta.md" in paths
        assert idx["total_pages"] == len(idx["pages"])

    def test_para_write_does_not_touch_index(self, vault, tmp_path):
        vault.create("Notes/plain.md", "# Plain")
        assert not (tmp_path / "wiki" / "index.json").exists()

    def test_archive_removes_entry(self, vault, tmp_path):
        vault.create("wiki/concepts/Gamma.md", "---\nuid: cpt-gamma\ntitel: Gamma\n---\n# Gamma")
        policies.update_wiki_index(
            tmp_path, "wiki/concepts/Gamma.md", "Gamma", "", "", action="archive"
        )
        idx = json.loads((tmp_path / "wiki" / "index.json").read_text(encoding="utf-8"))
        assert all(p["path"] != "concepts/Gamma.md" for p in idx["pages"])

    def test_group_sort_order(self, tmp_path):
        for p, t in [
            ("wiki/log/2026-08.md", "Log"),
            ("wiki/concepts/A.md", "A"),
            ("wiki/entities/B.md", "B"),
            ("wiki/sources/C.md", "C"),
            ("wiki/other/D.md", "D"),
        ]:
            policies.update_wiki_index(tmp_path, p, t, "", "")
        idx = json.loads((tmp_path / "wiki" / "index.json").read_text(encoding="utf-8"))
        groups = [p["path"].split("/")[0] for p in idx["pages"]]
        assert groups.index("concepts") < groups.index("entities") < groups.index("sources") < groups.index("log")


# ---- 6. link check ------------------------------------------------------


class TestLinkCheck:
    def test_extract_wikilinks_ignores_code(self):
        content = "Text [[Good]] link.\n\n```python\ns = '[[Bad]]'\n```\nInline `[[AlsoBad]]` code."
        links = policies.extract_wikilinks(content)
        assert links == ["Good"]

    def test_check_links_flags_unknown(self):
        broken = policies.check_links(
            pathlib.Path("."), "See [[Existing]] and [[Ghost]].", {"Existing"}
        )
        assert broken == ["Ghost"]

    def test_vault_check_note_links(self, vault, tmp_path):
        (tmp_path / "Beta.md").write_text("---\ntitel: Beta\n---\n# Beta\n", encoding="utf-8")
        broken = vault.check_note_links("wiki/concepts/Alpha.md")
        assert broken == []  # Beta now exists

    def test_opt_out_disables_check(self, monkeypatch):
        monkeypatch.setenv("VAULT_LINK_CHECK", "0")
        assert policies.check_links(pathlib.Path("."), "[[Nope]]", set()) == []


# ---- 3b. archive_note tool ---------------------------------------------


class TestArchiveNoteTool:
    def test_archive_moves_and_reports_backlinks(self, vault, tmp_path):
        import asyncio

        from obsidian_mcp.server import mcp, set_vault
        set_vault(vault)
        # Note that links TO Alpha
        vault.create("Notes/linker.md", "See [[Alpha]].")
        result = asyncio.run(mcp.call_tool("archive_note", {"path": "wiki/concepts/Alpha.md"}))
        import json as _json
        data = _json.loads(result[0][0].text)
        assert data["archived"] == "06 Archive/Alpha.md"
        assert "Notes/linker.md" in data["backlinks"]
        assert (tmp_path / "06 Archive" / "Alpha.md").exists()
        assert not (tmp_path / "wiki" / "concepts" / "Alpha.md").exists()

    def test_archive_refuses_overwrite(self, vault, tmp_path):
        import asyncio

        from obsidian_mcp.server import mcp, set_vault
        set_vault(vault)
        (tmp_path / "06 Archive" / "Alpha.md").write_text("existing", encoding="utf-8")
        try:
            asyncio.run(mcp.call_tool("archive_note", {"path": "wiki/concepts/Alpha.md"}))
            assert False, "should have raised"
        except Exception as e:
            assert "exists" in str(e).lower()

    def test_archive_updates_wiki_index(self, vault, tmp_path):
        import asyncio
        import json as _json

        from obsidian_mcp.server import mcp, set_vault
        set_vault(vault)
        # erst index aufbauen
        vault.update("wiki/concepts/Alpha.md", "---\ntitel: Alpha\ntyp: konzept\nstatus: aktiv\nuid: cpt-alpha\n---\n# Alpha")
        asyncio.run(mcp.call_tool("archive_note", {"path": "wiki/concepts/Alpha.md"}))
        idx = _json.loads((tmp_path / "wiki" / "index.json").read_text(encoding="utf-8"))
        assert all(p["path"] != "concepts/Alpha.md" for p in idx["pages"])
