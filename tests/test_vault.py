"""Tests for the Vault filesystem layer and MCP server tools."""
import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import Note as ExportedNote
from obsidian_mcp import Vault as ExportedVault
from obsidian_mcp.server import _to_mcp_error, mcp, set_vault
from obsidian_mcp.vault import Vault

# ---- Fixtures ----------------------------------------------------------

@pytest.fixture
def vault(tmp_path):
    """Create a temporary vault with test notes."""
    v = Vault(tmp_path)
    (tmp_path / "Inbox").mkdir()
    (tmp_path / "Inbox" / "2026-07-20.md").write_text(
        "---\ntitle: Test Note\ndate: 2026-07-20\ntags: [rag, sovereign-ai]\n---\n"
        "# Qdrant vs Pinecone\n\nFor the [[Second Brain Agent]].\n",
        encoding="utf-8",
    )
    (tmp_path / "Projects").mkdir()
    (tmp_path / "Projects" / "Second Brain Agent.md").write_text(
        "---\ntitle: Agent\ntags: [mcp, langgraph]\n---\n"
        "# Second Brain Agent\n\nUses #mcp and #rag.\n",
        encoding="utf-8",
    )
    return v


@pytest.fixture
def server_vault(vault):
    """Inject vault into server singleton."""
    set_vault(vault)
    yield vault
    set_vault(None)


