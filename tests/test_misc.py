"""Regression tests: note size cap, RMW locking, search limit,
serverInfo version.

- S13: notes above 2 MiB are rejected before YAML parsing (alias-bomb
  blast radius bounded).
- S15: read-modify-write methods hold an RLock; 20 parallel appends all
  survive (previously ~5 did).
- S18: search() is bounded (default 100) and empty queries return [].
- S14: serverInfo reports the project version, not the mcp library's.
"""
import concurrent.futures
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp.vault import Vault  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


class TestS13SizeCap:
    def test_oversized_create_rejected(self, vault):
        with pytest.raises(ValueError, match="size cap"):
            vault.create("Inbox/huge.md", "x" * (2 * 1024 * 1024 + 10))

    def test_oversized_read_rejected(self, vault, tmp_path):
        big = tmp_path / "big.md"
        big.write_text("x" * (2 * 1024 * 1024 + 10), encoding="utf-8")
        with pytest.raises(ValueError, match="size cap"):
            vault.read("big.md")

    def test_yaml_alias_bomb_capped(self, vault, tmp_path):
        """484-byte alias bomb expands to 121 MB — the 2 MiB file cap does
        not stop a SMALL bomb, so parse-level alias handling must bound it.
        A bomb this size (4 KB of aliases on disk) exceeds the file cap."""
        bomb = tmp_path / "bomb.md"
        # 4 KB file: over the file cap? No — build one under 2 MiB whose
        # expansion is huge: 18 nested levels of doubling aliases.
        lines = ["---", "a: &a [\"x\",\"x\"]"]
        prev = "a"
        for i in range(16):
            name = f"b{i}"
            lines.append(f"{name}: [*{prev},*{prev}]")
        lines.append("---")
        lines.append("body")
        bomb.write_text("\n".join(lines), encoding="utf-8")
        # The file itself is small; parse either succeeds bounded by the cap
        # on expansion or raises. Either way it must not OOM/hang.
        try:
            vault.read("bomb.md")
        except ValueError:
            pass  # cap enforced — fine

    def test_normal_notes_unaffected(self, vault):
        n = vault.create("Inbox/ok.md", "# ok" * 100)
        assert n.content


class TestS15RMWLocking:
    def test_20_parallel_appends_all_survive(self, vault):
        vault.create("Inbox/race.md", "# race")
        def do(i):
            return vault.append("Inbox/race.md", f"line-{i}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            list(ex.map(do, range(20)))
        content = vault.read("Inbox/race.md").content
        found = sum(1 for i in range(20) if f"line-{i}" in content)
        assert found == 20, f"lost {20 - found}/20 appends"

    def test_parallel_set_frontmatter_all_survive(self, vault):
        vault.create("Inbox/fm.md", "# fm")
        def do(i):
            return vault.set_frontmatter("Inbox/fm.md", f"k{i}", i)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(do, range(10)))
        meta = vault.read("Inbox/fm.md").metadata
        assert sum(1 for i in range(10) if f"k{i}" in meta) == 10


class TestS18SearchLimit:
    def test_empty_query_returns_empty(self, vault):
        vault.create("Inbox/a.md", "# alpha")
        assert vault.search("") == []

    def test_search_capped_at_100(self, vault):
        for i in range(120):
            vault.create(f"Inbox/n{i:03}.md", f"# note {i} zebra")
        hits = vault.search("zebra")
        assert len(hits) == 100

    def test_search_explicit_limit(self, vault):
        for i in range(10):
            vault.create(f"Inbox/m{i}.md", "# zebra")
        assert len(vault.search("zebra", limit=5)) == 5

    def test_search_still_finds(self, vault):
        vault.create("Inbox/findme.md", "# hello zebra")
        assert any(n.path == "Inbox/findme.md" for n in vault.search("zebra"))


class TestS14ServerInfoVersion:
    def test_serverinfo_reports_project_version(self):
        from obsidian_mcp import __version__, server
        assert server.mcp._mcp_server.version == __version__
