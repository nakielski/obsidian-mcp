# Changelog

All notable changes to **obsidian-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-09-19 — Security hardening

### Security
- **Path resolution hardening**: sandbox escape via 'Inbox/..'
  fixed; policy checks run on the NORMALIZED path ('./raw/x.md', ' raw/x.md',
  tab/backslash spellings, 'RAW/' case no longer bypass RAW_IMMUTABLE);
  '.'/'..' segments rejected with an explicit segment split.
- **Deny-list for internal files**: audit log, lock file, Whoosh
  index dir, `.obsidian/` (any depth, incl. symlinks via resolved-path check)
  and `wiki/index.json` are never tool-accessible; listings exclude them.
- **ReDoS protection**: user regex runs through the `regex` module with
  a 2s execution timeout (was: 102s CPU burn on a 40-char subject); pattern
  length capped; notes >2 MiB rejected for search/replace.
- **raw/ immutability completed**: append to existing raw notes
  blocked (daily-note journaling exempt); archive_note enforces lock +
  immutability (raw notes can no longer be moved out).
- **Literal replace fixed**: replace text is plain text in literal mode
  (no \\1 group injection, no Windows-path crashes).

### Fixed
- patch_section treats '#' lines inside code fences as content, not headings.
- search_and_replace re-indexes the note instead of dropping it from the
  Whoosh index; archive/delete keep index and wiki/index.json in sync.
- wiki/index.json updates are flock-guarded and atomic: 3-process race
  test now preserves 150/150 entries (was 887/900 lost).
- Write log records the note's uid for every action incl. delete.
- read-modify-write methods hold a lock: 20 parallel appends all survive.
- serverInfo reports the project version, not the mcp library version.
- search() bounded (default 100 hits), empty query returns [].
- Notes >2 MiB are rejected before YAML parsing.

### Changed
- Vault.archive() public API; server.py no longer uses private helpers.
- regex>=2024.11.6 added as a dependency.

## [0.8.0] — 2026-09-14

### Changed

- **Write log: per-actor shard files instead of one shared file.** Appends now
  go to `.vault-write-log.d/<actor>.jsonl` (one file per `VAULT_ACTOR`), not to
  the shared `.vault-write-log.jsonl`. Rationale: on a Syncthing-synchronized
  vault, file-level sync cannot merge concurrent appends to ONE file from two
  devices — the loser's lines are silently dropped (observed 2026-09-14: 4
  career lines lost to a concurrent windows writer, and 213 lines lost in an
  earlier August conflict). Per-actor shards restore a single-writer
  invariant per file: each device+actor appends only to its own shard, so
  there is nothing to lose. Readers must merge legacy file + shards; the
  legacy file is still written when the shard directory is not writable
  (fallback) and remains the target for external writers that opt out
  (`VAULT_WRITE_LOG_SHARDS=0` restores the old single-file behavior).
- **Log appends take an exclusive `flock`** while writing the line, making
  same-host concurrent appends from multiple Hermes processes explicit and
  atomic.

### Migration notes

- Consumers of `.vault-write-log.jsonl` (checker, routers, audits) must read
  the merged view: legacy file + `.vault-write-log.d/*.jsonl` (+ any
  `.vault-write-log.sync-conflict-*.jsonl` leftovers). A reference reader
  lives in the Hermes infra profile (`writelog_reader.py`).
- Existing history stays in the legacy file — no rewrite needed (append-only).


### Fixed

- **Frontmatter/tag writes now go through the policy hooks**: `set_frontmatter`,
  `delete_frontmatter`, `add_tags` and `remove_tags` bypassed the `_after_write`
  choke point — these write paths produced no `.vault-write-log.jsonl` entry and
  no search-index update
- **`wiki/index.json` `generated` timestamp** is refreshed on every update
  (previously only written when absent)
- **Index write lock acquired with a timeout** (5 s; 30 s for full rebuilds):
  concurrent writers on a shared index used to fail immediately with LockError,
  leaving notes silently unindexed until the next `rebuild_index`

### Docs

