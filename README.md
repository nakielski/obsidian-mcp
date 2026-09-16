# obsidian-mcp

> MCP server exposing an Obsidian vault to LLM clients (Claude, Cursor, Continue, ...).

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-1.0+-green.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests: 167](https://img.shields.io/badge/tests-167-brightgreen.svg)](#development)

## Why?

A Second Brain should be queryable by AI agents — **without shipping your data to a third-party API**.
This server gives any MCP-compatible client read and write access to a local Obsidian vault
via the Model Context Protocol.

**Key differentiators** (vs. existing Obsidian MCP servers):

- **Headless & filesystem-native** — no Obsidian desktop app required, container-ready
- **Structured output schemas** on every tool — LLMs know exactly what to expect
- **Vault graph analytics** — wikilink graph, orphan detection, outlink tracing
- **Vault health check** — automated quality scoring (broken links, untagged, TODOs, duplicates)
- **Policy enforcement** — write log, raw/ immutability, agent locking, and wiki-index
  upkeep enforced at the filesystem choke point, not left to client discipline
- **Ranked full-text search** — Whoosh-backed index with Lucene-style query syntax
  (fields, booleans, phrases, wildcards, fuzzy, ranges, did-you-mean)
- **AI-optimized tool descriptions** — 5-element docstring pattern with actionable error messages

## Features

- Read and Search notes (full-text, case-insensitive)
- Create, Update, Delete notes with proper YAML frontmatter
- Append to notes (upsert), patch sections under headings
- Frontmatter and tag management (get/set/delete, dedup, case-insensitive)
- Daily notes (YYYY-MM-DD format with date validation)
- Search & replace (literal + regex with capture groups)
- Backlink graph traversal (wikilinks)
- Vault structure as a JSON resource
- Path traversal protection on all entry points (vault sandbox)
- Tag extraction (frontmatter + inline tags)
- Structured output schemas for every MCP tool (22/22)
- Vault write policies: write log (.vault-write-log.jsonl), raw/ immutability,
  agent lock (.vault-lock), wiki/index.json upkeep, link checking
- archive_note: archive-instead-of-delete with backlink reporting
- Incremental Whoosh full-text index (BM25, stemmed, field queries, fuzzy,
  did-you-mean) with configurable index directory
- Vault graph analytics (nodes, edges, stats, broken links)
- Vault health scoring (0–100 with actionable issue report)

## Documentation

| Document | Description |
|---|---|
| [Build Guide](docs/BUILD_GUIDE.md) | Architecture walkthrough, code tour, extension guide, Docker/K8s deployment |
| [AI Optimization Guide](docs/MCP_AI_OPTIMIZATION_GUIDE.md) | Best practices for AI-optimized MCP tool descriptions |
| [Changelog](CHANGELOG.md) | Notable changes with added, fixed, and changed items |

## Quick Start

```bash
pip install -e .

# Point at your real vault
export OBSIDIAN_VAULT_ROOT=/path/to/your/obsidian/vault

# Run (stdio transport)
obsidian-mcp
```

Or use the bundled mock vault (no config needed):

```bash
obsidian-mcp   # uses examples/mock-vault/
```

## Client Integration

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "obsidian-mcp",
      "env": {
        "OBSIDIAN_VAULT_ROOT": "/path/to/your/vault"
      }
    }
  }
}
```

### Cursor

Cursor supports MCP from version 0.42+. Configure in `~/.cursor/mcp.json` (same format as Claude Desktop).

### Any other MCP client

The server runs on stdio. Register it with your client's standard MCP configuration
(command: `obsidian-mcp` or `python3 -m obsidian_mcp.server`, env: `OBSIDIAN_VAULT_ROOT`).

## Tools (22)

| Tool | Description |
|---|---|
| `read_note(path)` | Read a note (title, tags, metadata, content) |
| `search_notes(query)` | Full-text search across title and body |
| `list_notes(folder?)` | List notes, optionally scoped to a folder |
| `list_note_paths(folder?)` | List note paths only (lightweight) |
| `create_note(path, content, tags?)` | Create a note with frontmatter |
| `update_note(path, content)` | Update body (frontmatter preserved) |
| `delete_note(path)` | Permanently delete a note |
| `append_to_note(path, content)` | Append content (create if missing) |
| `manage_frontmatter(path, action, key, value?)` | Get/set/delete a frontmatter key |
| `manage_tags(path, action, tags?)` | Add/remove/list tags |
| `patch_note(path, heading, action, content)` | Insert/prepend/replace under a heading |
| `daily_note(action, content?, date?)` | Read/append daily notes (YYYY-MM-DD.md) |
| `search_and_replace(path, find, replace, use_regex?, case_sensitive?)` | Find/replace in note body |
| `get_backlinks(title)` | Find notes linking via wikilinks |
| `vault_graph()` | Full vault link graph (nodes, edges, stats) |
| `find_orphans()` | Notes with no incoming or outgoing wikilinks |
| `get_outlinks(path)` | Outgoing wikilinks from a note |
| `vault_health()` | Health score + issue report (0–100) |
| `archive_note(path, archive_dir?)` | Move note to archive folder; report backlinks |
| `search_query(query, limit?)` | Ranked Lucene-style full-text query (fields, fuzzy, ranges) |
| `rebuild_index()` | Rebuild the full-text index from all notes |
| `index_status()` | Index health: docs indexed vs. vault notes |

## Prompts (3)

| Prompt | Arguments | Description |
|---|---|---|
| `session-start` | `project?` | Review daily notes and project context before starting work |
| `session-end` | `project?` | Document accomplishments, decisions, and open questions |
| `project-checkin` | `project` (required) | Review and update a specific project's documentation |

## Resources (1)

| URI | Description |
|---|---|
| `vault://structure` | Folder/file tree of the vault as JSON |

## Architecture

```
MCP Client  ←—MCP stdio—→  obsidian-mcp server  →  vault.py  →  Obsidian Vault
(Claude/Cursor/...)         (22 tools + 3 prompts)   (filesystem)   (*.md + YAML)
                            + 1 resource                 │
                                        policies.py ←——————┘ (write log, raw lock,
                                                        wiki index, link check)
                                        search_index.py → Whoosh index (BM25)
```

**Design decisions:**
- **Filesystem-native** instead of REST API: headless, containerizable, no app dependency
- **Path traversal protection** at all entry points (`_resolve()`, `_resolve_folder()`)
- **Singleton pattern** for Vault instance (testable via `set_vault()`)
- **Pydantic BaseModel** return types for `outputSchema` on all 22 tools

## Development

```bash
pip install -e ".[dev]"
pytest tests/       # 167 tests, all passing
```

CI runs on Python 3.10, 3.11, and 3.12 with `pytest` + `ruff`.

## License

MIT
