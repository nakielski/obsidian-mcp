"""Regression tests: path resolution hardening.

Covers the security findings from the 2026-09 multi-loop review:
- S1: sandbox escape via 'Inbox/..' (suffix appended after containment check)
- N1: policy bypass via './raw/...', ' raw/...' (leading whitespace), tab,
  'raw//...' spellings — policies must classify the NORMALIZED path
- S3: append() on an existing raw/ note must be blocked (immutability)
- B5: layer classification must be case-insensitive (RAW/, Wiki/)
- B11: rejected writes must not leave a write-log entry
- S7: search_and_replace must keep the note in the search index
"""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import policies
from obsidian_mcp.vault import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


class TestS1SuffixEscape:
    """S1: 'Inbox/..' must not escape the vault via suffix handling."""

    def test_inbox_dotdot_rejected_and_no_outside_file(self, vault, tmp_path):
        outside = tmp_path.parent / (tmp_path.name + ".md")
        with pytest.raises(PermissionError):
            vault.append("Inbox/..", "ESCAPED PAYLOAD")
        assert not outside.exists(), "write escaped the vault sandbox"

    def test_create_inbox_dotdot_rejected(self, vault, tmp_path):
        outside = tmp_path.parent / (tmp_path.name + ".md")
        with pytest.raises(PermissionError):
            vault.create("Inbox/..", "ESCAPED")
        assert not outside.exists()

    def test_read_inbox_dotdot_rejected(self, vault):
        with pytest.raises(PermissionError):
            vault.read("Inbox/..")

    def test_delete_inbox_dotdot_rejected(self, vault):
        with pytest.raises(PermissionError):
            vault.delete("Inbox/..")

    def test_rejected_write_leaves_no_log_entry(self, vault, tmp_path):
        """B11: a refused write must not appear in the write log."""
        shard = tmp_path / ".vault-write-log.d"
        before = list(shard.rglob("*.jsonl")) if shard.exists() else []
        with pytest.raises(PermissionError):
            vault.create("Inbox/..", "x")
        after = list(shard.rglob("*.jsonl")) if shard.exists() else []
        assert before == after


class TestN1PolicyBypassSpellings:
    """N1: policies must see the normalized path, not the raw input."""

    BYPASS_SPELLINGS = [
        "./raw/x.md",
        " raw/x.md",
        "\traw/x.md",
        ".\\raw\\x.md",
    ]

    @pytest.mark.parametrize("spelling", BYPASS_SPELLINGS)
    def test_update_raw_bypass_blocked(self, vault, spelling):
        vault.create("raw/x.md", "# original")
        # Either exception is fine — the write must be blocked.
        with pytest.raises((policies.PolicyViolationError, PermissionError)):
            vault.update(spelling, "# tampered")

    @pytest.mark.parametrize("spelling", BYPASS_SPELLINGS)
    def test_delete_raw_bypass_blocked(self, vault, spelling):
        vault.create("raw/x.md", "# original")
        with pytest.raises((policies.PolicyViolationError, PermissionError)):
            vault.delete(spelling)

    def test_create_wiki_nouid_bypass_blocked(self, vault):
        """'./wiki/...' spellings are hard-rejected (never reach uid check)."""
        with pytest.raises(PermissionError):
            vault.create("./wiki/nouid.md", "# no uid")

    def test_normalized_path_used_for_logging_and_index(self, vault, tmp_path):
        """N2: the write log and note path use the normalized rel path."""
        vault.create("Inbox//norm.md", "# normalized")
        shard = tmp_path / ".vault-write-log.d"
        files = list(shard.rglob("*.jsonl"))
        assert files, "write log missing"
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[-1])
        assert entry["path"] == "Inbox/norm.md", f"log has raw path: {entry['path']}"

    def test_double_slash_normalized(self, vault):
        n = vault.create("Inbox//double.md", "# d")
        assert n.path == "Inbox/double.md"


class TestB5CaseInsensitiveLayers:
    """B5: layer classification is case-insensitive (NTFS/APFS parity)."""

    @pytest.mark.parametrize("spelling", ["raw/x.md", "RAW/x.md", "Raw/x.md"])
    def test_classify_raw_casefold(self, spelling):
        assert policies.classify_layer(spelling) == "raw"

    @pytest.mark.parametrize("spelling", ["wiki/a.md", "WIKI/a.md", "Wiki/a.md"])
    def test_classify_wiki_casefold(self, spelling):
        assert policies.classify_layer(spelling) == "wiki"

    def test_update_raw_uppercase_blocked(self, vault):
        vault.create("raw/case.md", "# original")
        with pytest.raises(policies.PolicyViolationError):
            vault.update("RAW/case.md", "# tampered")


class TestS3AppendImmutability:
    """S3: append() to an EXISTING raw/ note is blocked; creating is allowed."""

    def test_append_existing_raw_blocked(self, vault):
        vault.create("raw/keep.md", "# original")
        with pytest.raises(policies.PolicyViolationError):
            vault.append("raw/keep.md", "TAMPERED")

    def test_append_creates_new_raw_allowed(self, vault, tmp_path):
        note, created = vault.append("raw/fresh.md", "# fresh source")
        assert created is True
        assert (tmp_path / "raw" / "fresh.md").exists()


