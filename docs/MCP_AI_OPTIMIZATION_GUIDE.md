# MCP AI Optimization Best Practices

Research synthesis from: MCP spec (2025-06-18), AWS Labs Design Guidelines,
Anthropic best practices, steipete.me MCP guide, mcp-best-practice.github.io.

## Core Principles for AI-Optimized Tool Descriptions

### 1. Tool Descriptions = AI's Only Navigation Map
The LLM sees ONLY the tool name, description, and parameter schema. It cannot
read the implementation. If the description is vague, the LLM will:
- Pick the wrong tool
- Pass wrong parameters
- Fail silently

### 2. Description Requirements (from MCP spec + AWS Labs + steipete)

Every tool description MUST:
- Start with a **clear verb**: "Read", "Create", "Delete", "Search"
- State **what** it does (not how)
- State **when** to use it (context for tool selection)
- State **what it returns** (so LLM knows what to expect)
- State **side effects** (read-only vs destructive)
- Mention **constraints** (e.g., "fails if note already exists")

Example BAD: "Delete a note."
Example GOOD: "Permanently delete a note from the vault. This action cannot be undone.
Use this when the user wants to remove a note. Returns the path of the deleted note.
Fails with an error if the note does not exist."

### 3. Parameter Descriptions (from AWS Labs "Instructing AI Models")

Every parameter MUST have a description that tells the AI:
- **What format** to use (e.g., "vault-relative path like 'Inbox/2026-07-20.md'")
- **Example values** inline
- **Default behavior** if optional
- **Constraints** (e.g., "must be YYYY-MM-DD format")

Use Pydantic Field descriptions for FastMCP:
```python
from pydantic import Field

@mcp.tool()
def read_note(
    path: str = Field(description="Vault-relative path to the note, e.g. 'Projects/Architecture.md'. Must be a .md file.")
) -> str:
```

### 4. Tool Annotations (MCP 2025-06-18 spec)

Use `annotations` to declare tool behavior:
```python
from mcp.types import ToolAnnotations

@mcp.tool(annotations=ToolAnnotations(
    title="Read Note",
    readOnlyHint=True,        # This tool only reads data
    destructiveHint=False,    # This tool does not delete data
    idempotentHint=True,      # Same input = same output
    openWorldHint=False,      # Only operates on vault data
))
```

### 5. Output Schema
Define outputSchema so the LLM knows what structure to expect.

### 6. Naming Conventions
- snake_case consistently
- Start with verb: read_, create_, delete_, search_, manage_, patch_
- Avoid abbreviations
- Be specific: `manage_frontmatter` not `edit_metadata`

### 7. Error Messages
Error messages must be **actionable** — tell the AI what went wrong AND how to fix it:
- BAD: "Error"
- GOOD: "Note not found: Inbox/missing.md. Use list_notes to see available notes."

### 8. Return Format Consistency
- ALL tools return JSON strings (already done)
- ALL errors are MCP errors (already done)
- Empty results return `[]` not text (already done)

## Checklist for Each Tool

For each of the 22 tools, verify:
1. [ ] Description starts with a verb
2. [ ] Description explains WHEN to use this tool
3. [ ] Description explains WHAT it returns
4. [ ] Description mentions side effects (destructive? read-only?)
5. [ ] Every parameter has a Field(description=...) with example
6. [ ] Optional parameters explain their default
7. [ ] Constraints are stated (e.g., "fails if exists", "must be YYYY-MM-DD")
8. [ ] ToolAnnotations are set (readOnlyHint, destructiveHint)