- `docs/DEPLOYMENT.md`: index location guidance for synchronized vaults
  (Syncthing/`.stignore`), multi-profile `VAULT_ACTOR` attribution pattern

## [0.7.0] — 2026-08-31 (Rev 7)

### Added — Vault Policy Hooks + Full-Text Index

- **Vault write policies** (`policies.py`, enforced at the Vault choke point):
  - `VAULT_WRITE_LOG`: JSON line per write to `.vault-write-log.jsonl`
    (actor from `VAULT_ACTOR`, layer auto-classified raw/wiki/para)
  - `RAW_IMMUTABLE`: update/delete on `raw/` blocked — sources are immutable
  - `VAULT_LOCK`: writes refused while a foreign `.vault-lock` (younger than
    30 min) exists; `acquire_lock`/`release_lock` helpers
  - `VAULT_INDEX_UPDATE`: `wiki/index.json` kept in sync for wiki/ writes
    (path/title/type/status/uid, grouped sort order, total_pages)
  - `VAULT_LINK_CHECK`: advisory unresolved-wikilink detection
    (`Vault.check_note_links`)
  - All default-on, each individually disableable via environment variable
- **archive_note tool**: move a note to the archive folder (default
  `06 Archive`) instead of deleting; reports incoming backlinks; keeps the
  wiki index in sync; refuses to overwrite existing archive targets
- **Whoosh-backed full-text index** (`search_index.py`, dependency `whoosh3`):
  - Incremental BM25 index over title (boost ×3, stemmed), body (stemmed),
    tags, folder, typ, status, uid, mtime — updated automatically on every write
  - `search_query` tool: Lucene-style syntax — field queries (`title:`,
    `tags:`, `folder:`, `typ:`, `status:`, `uid:`), boolean AND/OR/NOT,
    phrases `"…"`, wildcards `*`, fuzzy `term~N`, ranges
    `mtime:[2026-01-01 to 2026-12-31]`, ranked results, did-you-mean
  - `rebuild_index` and `index_status` tools
  - Index directory configurable via `OBSIDIAN_INDEX_DIR`
    (default `<vault>/.obsidian-mcp-index`)
  - Fixes: multi-word queries (`"Mayer Screener"`) now return results;
    plain `search_notes` remains unchanged as substring fallback

### Fixed

- **create() double-frontmatter bug**: content that already starts with a YAML
  header was wrapped in a second empty header (metadata lost, file corrupted);
  create() now parses existing frontmatter and merges tags instead of
  overwriting
- `mcp` dependency pinned `>=1.0.0,<2` — FastMCP API removed in mcp 2.x broke
  fresh installs

### Docs

- `docs/DEPLOYMENT.md`: Hermes-subprocess deployment (Linux server persistent
  across container rebuilds, Windows), update procedure

**Tests:** 122 → 167 · **Tools:** 18 → 22


## [0.6.0] — 2026-07-23 (Rev 6)

### Added — Differentiator Features

- **Output Schemas** (REQ-09): All 18 tools now expose `outputSchema` via Pydantic
  `BaseModel` return types. FastMCP auto-generates JSON Schema from the return
  annotations, giving LLM clients exact knowledge of response structures.
  No competing Obsidian MCP server implements this.
- **Vault Graph Analytics** (REQ-10):
  - `vault_graph()` — returns complete wikilink graph (nodes, edges, stats)
  - `find_orphans()` — identifies isolated notes with no incoming/outgoing links
  - `get_outlinks(path)` — returns outgoing wikilinks from a specific note
  - Code-block-aware wikilink extraction with deduplication
- **Vault Health Check** (REQ-11):
  - `vault_health()` — analyzes vault and returns 0–100 health score
  - Detects: broken wikilinks, untagged notes, empty notes, TODO/FIXME markers,
    duplicate titles
  - Weighted scoring with per-category penalties
- 25 new tests (TestOutputSchemas, TestVaultGraph, TestVaultHealth + server-layer tests)

### Research Sources
- MCP Specification 2025-06-18 (`modelcontextprotocol.io`)
- AWS Labs MCP Design Guidelines (`github.com/awslabs/mcp`)
- Peter Steinberger MCP Best Practices (`steipete.me`)
- MCP Best Practice Community Guide (`mcp-best-practice.github.io`)

