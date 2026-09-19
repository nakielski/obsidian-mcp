"""Regression tests: deny-list for internal bookkeeping files.

Covers:
- The audit log (.vault-write-log.jsonl / .vault-write-log.d) must not be
  readable or writable via tools — 'TAMPER OK' via search_and_replace and
  'AUDIT-SPY' via read_note were live exploits.
- .obsidian/ is app config: delete on workspace.json, and WRITE access to
  .obsidian/plugins/<id>/main.js equals code execution on the next Obsidian
  start.
- .vault-lock, .obsidian-mcp-index/, wiki/index.json: derived/internal data.
"""
import asyncio
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp.vault import Vault  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


DENIED_PATHS = [
    ".vault-write-log.jsonl",
    ".vault-write-log.d/reviewer.jsonl",
    ".VAULT-WRITE-LOG.JSONL",  # casefold
    "Inbox/.vault-lock",       # smuggled as later component
    ".obsidian-mcp-index/x",
    "wiki/index.json",
    "wiki/Index.json",
    ".obsidian/workspace.json",
    ".Obsidian/plugins/x/main.js",
]

DENIED_FOLDERS = [
    ".obsidian",
    ".OBSIDIAN",
    ".vault-write-log.d",
    "Inbox/.obsidian-mcp-index",
]


class TestDenyListPaths:
    @pytest.mark.parametrize("path", DENIED_PATHS)
    def test_read_denied(self, vault, path):
        with pytest.raises(PermissionError):
            vault.read(path)

    @pytest.mark.parametrize("path", DENIED_PATHS)
    def test_create_denied(self, vault, path):
        with pytest.raises(PermissionError):
            vault.create(path, "# x")

    @pytest.mark.parametrize("path", DENIED_PATHS)
    def test_update_denied(self, vault, path):
        with pytest.raises(PermissionError):
            vault.update(path, "# x")

    @pytest.mark.parametrize("path", DENIED_PATHS)
    def test_delete_denied(self, vault, path):
        with pytest.raises(PermissionError):
            vault.delete(path)

    @pytest.mark.parametrize("path", DENIED_PATHS)
    def test_search_and_replace_denied(self, vault, path):
        with pytest.raises(PermissionError):
            vault.search_and_replace(path, "a", "b")


class TestDenyListFolders:
    @pytest.mark.parametrize("folder", DENIED_FOLDERS)
    def test_list_notes_denied(self, vault, folder):
        with pytest.raises(PermissionError):
            vault.list_notes(folder)

    @pytest.mark.parametrize("folder", DENIED_FOLDERS)
    def test_list_note_paths_denied(self, vault, folder):
        with pytest.raises(PermissionError):
            vault.list_note_paths(folder)


class TestDenyListDoesNotBreakInternals:
    def test_write_log_still_written_by_create(self, vault, tmp_path):
        vault.create("Inbox/logged.md", "# x")
        shards = list((tmp_path / ".vault-write-log.d").rglob("*.jsonl"))
        assert shards, "internal write-log broken by deny-list"

    def test_wiki_index_still_updated(self, vault, tmp_path):
        vault.create("wiki/concepts/x.md", "---\nuid: cpt-x\n---\n# X")
        idx = tmp_path / "wiki" / "index.json"
        assert idx.exists(), "internal wiki index write broken by deny-list"

    def test_index_json_as_note_name_still_ok_elsewhere(self, vault):
        """A note literally named 'index-something.md' is fine; only the exact
        wiki/index.json bookkeeping file is denied."""
        n = vault.create("Inbox/index.json.md", "# not the bookkeeping file")
        assert n.path == "Inbox/index.json.md"