def _run_async(coro):
    """Run an async coroutine in a temporary event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestVaultBasics:
    def test_list_notes(self, vault):
        notes = vault.list_notes()
        titles = [n.title for n in notes]
        assert "Qdrant vs Pinecone" in titles
        assert "Second Brain Agent" in titles

    def test_search(self, vault):
        hits = vault.search("qdrant")
        assert len(hits) == 1

    def test_read(self, vault):
        n = vault.read("Inbox/2026-07-20.md")
        assert n.title == "Qdrant vs Pinecone"
        assert "sovereign-ai" in n.tags

    def test_backlinks(self, vault):
        bl = vault.get_backlinks("Second Brain Agent")
        assert len(bl) == 1


class TestDateSerialization:
    def test_read_note_with_date_does_not_crash(self, vault):
        """Reading a note with date: 2026-07-20 must not raise."""
        n = vault.read("Inbox/2026-07-20.md")
        result = json.dumps({"metadata": n.metadata})
        parsed = json.loads(result)
        assert parsed["metadata"]["date"] == "2026-07-20"

    def test_note_to_dict_is_json_serializable(self, vault):
        """Full note dict must survive json.dumps."""
        n = vault.read("Inbox/2026-07-20.md")
        d = {"path": n.path, "title": n.title, "tags": n.tags, "metadata": n.metadata, "content": n.content}
        result = json.dumps(d)
        assert "Qdrant" in result


class TestCreateOverwriteProtection:
    def test_create_fails_on_existing(self, vault, tmp_path):
        with pytest.raises(FileExistsError):
            vault.create("Inbox/2026-07-20.md", "# Overwrite attempt")

    def test_create_succeeds_on_new(self, vault, tmp_path):
        n = vault.create("Inbox/new-note.md", "# Fresh Note")
        assert n.title == "Fresh Note"


class TestPathTraversal:
    def test_read_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.read("../../../etc/passwd")

    def test_list_notes_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.list_notes("../..")

    def test_list_note_paths_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.list_note_paths("../../etc")

    def test_absolute_path_rejected(self, vault):
        with pytest.raises(PermissionError):
            vault.read("/etc/passwd")

    def test_create_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.create("../../etc/evil.md", "# pwned")

    def test_update_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.update("../../etc/passwd", "# pwned")


class TestMcpErrorConstruction:
    def test_file_not_found_error_to_mcp(self):
        err = _to_mcp_error(FileNotFoundError("test"))
        assert hasattr(err, "error")
        assert err.error.code == -32602

    def test_permission_error_to_mcp(self):
        err = _to_mcp_error(PermissionError("test"))
        assert err.error.code == -32603

    def test_file_exists_error_to_mcp(self):
        err = _to_mcp_error(FileExistsError("test"))
        assert err.error.code == -32603


class TestServerTools:
    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_call_tool_read_note(self, server_vault):
        result = self._run_async(mcp.call_tool("read_note", {"path": "Inbox/2026-07-20.md"}))
        assert len(result) == 2
        text = result[0][0].text
        data = json.loads(text)
        assert data["title"] == "Qdrant vs Pinecone"

    def test_call_tool_read_nonexistent_returns_mcp_error(self, server_vault):
        with pytest.raises(Exception) as exc_info:
            self._run_async(mcp.call_tool("read_note", {"path": "does-not-exist.md"}))
        assert "TypeError" not in str(exc_info.value)

    def test_call_tool_list_notes(self, server_vault):
        result = self._run_async(mcp.call_tool("list_notes", {}))
        text = result[0][0].text
        data = json.loads(text)
        assert len(data) == 2

    def test_call_tool_search_notes_empty(self, server_vault):
        result = self._run_async(mcp.call_tool("search_notes", {"query": "zzznonexistent"}))
        text = result[0][0].text
        data = json.loads(text)
        assert data == []

    def test_call_tool_create_note(self, server_vault):
        result = self._run_async(mcp.call_tool("create_note", {"path": "Inbox/created.md", "content": "# Created Note", "tags": ["test"]}))
        text = result[0][0].text
        data = json.loads(text)
        assert data["created"] == "Inbox/created.md"

    def test_call_tool_create_existing_fails(self, server_vault):
        with pytest.raises(Exception):
            self._run_async(mcp.call_tool("create_note", {"path": "Inbox/2026-07-20.md", "content": "# overwrite"}))

    def test_call_tool_get_backlinks(self, server_vault):
        result = self._run_async(mcp.call_tool("get_backlinks", {"title": "Second Brain Agent"}))
        text = result[0][0].text
        data = json.loads(text)
        assert len(data) == 1


class TestServerResources:
    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_read_resource_structure(self, server_vault):
        result = self._run_async(mcp.read_resource("vault://structure"))
        text = result[0].content
        data = json.loads(text)
        assert "Inbox" in data
        assert "Projects" in data


class TestEdgeCases:
    def test_update_missing_file(self, vault):
        with pytest.raises(FileNotFoundError):
            vault.update("Inbox/nonexistent.md", "# content")

    def test_trailing_slash_path(self, vault, tmp_path):
        n = vault.create("Inbox/slash-test/", "# Slash")
        assert n.path == "Inbox/slash-test.md"

    def test_tag_deduplication(self, vault, tmp_path):
        n = vault.create("Inbox/_dedup_test.md", "# Test\n\nThis has #mcp inline.", tags=["mcp", "test"])
        assert n.tags.count("mcp") == 1
        (tmp_path / "Inbox" / "_dedup_test.md").unlink()

    def test_title_not_from_codeblock(self, vault, tmp_path):
        n = vault.create("Inbox/_codeblock_test.md", "```\n# Not a heading\n```\n\n# Real Heading")
        assert n.title == "Real Heading"
        (tmp_path / "Inbox" / "_codeblock_test.md").unlink()

    def test_invalid_yaml_skipped_in_list(self, vault, tmp_path):
        (tmp_path / "Inbox" / "broken.md").write_text("not valid: [yaml: at: all\n---\n# broken", encoding="utf-8")
        notes = vault.list_notes()
        assert len(notes) >= 2
        (tmp_path / "Inbox" / "broken.md").unlink()

    def test_trash_filtered(self, vault, tmp_path):
        trash_dir = tmp_path / ".trash"
        trash_dir.mkdir(exist_ok=True)
        (trash_dir / "deleted.md").write_text("# Deleted", encoding="utf-8")
        paths = vault.list_note_paths()
        assert not any(".trash" in p for p in paths)


class TestPackageExports:
    def test_vault_exported(self):
        assert ExportedVault is Vault

    def test_note_exported(self):
        assert ExportedNote is not None


class TestDeleteNote:
    def test_delete_note(self, vault, tmp_path):
        vault.create("Inbox/delete-me.md", "# Delete Me")
        deleted = vault.delete("Inbox/delete-me.md")
        assert deleted == "Inbox/delete-me.md"
        assert not (tmp_path / "Inbox" / "delete-me.md").exists()

    def test_delete_nonexistent_raises(self, vault):
        with pytest.raises(FileNotFoundError):
            vault.delete("Inbox/missing.md")

    def test_delete_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.delete("../../../etc/passwd")


class TestServerDeleteNote:
    def test_call_tool_delete_note(self, server_vault):
        server_vault.create("Inbox/del-server.md", "# X")
        result = _run_async(mcp.call_tool("delete_note", {"path": "Inbox/del-server.md"}))
        data = json.loads(result[0][0].text)
        assert data["deleted"] == "Inbox/del-server.md"

    def test_call_tool_delete_traversal(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("delete_note", {"path": "../../../etc/passwd"}))

    def test_call_tool_delete_nonexistent(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("delete_note", {"path": "missing.md"}))


class TestAppendToNote:
    def test_append_to_existing(self, vault):
        n, created = vault.append("Inbox/2026-07-20.md", "Additional line.")
        assert not created
        assert "Additional line." in n.content

    def test_append_creates_new(self, vault, tmp_path):
        n, created = vault.append("Inbox/brand-new.md", "First line.")
        assert created
        assert n.title == "brand-new"
        assert (tmp_path / "Inbox" / "brand-new.md").exists()

    def test_append_preserves_frontmatter(self, vault):
        n, _ = vault.append("Inbox/2026-07-20.md", "More content.")
        assert n.metadata.get("title") == "Test Note"
        assert n.metadata.get("date") == "2026-07-20"

    def test_append_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.append("../../etc/passwd", "evil")


class TestServerAppendToNote:
    def test_call_tool_append_to_existing(self, server_vault):
        result = _run_async(mcp.call_tool("append_to_note", {"path": "Inbox/2026-07-20.md", "content": "Appended via MCP."}))
        data = json.loads(result[0][0].text)
        assert data["appended"] is True
        assert data["path"] == "Inbox/2026-07-20.md"
        assert data["created"] is False

    def test_call_tool_append_creates_new(self, server_vault):
        result = _run_async(mcp.call_tool("append_to_note", {"path": "Inbox/mcp-new.md", "content": "Created via MCP."}))
        data = json.loads(result[0][0].text)
        assert data["created"] is True

    def test_call_tool_append_traversal(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("append_to_note", {"path": "../../etc/passwd", "content": "evil"}))


class TestFrontmatterManagement:
    def test_get_frontmatter_existing(self, vault):
        assert vault.get_frontmatter("Inbox/2026-07-20.md", "title") == "Test Note"

    def test_get_frontmatter_missing_key(self, vault):
        assert vault.get_frontmatter("Inbox/2026-07-20.md", "missing") is None

    def test_set_frontmatter_preserves_body(self, vault):
        vault.set_frontmatter("Inbox/2026-07-20.md", "status", "review")
        n = vault.read("Inbox/2026-07-20.md")
        assert n.metadata["status"] == "review"
        assert "Qdrant vs Pinecone" in n.content
        assert n.metadata["title"] == "Test Note"

    def test_delete_frontmatter_removes_key(self, vault):
        vault.delete_frontmatter("Inbox/2026-07-20.md", "date")
        n = vault.read("Inbox/2026-07-20.md")
        assert "date" not in n.metadata

    def test_delete_frontmatter_no_error_on_missing_key(self, vault):
        vault.delete_frontmatter("Inbox/2026-07-20.md", "nonexistent")

    def test_frontmatter_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.get_frontmatter("../../etc/passwd", "x")


class TestServerManageFrontmatter:
    def test_call_tool_manage_frontmatter_get(self, server_vault):
        result = _run_async(mcp.call_tool("manage_frontmatter", {"path": "Inbox/2026-07-20.md", "action": "get", "key": "title"}))
        data = json.loads(result[0][0].text)
        assert data == {"key": "title", "value": "Test Note"}

    def test_call_tool_manage_frontmatter_set(self, server_vault):
        result = _run_async(mcp.call_tool("manage_frontmatter", {"path": "Inbox/2026-07-20.md", "action": "set", "key": "priority", "value": 1}))
        data = json.loads(result[0][0].text)
        assert data["key"] == "priority"
        assert data["value"] == 1

    def test_call_tool_manage_frontmatter_delete(self, server_vault):
        result = _run_async(mcp.call_tool("manage_frontmatter", {"path": "Inbox/2026-07-20.md", "action": "delete", "key": "date"}))
        data = json.loads(result[0][0].text)
        assert data["deleted"] is True

    def test_call_tool_manage_frontmatter_invalid_action(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("manage_frontmatter", {"path": "Inbox/2026-07-20.md", "action": "destroy", "key": "x"}))

    def test_call_tool_manage_frontmatter_traversal(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("manage_frontmatter", {"path": "../../etc/passwd", "action": "get", "key": "x"}))


class TestTagManagement:
    def test_add_tags_creates_array(self, vault):
        vault.create("Inbox/no-tags.md", "# No tags", tags=None)
        updated = vault.add_tags("Inbox/no-tags.md", ["new-tag"])
        assert updated == ["new-tag"]

    def test_add_tags_deduplicates(self, vault):
        updated = vault.add_tags("Inbox/2026-07-20.md", ["rag", "NEW"])
        assert "rag" in updated
        assert "new" in updated
        assert updated.count("rag") == 1

    def test_remove_tags_case_insensitive(self, vault):
        updated = vault.remove_tags("Inbox/2026-07-20.md", ["RAG"])
        assert "rag" not in updated

    def test_remove_tags_leaves_empty_list(self, vault, tmp_path):
        vault.create("Inbox/only-one.md", "# X", tags=["only"])
        updated = vault.remove_tags("Inbox/only-one.md", ["only"])
        assert updated == []

    def test_list_tags_includes_inline(self, vault):
        tags = vault.list_tags("Projects/Second Brain Agent.md")
        assert "mcp" in tags
        assert "langgraph" in tags
        assert "rag" in tags

    def test_tag_management_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.add_tags("../../etc/passwd", ["evil"])


class TestServerManageTags:
    def test_call_tool_manage_tags_add(self, server_vault):
        result = _run_async(mcp.call_tool("manage_tags", {"path": "Inbox/2026-07-20.md", "action": "add", "tags": ["server-tag"]}))
        data = json.loads(result[0][0].text)
        assert "server-tag" in data["tags"]

    def test_call_tool_manage_tags_list(self, server_vault):
        result = _run_async(mcp.call_tool("manage_tags", {"path": "Projects/Second Brain Agent.md", "action": "list"}))
        data = json.loads(result[0][0].text)
        assert "mcp" in data["tags"]

    def test_call_tool_manage_tags_remove(self, server_vault):
        result = _run_async(mcp.call_tool("manage_tags", {"path": "Inbox/2026-07-20.md", "action": "remove", "tags": ["rag"]}))
        data = json.loads(result[0][0].text)
        assert "rag" not in data["tags"]

    def test_call_tool_manage_tags_traversal(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("manage_tags", {"path": "../../etc/passwd", "action": "list"}))


class TestPatchNote:
    @pytest.fixture
    def structured(self, vault):
        vault.create("Inbox/structured.md", "# Title\n\nIntro.\n\n## Section A\n\nLine one.\nLine two.\n\n### Subsection\n\nSub text.\n\n## Section B\n\nB content.\n")
        return vault

    def test_patch_append_to_section(self, structured):
        n = structured.patch_section("Inbox/structured.md", "Section A", "append", "Appended line.")
        assert "Appended line." in n.content
        idx_appended = n.content.index("Appended line.")
        idx_b = n.content.index("## Section B")
        assert idx_appended < idx_b

    def test_patch_prepend_to_section(self, structured):
        n = structured.patch_section("Inbox/structured.md", "Section A", "prepend", "Prepended line.")
        heading_idx = n.content.index("## Section A")
        prepended_idx = n.content.index("Prepended line.")
        assert prepended_idx > heading_idx
        assert n.content.index("Line one.") > prepended_idx

    def test_patch_replace_section(self, structured):
        n = structured.patch_section("Inbox/structured.md", "Section A", "replace", "Replaced.")
        assert "Replaced." in n.content
        assert "Line one." not in n.content

    def test_patch_heading_not_found(self, structured):
        with pytest.raises(ValueError):
            structured.patch_section("Inbox/structured.md", "Missing", "append", "x")

    def test_patch_preserves_frontmatter(self, structured):
        structured.set_frontmatter("Inbox/structured.md", "author", "test")
        n = structured.patch_section("Inbox/structured.md", "Section A", "append", "x")
        assert n.metadata.get("author") == "test"

    def test_patch_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.patch_section("../../etc/passwd", "X", "append", "evil")


class TestServerPatchNote:
    def test_call_tool_patch_note(self, server_vault):
        server_vault.create("Inbox/patch-server.md", "# T\n\n## H\n\nbody\n")
        result = _run_async(mcp.call_tool("patch_note", {"path": "Inbox/patch-server.md", "heading": "H", "action": "append", "content": "extra"}))
        data = json.loads(result[0][0].text)
        assert data["heading"] == "H"
        assert data["action"] == "append"

    def test_call_tool_patch_note_heading_not_found(self, server_vault):
        server_vault.create("Inbox/patch-missing.md", "# T\n\nbody\n")
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("patch_note", {"path": "Inbox/patch-missing.md", "heading": "Ghost", "action": "append", "content": "x"}))

    def test_call_tool_patch_note_traversal(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("patch_note", {"path": "../../etc/passwd", "heading": "X", "action": "append", "content": "evil"}))


class TestDailyNote:
    def test_daily_note_read_existing(self, vault):
        p = vault.daily_note_path("2026-07-23")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Daily\n\nNote.", encoding="utf-8")
        n = vault.read(str(p.relative_to(vault.root)))
        assert n.title == "Daily"

    def test_daily_note_read_missing(self, vault):
        p = vault.daily_note_path("2099-01-01")
        assert not p.exists()

    def test_daily_note_append_creates(self, vault):
        n, created = vault.append(str(vault.daily_note_path("2099-02-02").relative_to(vault.root)), "First daily line.")
        assert created
        assert "First daily line." in n.content

    def test_daily_note_append_existing(self, vault):
        rel = str(vault.daily_note_path("2099-03-03").relative_to(vault.root))
        vault.append(rel, "Line 1.")
        n, created = vault.append(rel, "Line 2.")
        assert not created
        assert "Line 1." in n.content
        assert "Line 2." in n.content

    def test_daily_note_invalid_date_format(self, vault):
        with pytest.raises(ValueError):
            vault.daily_note_path("not-a-date")


class TestServerDailyNote:
    def test_call_tool_daily_note_read_missing(self, server_vault):
        result = _run_async(mcp.call_tool("daily_note", {"action": "read", "date": "2099-04-04"}))
        data = json.loads(result[0][0].text)
        assert data["exists"] is False
        assert data["date"] == "2099-04-04"

    def test_call_tool_daily_note_append(self, server_vault):
        result = _run_async(mcp.call_tool("daily_note", {"action": "append", "date": "2099-05-05", "content": "Daily append."}))
        data = json.loads(result[0][0].text)
        assert data["appended"] is True
        assert data["created"] is True

    def test_call_tool_daily_note_invalid_date(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("daily_note", {"action": "read", "date": "../../etc/passwd"}))


class TestSearchAndReplace:
    def test_search_replace_literal(self, vault):
        vault.create("Inbox/replace.md", "Hello world. Hello again.")
        new, count = vault.search_and_replace("Inbox/replace.md", "Hello", "Hi")
        assert count == 2
        assert "Hi world. Hi again." in new

    def test_search_replace_regex_groups(self, vault):
        vault.create("Inbox/replace-re.md", "John Doe")
        new, count = vault.search_and_replace("Inbox/replace-re.md", r"(\w+) (\w+)", r"\2, \1", use_regex=True)
        assert count == 1
        assert "Doe, John" in new

    def test_search_replace_case_insensitive(self, vault):
        vault.create("Inbox/replace-ci.md", "HELLO world")
        new, count = vault.search_and_replace("Inbox/replace-ci.md", "hello", "hi", case_sensitive=False)
        assert count == 1
        assert "hi world" in new.lower()

    def test_search_replace_no_matches(self, vault):
        vault.create("Inbox/replace-none.md", "Hello world")
        new, count = vault.search_and_replace("Inbox/replace-none.md", "xyz", "abc")
        assert count == 0
        assert "Hello world" in new

    def test_search_replace_nonexistent_note(self, vault):
        with pytest.raises(FileNotFoundError):
            vault.search_and_replace("Inbox/missing.md", "x", "y")

    def test_search_replace_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.search_and_replace("../../etc/passwd", "x", "y")


class TestServerSearchAndReplace:
    def test_call_tool_search_and_replace_literal(self, server_vault):
        server_vault.create("Inbox/sr-server.md", "foo bar foo")
        result = _run_async(mcp.call_tool("search_and_replace", {"path": "Inbox/sr-server.md", "find": "foo", "replace": "baz"}))
        data = json.loads(result[0][0].text)
        assert data["replacements"] == 2
        assert "baz bar baz" in data["new_content_preview"]

    def test_call_tool_search_and_replace_traversal(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("search_and_replace", {"path": "../../etc/passwd", "find": "x", "replace": "y"}))


class TestPrompts:
    def test_prompts_listed(self, server_vault):
        prompts = _run_async(mcp.list_prompts())
        names = {p.name for p in prompts}
        assert names >= {"session-start", "session-end", "project-checkin"}

    def test_prompt_session_start_registered(self, server_vault):
        result = _run_async(mcp.get_prompt("session-start", {"project": "AI"}))
        text = result.messages[0].content.text
        assert "today's daily note" in text.lower()
        assert "AI" in text

    def test_prompt_session_end_returns_instructions(self, server_vault):
        result = _run_async(mcp.get_prompt("session-end", {}))
        text = result.messages[0].content.text
        assert "session summary" in text.lower()
        assert "accomplished" in text.lower()

    def test_prompt_project_checkin_requires_project(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.get_prompt("project-checkin", {}))

    def test_prompt_project_checkin_with_project(self, server_vault):
        result = _run_async(mcp.get_prompt("project-checkin", {"project": "AI"}))
        text = result.messages[0].content.text
        assert "AI" in text
        assert "TODO" in text


class TestOutputSchemas:
    def test_all_tools_have_output_schema(self, server_vault):
        tools = _run_async(mcp.list_tools())
        assert len(tools) >= 18
        missing = [t.name for t in tools if not t.outputSchema]
        assert missing == [], f"Tools missing outputSchema: {missing}"

    def test_read_note_output_schema_shape(self, server_vault):
        tools = _run_async(mcp.list_tools())
        tool = next(t for t in tools if t.name == "read_note")
        schema = tool.outputSchema
        assert schema["type"] == "object"
        assert set(schema["properties"].keys()) == {"path", "title", "tags", "metadata", "content"}

    def test_search_notes_output_schema_is_array(self, server_vault):
        tools = _run_async(mcp.list_tools())
        tool = next(t for t in tools if t.name == "search_notes")
        schema = tool.outputSchema
        assert schema.get("type") == "array"

    def test_new_rev6_tools_registered(self, server_vault):
        tools = _run_async(mcp.list_tools())
        names = {t.name for t in tools}
        assert names >= {"vault_graph", "find_orphans", "get_outlinks", "vault_health"}


class TestVaultGraph:
    def test_vault_graph_builds_nodes_and_edges(self, vault):
        graph = vault.vault_graph()
        assert graph["stats"]["total_notes"] == 2
        assert graph["stats"]["total_edges"] == 1
        paths = {n["path"] for n in graph["nodes"]}
        assert paths == {"Inbox/2026-07-20.md", "Projects/Second Brain Agent.md"}
        edge = graph["edges"][0]
        assert edge["source"] == "Inbox/2026-07-20.md"
        assert edge["target"] == "Projects/Second Brain Agent.md"
        assert edge["target_exists"] is True

    def test_vault_graph_counts_broken_links(self, vault, tmp_path):
        (tmp_path / "Inbox" / "broken-link.md").write_text("# Broken\n\nSee [[Nonexistent Note]].", encoding="utf-8")
        graph = vault.vault_graph()
        broken = [e for e in graph["edges"] if not e["target_exists"]]
        assert len(broken) == 1
        assert broken[0]["target"] == "Nonexistent Note"
        assert graph["stats"]["broken_link_count"] == 1

    def test_find_orphans_isolated_note(self, vault, tmp_path):
        (tmp_path / "Inbox" / "orphan.md").write_text("# Orphan\n\nNo links.", encoding="utf-8")
        orphans = vault.find_orphans()
        assert any(n.path == "Inbox/orphan.md" for n in orphans)

    def test_find_orphans_non_orphan_linked_note(self, vault):
        orphans = vault.find_orphans()
        assert not any(n.path == "Inbox/2026-07-20.md" for n in orphans)

    def test_get_outlinks_existing_note(self, vault):
        links = vault.get_outlinks("Inbox/2026-07-20.md")
        assert links == ["Second Brain Agent"]

    def test_get_outlinks_deduplicates(self, vault, tmp_path):
        (tmp_path / "Inbox" / "dup-links.md").write_text("# Dups\n\n[[A]] and [[a]] and [[A]].", encoding="utf-8")
        links = vault.get_outlinks("Inbox/dup-links.md")
        assert links == ["A"]

    def test_get_outlinks_ignores_code_blocks(self, vault, tmp_path):
        (tmp_path / "Inbox" / "code-links.md").write_text("# Code\n\n```\n[[Ignored]]\n```\n\nSee [[Real Target]].", encoding="utf-8")
        links = vault.get_outlinks("Inbox/code-links.md")
        assert "Ignored" not in links
        assert links == ["Real Target"]

    def test_get_outlinks_blocks_traversal(self, vault):
        with pytest.raises(PermissionError):
            vault.get_outlinks("../../etc/passwd")

    def test_get_outlinks_nonexistent_raises(self, vault):
        with pytest.raises(FileNotFoundError):
            vault.get_outlinks("Inbox/missing.md")


class TestServerVaultGraph:
    def test_call_tool_vault_graph(self, server_vault):
        result = _run_async(mcp.call_tool("vault_graph", {}))
        text = result[0][0].text
        data = json.loads(text)
        assert data["stats"]["total_notes"] == 2
        assert data["stats"]["total_edges"] == 1
        assert "nodes" in data
        assert "edges" in data

    def test_call_tool_find_orphans(self, server_vault, tmp_path):
        (tmp_path / "Inbox" / "orphan.md").write_text("# Orphan\n\nAlone.", encoding="utf-8")
        result = _run_async(mcp.call_tool("find_orphans", {}))
        text = result[0][0].text
        data = json.loads(text)
        assert any(item["path"] == "Inbox/orphan.md" for item in data)

    def test_call_tool_get_outlinks(self, server_vault):
        result = _run_async(mcp.call_tool("get_outlinks", {"path": "Inbox/2026-07-20.md"}))
        text = result[0][0].text
        data = json.loads(text)
        assert data == ["Second Brain Agent"]

    def test_call_tool_get_outlinks_traversal(self, server_vault):
        with pytest.raises(Exception):
            _run_async(mcp.call_tool("get_outlinks", {"path": "../../etc/passwd"}))


class TestVaultHealth:
    def test_vault_health_detects_broken_link(self, vault, tmp_path):
        (tmp_path / "Inbox" / "broken.md").write_text("# Broken\n\n[[Missing Page]]", encoding="utf-8")
        report = vault.vault_health()
        broken = report["checks"]["broken_links"]
        assert broken["count"] == 1
        assert broken["items"][0]["target"] == "Missing Page"

    def test_vault_health_detects_untagged_note(self, vault, tmp_path):
        (tmp_path / "Inbox" / "untagged.md").write_text("# Untagged\n\nNo tags here.", encoding="utf-8")
        report = vault.vault_health()
        untagged = report["checks"]["untagged_notes"]
        assert "Inbox/untagged.md" in untagged["items"]

    def test_vault_health_detects_empty_note(self, vault, tmp_path):
        (tmp_path / "Inbox" / "empty.md").write_text("", encoding="utf-8")
        report = vault.vault_health()
        empty = report["checks"]["empty_notes"]
        assert "Inbox/empty.md" in empty["items"]

    def test_vault_health_detects_todo_and_fixme(self, vault, tmp_path):
        (tmp_path / "Inbox" / "tasks.md").write_text("# Tasks\n\nTODO: finish this\nFIXME: bug here", encoding="utf-8")
        report = vault.vault_health()
        assert report["checks"]["todos"]["count"] == 1
        assert report["checks"]["fixmes"]["count"] == 1

    def test_vault_health_detects_duplicate_titles(self, vault, tmp_path):
        (tmp_path / "Inbox" / "dup1.md").write_text("# Same Title\n\nOne.", encoding="utf-8")
        (tmp_path / "Projects" / "dup2.md").write_text("# Same Title\n\nTwo.", encoding="utf-8")
        report = vault.vault_health()
        dupes = report["checks"]["duplicate_titles"]
        assert dupes["count"] == 1
        assert set(dupes["items"][0]["paths"]) == {"Inbox/dup1.md", "Projects/dup2.md"}

    def test_vault_health_score_is_within_range(self, vault):
        report = vault.vault_health()
        assert 0 <= report["score"] <= 100
        assert report["total_notes"] >= 2

    def test_vault_health_empty_vault(self, tmp_path):
        empty_vault = Vault(tmp_path)
        report = empty_vault.vault_health()
        assert report["score"] == 100
        assert report["total_notes"] == 0
        for check in report["checks"].values():
            assert check["count"] == 0


class TestServerVaultHealth:
    def test_call_tool_vault_health_returns_report(self, server_vault):
        result = _run_async(mcp.call_tool("vault_health", {}))
        text = result[0][0].text
        data = json.loads(text)
        assert "score" in data
        assert "total_notes" in data
        assert "checks" in data
        assert set(data["checks"].keys()) == {
            "broken_links",
            "untagged_notes",
            "empty_notes",
            "todos",
            "fixmes",
            "duplicate_titles",
        }


class TestWikiUidValidation:
    """Wiki notes require a non-empty uid field at creation time."""

    def test_b1_create_wiki_without_uid_rejected(self, vault, tmp_path):
        with pytest.raises(ValueError, match="wiki note requires uid"):
            vault.create("wiki/concepts/x.md", "# X")
        assert not (tmp_path / "wiki" / "concepts" / "x.md").exists()

    def test_b2_create_wiki_with_uid_ok(self, vault):
        n = vault.create("wiki/concepts/x.md", "---\nuid: note-test\ntitel: X\n---\n# X")
        assert n.metadata["uid"] == "note-test"

    def test_b3_create_raw_without_uid_ok(self, vault, tmp_path):
        vault.create("raw/conversations/x.md", "# R")
        assert (tmp_path / "raw" / "conversations" / "x.md").exists()

    def test_b4_update_existing_wiki_body_only_ok(self, vault):
        vault.create("wiki/concepts/x.md", "---\nuid: note-test\ntitel: X\n---\n# X")
        n = vault.update("wiki/concepts/x.md", "# New body")
        assert n.metadata.get("uid") == "note-test"
        assert "New body" in n.content

    def test_b5_append_create_wiki_log_exempt(self, vault, tmp_path):
        n, created = vault.append("wiki/log/2026-09.md", "# Log entry")
        assert created
        assert (tmp_path / "wiki" / "log" / "2026-09.md").exists()

    def test_b6_append_create_wiki_concepts_without_uid_rejected(self, vault, tmp_path):
        with pytest.raises(ValueError, match="wiki note requires uid"):
            vault.append("wiki/concepts/y.md", "# No uid")
        assert not (tmp_path / "wiki" / "concepts" / "y.md").exists()

    def test_b7_empty_string_uid_rejected(self, vault, tmp_path):
        with pytest.raises(ValueError, match="wiki note requires uid"):
            vault.create("wiki/concepts/z.md", '---\nuid: ""\ntitel: Y\n---\n# Y')
        assert not (tmp_path / "wiki" / "concepts" / "z.md").exists()

    def test_b8_wiki_index_json_exempt(self, vault, tmp_path):
        """wiki/index.json is DENIED as a note target — it is derived
        bookkeeping, writing it via tools corrupts the index (B3/B8)."""
        with pytest.raises(PermissionError):
            vault.create("wiki/index.json", "{}")
        assert not (tmp_path / "wiki" / "index.json").exists()


class TestServerWikiUidValidation:
    """Integration: MCP error reaches the client when uid is missing."""

    def test_create_wiki_no_uid_raises_with_message(self, server_vault):
        with pytest.raises(Exception) as exc_info:
            _run_async(mcp.call_tool("create_note", {"path": "wiki/concepts/no-uid.md", "content": "# Hi"}))
        assert "wiki note requires uid" in str(exc_info.value)


class TestAppendFrontmatter:
    """append() create-path must not double-wrap content that already carries YAML FM."""

    FM_CONTENT = "---\nuid: note-x\ntitle: T\n---\n\n# Body"
    PLAIN_CONTENT = "# Nur Text"

    def test_a1_first_bytes_are_dashes(self, vault, tmp_path):
        """A1: file written by append(FM_CONTENT) must start with '---' on disk."""
        vault.append("Inbox/a1-test.md", self.FM_CONTENT)
        raw_bytes = (tmp_path / "Inbox" / "a1-test.md").read_bytes()
        assert raw_bytes[:3] == b"---", (
            f"Expected file to start with b'---', got {raw_bytes[:20]!r}"
        )

    def test_a1_uid_parsed_correctly(self, vault, tmp_path):
        """A1: frontmatter uid must survive the round-trip."""
        import frontmatter as fm
        vault.append("Inbox/a1-uid.md", self.FM_CONTENT)
        raw = (tmp_path / "Inbox" / "a1-uid.md").read_text(encoding="utf-8")
        post = fm.loads(raw)
        assert post.metadata.get("uid") == "note-x", (
            f"uid not found in metadata: {post.metadata}"
        )

    def test_a1_no_double_fm_block(self, vault, tmp_path):
        """A1 negative: body must not contain a second '---' FM delimiter."""
        import frontmatter as fm
        vault.append("Inbox/a1-double.md", self.FM_CONTENT)
        raw = (tmp_path / "Inbox" / "a1-double.md").read_text(encoding="utf-8")
        post = fm.loads(raw)
        body = post.content
        assert not body.lstrip().startswith("---"), (
            f"Body starts with '---', indicating a double FM block:\n{body[:120]}"
        )

    def test_a2_plain_body_preserved(self, vault, tmp_path):
        """A2: append of plain body must write the body text unchanged."""
        vault.append("Inbox/a2-plain.md", self.PLAIN_CONTENT)
        raw = (tmp_path / "Inbox" / "a2-plain.md").read_text(encoding="utf-8")
        assert "# Nur Text" in raw, (
            f"Plain body not found in written file: {raw[:120]!r}"
        )

    def test_a2_plain_body_note_readable(self, vault):
        """A2: vault.read() must succeed and return correct content."""
        vault.append("Inbox/a2-read.md", self.PLAIN_CONTENT)
        n = vault.read("Inbox/a2-read.md")
        assert "# Nur Text" in n.content or n.title == "Nur Text"