## [0.5.0] — 2026-07-23 (Rev 5)

### Changed — AI-Optimized Tool Descriptions

- All 14 tool docstrings rewritten with 5-element pattern:
  1. Clear verb at start (Read, Create, Delete, Search, etc.)
  2. WHAT it does
  3. WHEN to use it (context for AI tool selection)
  4. WHAT it returns (JSON structure)
  5. SIDE EFFECTS (read-only, destructive, idempotent)
- Pydantic `Field(description=...)` added to every parameter with examples and constraints
- New `_actionable_message(e)` helper: exception-type-specific error messages that
  tell the AI what went wrong AND how to recover (e.g., "Note not found. Use
  list_notes to see available notes.")

## [0.4.0] — 2026-07-23 (Rev 4)

### Added — Feature Gap Closure

- `delete_note` — permanently delete a note (REQ-01)
- `append_to_note` — append content, creating note if missing (REQ-02)
- `manage_frontmatter` — get/set/delete YAML frontmatter keys (REQ-03)
- `manage_tags` — add/remove/list tags combining frontmatter + inline (REQ-04)
- `patch_note` — section-level edit under a Markdown heading (REQ-05)
- `daily_note` — read/append daily notes in YYYY-MM-DD format (REQ-06)
- `search_and_replace` — find/replace with regex support (REQ-07)
- 3 MCP Prompts: `session-start`, `session-end`, `project-checkin` (REQ-08)
- 64 new tests (97 total)

## [0.3.0] — 2026-07-23 (Rev 3)

### Fixed — Kimi Code Review (2 CRITICAL, 4 HIGH, ~10 MEDIUM)

- **C1**: `_serialize_value()` / `_serialize_metadata()` for JSON-safe datetime→ISO conversion
- **C2**: `McpError(ErrorData(code=..., message=..., data=...))` correct construction
- **H1**: `create()` raises `FileExistsError` if note already exists
- **H2**: Server-layer tests via `mcp.call_tool` / `mcp.read_resource`
- **H4**: Edge case tests for dates, invalid YAML, trash filtering, tag dedup
- **M1**: `_strip_code_blocks()` before title extraction
- **M2**: `_is_in_trash()` uses relative path parts
- **M3**: `list_notes()` skips unparseable YAML notes
- **M4**: `set_vault()` replaces `lru_cache` for testability
- **M5**: `mime_type='application/json'` on resource
- **M6**: Dev deps added (pytest, pytest-asyncio, mypy, ruff)
- **M7**: All tests use `tmp_path` fixtures
- **M8**: GitHub Actions CI workflow + `ruff.toml`
- **M9**: README security docs
- **M10**: BUILD_GUIDE present

## [0.2.0] — 2026-07-22 (Rev 2)

### Fixed — Manual Code Review

- Path traversal protection via `_resolve()` on all entry points
- Singleton Vault instance (replaced brittle `lru_cache`)
- Lightweight `list_note_paths()` (no file parsing)
- Tag deduplication via `set` union (frontmatter ∪ inline)
- Real MCP tool errors instead of string returns
- `FileExistsError` protection on `create()`
- `yaml.YAMLError` handling in `update_note` (broad `except Exception`)

## [0.1.0] — 2026-07-22 (Rev 1)

### Added — Initial Implementation

- 6 MCP Tools: `read_note`, `search_notes`, `list_notes`, `create_note`, `update_note`, `get_backlinks`
- 1 MCP Resource: `vault://structure`
- `Vault` class with filesystem operations
- `Note` dataclass with path, title, content, metadata, tags
- YAML frontmatter parsing via `python-frontmatter`
- Wikilink extraction for backlinks
- Mock vault in `examples/mock-vault/`
- `pyproject.toml` with PEP 621 metadata
- MIT License
- 10 initial tests

---

**Test progression:** 10 → 33 → 33 → 97 → 97 → 122 → **167**
**Tool progression:** 6 → 6 → 6 → 14 → 14 → 18 → **22**