class TestS7SearchAndReplaceIndex:
    """S7: search_and_replace keeps the note in the Whoosh index."""

    def test_sr_reindexes_note(self, vault):
        vault.create("wiki/concepts/sr.md", "---\nuid: cpt-sr\n---\n# SR\nalpha beta")
        ix = vault._init_index()
        if ix is None:
            pytest.skip("whoosh not available")
        before = ix.count()
        vault.search_and_replace("wiki/concepts/sr.md", "alpha", "gamma")
        assert ix.count() == before, "note dropped from index by S&R"
        hits = ix.search("gamma")
        assert any(h["path"] == "wiki/concepts/sr.md" for h in hits), "new term not indexed"


class TestSuffixRecheck:
    """The containment re-check AFTER suffix handling is load-bearing (S1).

    '.', './' and symlinks-to-'.' pass the segment reject and the FIRST
    containment check (root is relative to itself); only the re-check after
    with_suffix('.md') catches root.with_suffix escaping to the parent dir.
    """

    def test_dot_maps_to_rejected_not_root_md(self, vault, tmp_path):
        outside = tmp_path.parent / (tmp_path.name + ".md")
        with pytest.raises(PermissionError):
            vault.append(".", "ESCAPED")
        assert not outside.exists()

    def test_slash_dot_rejected(self, vault, tmp_path):
        outside = tmp_path.parent / (tmp_path.name + ".md")
        with pytest.raises(PermissionError):
            vault.append("./", "ESCAPED")
        assert not outside.exists()

    def test_symlink_to_dot_rejected(self, vault, tmp_path):
        (tmp_path / "selfdir").symlink_to(".", target_is_directory=True)
        outside = tmp_path.parent / (tmp_path.name + ".md")
        with pytest.raises(PermissionError):
            vault.append("selfdir", "ESCAPED")
        assert not outside.exists()

    def test_inbox_dot_rejected_not_silent_inbox_md(self, vault, tmp_path):
        """'Inbox/.' must be rejected, not silently collapse to Inbox.md."""
        with pytest.raises(PermissionError):
            vault.append("Inbox/.", "x")
        assert not (tmp_path / "Inbox.md").exists()


class TestArchiveNormalization:
    """F1/F2: archive_note must use the normalized path and enforce policies."""

    def test_archive_raw_blocked(self, vault, monkeypatch):
        from obsidian_mcp import server
        server.set_vault(vault)
        vault.create("raw/archive-me.md", "# source")
        with pytest.raises(Exception, match="immutable"):
            asyncio.run(server.mcp.call_tool(
                "archive_note", {"path": "raw/archive-me.md"}
            ))

    def test_archive_normalized_logging(self, vault, tmp_path, monkeypatch):
        from obsidian_mcp import server
        server.set_vault(vault)
        vault.create("wiki/concepts/Archive Me.md", "---\nuid: cpt-a\n---\n# Archive Me")
        asyncio.run(server.mcp.call_tool(
            "archive_note", {"path": "wiki//concepts/Archive Me.md"}
        ))
        shard = tmp_path / ".vault-write-log.d"
        lines = list(shard.rglob("*.jsonl"))[0].read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[-1])
        assert entry["path"] == "wiki/concepts/Archive Me.md", f"raw path in log: {entry['path']}"
        assert entry["layer"] == "wiki", f"wrong layer: {entry['layer']}"

    def test_archive_removes_from_index(self, vault):
        vault.create("wiki/concepts/Gone.md", "---\nuid: cpt-g\n---\n# Gone")
        ix = vault._init_index()
        if ix is None:
            pytest.skip("whoosh not available")
        before = ix.count()
        vault.delete("wiki/concepts/Gone.md")
        assert ix.count() == before - 1


class TestDailyNoteRawCarveOut:
    """F3: daily-note journaling keeps working under policy-protected layers."""

    def test_second_append_to_raw_daily_works(self, vault, tmp_path, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_DAILY_DIR", "raw/Daily")
        n1, c1 = vault.append("raw/Daily/2026-09-19.md", "- first entry")
        n2, c2 = vault.append("raw/Daily/2026-09-19.md", "- second entry")
        assert c1 is True and c2 is False
        content = (tmp_path / "raw" / "Daily" / "2026-09-19.md").read_text(encoding="utf-8")
        assert "first entry" in content and "second entry" in content

    def test_other_raw_notes_still_immutable_to_append(self, vault, monkeypatch):
        monkeypatch.setenv("OBSIDIAN_DAILY_DIR", "raw/Daily")
        vault.create("raw/keep.md", "# original")
        with pytest.raises(policies.PolicyViolationError):
            vault.append("raw/keep.md", "TAMPERED")


class TestBomAndUnicodeWhitespace:
    """BOM/zero-width prefixes create genuinely different dirs — acceptable,
    but they must never classify as a protected layer."""

    def test_bom_prefix_not_raw(self):
        assert policies.classify_layer("\ufeffraw/x.md") == "para"

    def test_zwsp_prefix_not_raw(self):
        assert policies.classify_layer("\u200braw/x.md") == "para"
