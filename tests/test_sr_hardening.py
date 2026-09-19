"""Regression tests: search_and_replace hardening + uid logging.

Covers:
- S2 ReDoS: user-supplied regex runs with a 2s execution timeout (regex
  module). The classic exploit burned 102s of CPU; stdlib re needs 22s for
  just 28 chars of '^(a|a)*$'.
- B4: literal replace mode treats `replace` as plain text — no group-ref
  injection, no backslash-escape crashes ('C:\\Users\\x').
- S6: the write log records the note's uid (was always '').
"""
import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp.vault import Vault  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


def last_log_entry(tmp_path):
    shard = tmp_path / ".vault-write-log.d"
    files = list(shard.rglob("*.jsonl"))
    assert files, "write log missing"
    return json.loads(files[0].read_text(encoding="utf-8").strip().splitlines()[-1])


class TestS2RedosTimeout:
    def test_catastrophic_pattern_times_out(self, vault):
        vault.create("Inbox/bomb.md", "a" * 40 + "b")
        t0 = time.monotonic()
        with pytest.raises(ValueError, match="[Tt]imed ?out|[Tt]imeout"):
            vault.search_and_replace("Inbox/bomb.md", r"^(a|a)*$", "X", use_regex=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 10, f"timeout took too long: {elapsed:.1f}s"

    def test_regex_mode_still_works(self, vault):
        vault.create("Inbox/rx.md", "foo1 foo2 foo12")
        new, n = vault.search_and_replace("Inbox/rx.md", r"foo(\d)", r"bar\1", use_regex=True)
        assert n == 3
        assert new == "bar1 bar2 bar12"

    def test_invalid_regex_gives_value_error(self, vault):
        vault.create("Inbox/rx2.md", "x")
        with pytest.raises(ValueError, match="Invalid regex"):
            vault.search_and_replace("Inbox/rx2.md", "(unclosed", "y", use_regex=True)

    def test_pattern_length_cap(self, vault):
        vault.create("Inbox/cap.md", "x")
        with pytest.raises(ValueError, match="too long"):
            vault.search_and_replace("Inbox/cap.md", "a" * 501, "y", use_regex=True)

    def test_oversized_note_rejected(self, vault):
        # S13: notes above the 2 MiB cap are rejected outright (creation
        # calls load_note, which enforces the cap) — S&R never sees them.
        with pytest.raises(ValueError, match="size cap|too large"):
            vault.create("Inbox/huge.md", "x" * (2 * 1024 * 1024 + 10))


class TestB4LiteralReplace:
    def test_windows_path_replace(self, vault):
        vault.create("Inbox/b4a.md", "path: OLD")
        new, n = vault.search_and_replace("Inbox/b4a.md", "OLD", "C:" + "\\" + "Users" + "\\" + "new")
        assert n == 1
        assert new == "path: C:" + "\\" + "Users" + "\\" + "new"

    def test_group_ref_not_processed_in_literal_mode(self, vault):
        vault.create("Inbox/b4b.md", "hello world")
        new, _ = vault.search_and_replace("Inbox/b4b.md", "world", "\\1evil")
        assert new == "hello \\1evil"

    def test_find_with_regex_metachars_is_literal(self, vault):
        vault.create("Inbox/b4c.md", "1+1=2 and price 5.00")
        new, n = vault.search_and_replace("Inbox/b4c.md", "5.00", "six")
        assert n == 1
        assert "5.00" not in new

    def test_case_insensitive_literal(self, vault):
        vault.create("Inbox/b4d.md", "Hello HELLO hello")
        new, n = vault.search_and_replace("Inbox/b4d.md", "hello", "hi", case_sensitive=False)
        assert n == 3
        assert new == "hi hi hi"


class TestS6UidLogging:
    def test_create_wiki_logs_uid(self, vault, tmp_path):
        vault.create("wiki/concepts/u.md", "---\nuid: cpt-u1\n---\n# U")
        e = last_log_entry(tmp_path)
        assert e["uid"] == "cpt-u1"

    def test_append_logs_existing_uid(self, vault, tmp_path):
        vault.create("wiki/concepts/u2.md", "---\nuid: cpt-u2\n---\n# U2")
        vault.append("wiki/concepts/u2.md", "extra")
        e = last_log_entry(tmp_path)
        assert e["uid"] == "cpt-u2"

    def test_sr_logs_uid(self, vault, tmp_path):
        vault.create("wiki/concepts/u3.md", "---\nuid: cpt-u3\n---\n# U3\nalpha")
        vault.search_and_replace("wiki/concepts/u3.md", "alpha", "beta")
        e = last_log_entry(tmp_path)
        assert e["uid"] == "cpt-u3"

    def test_no_uid_stays_empty_string(self, vault, tmp_path):
        vault.create("Inbox/plain.md", "# plain")
        e = last_log_entry(tmp_path)
        assert e["uid"] == ""

    def test_delete_logs_uid_of_deleted(self, vault, tmp_path):
        vault.create("wiki/concepts/u4.md", "---\nuid: cpt-u4\n---\n# U4")
        vault.delete("wiki/concepts/u4.md")
        e = last_log_entry(tmp_path)
        assert e["uid"] == "cpt-u4", "delete must log the deleted note's uid"

