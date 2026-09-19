"""Regression tests: patch_section fences + index locking.

B1: '#' lines inside fenced code blocks are content, not headings —
patch_section previously matched them and silently replaced/destroyed code.
B3: concurrent writers to wiki/index.json lost each other's entries
(887/900 lost in the review's 3-process race); the update cycle is now
flock-guarded and atomic.
"""
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp.vault import Vault  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


class TestB1PatchSectionFences:
    def test_hash_in_code_fence_is_not_a_heading(self, vault):
        vault.create("Inbox/b1.md", "# Title\n\n## Config\n```python\n# not a heading\nx = 1\n```\n\n## Real\nkeep me\n")
        note = vault.patch_section("Inbox/b1.md", "Real", "append", "added")
        assert "# not a heading" in note.content
        assert "x = 1" in note.content
        assert "added" in note.content

    def test_hash_inside_indented_fence(self, vault):
        content = "# T\n\n```yaml\n# yaml comment\na: 1\n```\n\n## Target\nold\n"
        vault.create("Inbox/b1b.md", content)
        note = vault.patch_section("Inbox/b1b.md", "Target", "replace", "new")
        assert "# yaml comment" in note.content
        assert "old" not in note.content

    def test_tilde_fence(self, vault):
        content = "# T\n\n~~~\n# inner\n~~~\n\n## Target\nold\n"
        vault.create("Inbox/b1c.md", content)
        note = vault.patch_section("Inbox/b1c.md", "Target", "replace", "new")
        assert "# inner" in note.content

    def test_unterminated_fence_keeps_scanning(self, vault):
        """Odd fence state (unterminated block) must not break patching."""
        content = "# T\n\n```\ncode with # hash\n\n## Target\nold\n"
        vault.create("Inbox/b1d.md", content)
        # '## Target' is inside the unterminated fence → correctly NOT found
        with pytest.raises(ValueError, match="Heading not found"):
            vault.patch_section("Inbox/b1d.md", "Target", "replace", "new")

    def test_normal_headings_unaffected(self, vault):
        vault.create("Inbox/b1e.md", "# A\n\n## First\none\n\n## Second\ntwo\n")
        note = vault.patch_section("Inbox/b1e.md", "First", "replace", "ONE")
        assert "## Second" in note.content
        assert "two" in note.content


class TestB3IndexLocking:
    def test_race_three_writers_no_lost_entries(self, tmp_path):
        """Replay of the review's 3-process race (887/900 lost pre-fix).

        Each subprocess updates a DIFFERENT entry 50 times; without the
        lock, interleaved read-modify-write cycles drop entries. With
        flock around the whole cycle, all entries survive.
        """
        code = f"""
import sys
sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[1] / 'src')!r})
from pathlib import Path
from obsidian_mcp import policies
root = Path({str(tmp_path)!r})
for i in range(50):
    policies.update_wiki_index(root, f"wiki/concepts/w{{sys.argv[1]}}-{{i}}.md", f"W{{i}}", "concept", "active", uid=f"c-{{sys.argv[1]}}-{{i}}")
"""
        script = tmp_path / "racer.py"
        script.write_text(code)
        procs = [subprocess.Popen([sys.executable, str(script), str(w)]) for w in range(3)]
        for pr in procs:
            assert pr.wait(timeout=120) == 0
        data = json.loads((tmp_path / "wiki" / "index.json").read_text(encoding="utf-8"))
        paths = {p["path"] for p in data["pages"]}
        expected = {f"concepts/w{w}-{i}.md" for w in range(3) for i in range(50)}
        missing = expected - paths
        assert not missing, f"lost {len(missing)}/150 entries: {sorted(missing)[:5]}"
        assert data["total_pages"] == 150

    def test_index_stays_valid_json_after_race(self, tmp_path):
        code = f"""
import sys
sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[1] / 'src')!r})
from pathlib import Path
from obsidian_mcp import policies
root = Path({str(tmp_path)!r})
for i in range(30):
    policies.update_wiki_index(root, f"wiki/x-{{i}}.md", f"X{{i}}", "page", "active")
"""
        script = tmp_path / "racer2.py"
        script.write_text(code)
        procs = [subprocess.Popen([sys.executable, str(script)]) for _ in range(4)]
        for pr in procs:
            assert pr.wait(timeout=120) == 0
        raw = (tmp_path / "wiki" / "index.json").read_text(encoding="utf-8")
        json.loads(raw)  # must not raise — no torn files

    def test_lockfile_denied_as_note(self, vault):
        with pytest.raises(PermissionError):
            vault.read("wiki/index.json.lock")


class TestN7FencePerformance:
    """patch_section on a large note must stay fast, not O(n^2)."""

    def test_large_note_fast(self, vault, tmp_path):
        import time
        big = tmp_path / "Inbox" / "big.md"
        big.parent.mkdir(parents=True)
        big.write_text("# Title\n" + "filler\n" * 40000 + "\n## Target\nold\n", encoding="utf-8")
        t0 = time.monotonic()
        note = vault.patch_section("Inbox/big.md", "Target", "replace", "new")
        elapsed = time.monotonic() - t0
        assert "new" in note.content
        assert elapsed < 5.0, f"patch_section took {elapsed:.1f}s — O(n^2) regression?"

    def test_missed_heading_fast(self, vault, tmp_path):
        import time
        big = tmp_path / "Inbox" / "big2.md"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text("# Title\n" + "filler\n" * 40000, encoding="utf-8")
        t0 = time.monotonic()
        with pytest.raises(ValueError, match="Heading not found"):
            vault.patch_section("Inbox/big2.md", "Missing", "append", "x")
        assert time.monotonic() - t0 < 5.0

    def test_unpaired_fence_in_indented_block(self, vault):
        """N8: a lone ``` inside 4-space indented code must not toggle fences."""
        content = "# T\n\n    ```\n    code\n\n## Real\nold\n"
        vault.create("Inbox/n8.md", content)
        note = vault.patch_section("Inbox/n8.md", "Real", "replace", "new")
        assert "new" in note.content


class TestN9IndexFamilyDeny:
    """The wiki/index.json.* family cannot be clobbered via tools."""

    @pytest.mark.parametrize(
        "path",
        [
            "wiki/index.json",
            "wiki/index.json.lock",
            "wiki/index.json.lock/x.md",
            "wiki/index.json.tmp",
            "wiki/Index.JSON",
        ],
    )
    def test_family_denied(self, vault, path):
        with pytest.raises(PermissionError):
            vault.create(path, "---\nuid: cpt-x\n---\n# pwn")

    def test_corrupt_index_preserved(self, tmp_path):
        """N10: a torn index is kept as .corrupt, not silently emptied."""
        import os

        from obsidian_mcp import policies
        os.makedirs(tmp_path / "wiki", exist_ok=True)
        (tmp_path / "wiki" / "index.json").write_text("{torn json", encoding="utf-8")
        policies.update_wiki_index(tmp_path, "wiki/first.md", "First", "page", "active")
        assert (tmp_path / "wiki" / "index.json.corrupt").exists(), "corrupt original not preserved"
        import json
        data = json.loads((tmp_path / "wiki" / "index.json").read_text(encoding="utf-8"))
        assert data["total_pages"] == 1