class TestSuffixQuirkInteraction:
    """A5: suffix handling must not bypass the deny-list via '.md' appending
    or the dotfile-suffix quirk (Path('x.jsonl').suffix == '.jsonl')."""

    def test_no_suffix_appending_rescue(self, vault):
        with pytest.raises(PermissionError):
            vault.read(".vault-lock" + " ")  # strip() cleans, still denied

    def test_dotfile_suffix_quirk(self, vault):
        # '.vault-lock' has suffix '' — with_suffix('.md') would NOT touch it.
        with pytest.raises(PermissionError):
            vault.update(".vault-lock", "# tampered")


class TestAuditLogTamperViaServerTool:
    """S5 end-to-end: the live exploit was search_and_replace on the log."""

    def test_sr_on_log_denied_end_to_end(self, vault, tmp_path):
        from obsidian_mcp import server

        server.set_vault(vault)
        vault.create("Inbox/trigger.md", "# trigger")
        with pytest.raises(Exception, match="internal bookkeeping"):
            asyncio.run(server.mcp.call_tool(
                "search_and_replace",
                {"path": ".vault-write-log.jsonl", "find": "create", "replace": "!!!"},
            ))


class TestCCReviewF1Symlinks:
    """Symlinks must not bypass the deny-list."""

    def test_symlink_to_audit_log_blocked(self, vault, tmp_path):
        vault.create("Inbox/x.md", "# x")
        os.symlink(tmp_path / ".vault-write-log.jsonl", tmp_path / "Inbox" / "loglink.jsonl")
        with pytest.raises(PermissionError):
            vault.read("Inbox/loglink.jsonl")
        with pytest.raises(PermissionError):
            vault.update("Inbox/loglink.jsonl", "TAMPER")

    def test_dir_symlink_to_obsidian_blocked(self, vault, tmp_path):
        (tmp_path / ".obsidian").mkdir()
        (tmp_path / "Inbox").mkdir(exist_ok=True)
        os.symlink(tmp_path / ".obsidian", tmp_path / "Inbox" / "obslink")
        with pytest.raises(PermissionError):
            vault.read("Inbox/obslink/workspace.json")
        with pytest.raises(PermissionError):
            vault.create("Inbox/obslink/plugins/evil/main.js", "// evil")

    def test_nested_obsidian_component_blocked(self, vault, tmp_path):
        deep = tmp_path / "Inbox" / "plain-obsidian" / ".obsidian"
        deep.mkdir(parents=True)
        (deep / "x.json").write_text("{}")
        with pytest.raises(PermissionError):
            vault.read("Inbox/plain-obsidian/.obsidian/x.json")


class TestCCReviewF2ListingLeak:
    def test_obsidian_md_not_in_listings(self, vault, tmp_path):
        (tmp_path / ".obsidian").mkdir()
        (tmp_path / ".obsidian" / "README.md").write_text("# plugin SECRETLEAK")
        (tmp_path / "Inbox").mkdir(exist_ok=True)
        (tmp_path / "Inbox" / "normal.md").write_text("# normal")
        paths = [n.path for n in vault.list_notes()]
        assert paths == ["Inbox/normal.md"]
        assert vault.list_note_paths() == ["Inbox/normal.md"]

    def test_write_log_shard_not_listed(self, vault, tmp_path):
        vault.create("Inbox/x.md", "# x")
        # shard dir exists with .jsonl (not .md) — but be explicit:
        (tmp_path / ".vault-write-log.d").mkdir(exist_ok=True)
        (tmp_path / ".vault-write-log.d" / "leak.md").write_text("# leak")
        assert all(".vault-write-log" not in p for p in vault.list_note_paths())


class TestCCReviewF4IndexJsonScope:
    def test_legit_index_json_elsewhere_allowed(self, vault, tmp_path):
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "index.json").write_text('{"ok":1}')
        assert vault.read("sources/index.json") is not None or True
        # read() on non-.md: suffix append makes it sources/index.json — verify:
        n = vault.read("sources/index.json")
        assert n.path == "sources/index.json"

    def test_wiki_index_json_still_denied(self, vault):
        with pytest.raises(PermissionError):
            vault.update("wiki/index.json", "{}")
