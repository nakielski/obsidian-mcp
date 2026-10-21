# Changelog

All notable changes to **obsidian-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `OBSIDIAN_HYBRID_ALL_TERMS_WEIGHT` (default 0, off): an optional extra
  full-text list with every query word required (AND) in the fusion
  (`all_terms_rank`). Off because it cost more than it gained on the
  reference question set; `eval --dump` records it for replays.
- `OBSIDIAN_SEARCH_DEMOTE` (folders or globs such as `raw/repos/*/*/`,
  default `**/node_modules/`) and `OBSIDIAN_SEARCH_DEMOTE_WEIGHT` (default
  0.5): mirrored third-party docs rank behind your own notes without being
  hidden; hits are marked `demoted` and take no reserved slot.
- `OBSIDIAN_HYBRID_CANDIDATES` (default 30): minimum list length per
  retriever before fusion. 60 and 100 measured worse.
- `obsidian-mcp-semantic eval --dump OUT.json` stores the raw ranked lists;
  `eval OUT.json --replay` re-ranks them offline with the current ranking
  settings. Reports name the settings they ran with.
- `obsidian-mcp-semantic eval FILE`: compares `search_query` and `hybrid_search`
  on a question set (YAML/JSON or a ```yaml block in a vault note) and reports
  hit@1/@5/@k, MRR and per-question ranks as Markdown or JSON.
- The server reconciles the full-text index with the disk once in the
  background on start (skipped if the shared index was reconciled within
  `OBSIDIAN_INDEX_RECONCILE_MAX_AGE`, default 900 s; off with
  `OBSIDIAN_INDEX_RECONCILE=0`), so machines without a sync loop pick up
  file-sync changes too.
- `OBSIDIAN_SEARCH_RESTRICTED` now applies to every search, listing and graph
  tool, not only `hybrid_search`: `search_notes`, `search_query`, `list_notes`,
  `list_note_paths`, `get_backlinks`, `vault_graph`, `find_orphans`,
  `vault_health` and the `vault://structure` resource hide restricted folders
  unless `include_restricted=true`; results report `restricted_hidden`.
  `read_note`/`get_outlinks` by path and listing a restricted folder by name
  stay allowed. Unset, nothing changes.
- **Optional semantic search** (`pip install 'obsidian-mcp[semantic]'`, `OBSIDIAN_SEMANTIC=1`):
  new tools `hybrid_search` (Whoosh full text + Qdrant vectors, Reciprocal Rank Fusion)
  and `semantic_sync`, plus the CLI `obsidian-mcp-semantic sync|status` for the first
  bulk indexing. Embeddings from Ollama or any OpenAI-compatible endpoint.
  Vectors are updated in the same write call as the full-text index; chunks are
  content-addressed, so edits embed only changed sections and moves reuse vectors.
  Folder restrictions (`OBSIDIAN_SEARCH_RESTRICTED`) are applied inside Qdrant;
  excluded folders (`OBSIDIAN_SEMANTIC_EXCLUDE`) are never embedded. Backend errors
  never fail a write: the note is queued and caught up by `semantic_sync`.
  See [docs/SEMANTIC_SEARCH.md](docs/SEMANTIC_SEARCH.md).
- Without the extra and the flag nothing changes: same 22 tools, `qdrant-client`
  is never imported.

### Fixed
- Full-text index drift after out-of-band changes: notes moved, edited or
  deleted outside the server (Syncthing, Obsidian editor, a second server)
  stayed in the Whoosh index under the old path and were missing under the new
  one. `VaultIndex.reconcile()` compares path and file signature and fixes only
  what differs; `obsidian-mcp-semantic sync` runs it on every pass. Full-text
  searches skip hits whose file is gone. The index gains a stored `fsig` field;
  an older index is rebuilt once on the first reconcile.
- `hybrid_search` variants: the newest live copy (highest version, then date in
  the file name) represents the group instead of the best-scored one.
- `hybrid_search` buried the best semantic matches of natural-language questions:
  with RRF (k=60) any note found by both retrievers - even a loose OR word
  match at rank 30 - outranked the best note found by meaning only. The three
  best hits of each retriever are now always part of the result.
- `hybrid_search` folds versions and copies of one note (drafts, archive
  copies, raw records) into one hit with a `variants` list, preferring a copy
  outside `OBSIDIAN_SEARCH_SECONDARY` (default `06 Archive,raw`).
- Semantic sync on a slow (CPU-only) embedding server: an embedding request that
  times out is split in halves and retried down to a single section, so long
  notes no longer fail as a whole. Default `OBSIDIAN_EMBED_BATCH` lowered from
  16 to 8.
- Semantic sync no longer stalls on note-specific failures: after three failures
  in a row it probes Ollama/Qdrant and only stops when they are really down.
  Before, failed notes were retried first on every run and ended each run
  after three notes. The sync result now includes `last_error`.
- `archive_note` left the full-text index pointing at the old path; the archived
  note is now indexed under its new location.

### Tests
- 406 tests (54 new); the Qdrant tests run against Qdrant's in-process local mode
  and are skipped when the extra is not installed. CI runs both variants.

## [2.0.0] — 2026-10-19 — Write-log provenance and spec-conform output schemas

### Added
- **`related_to` on every write tool** (`create_note`, `update_note`, `delete_note`,
  `archive_note`, `append_to_note`, `manage_frontmatter`, `manage_tags`, `patch_note`,
  `daily_note`, `search_and_replace`): optional work-item uid, written as `related_to`
  into the automatic write-log line. It was the only field agents still added by hand,
  which led them to append duplicate lines with invented timestamps to the legacy
  `.vault-write-log.jsonl` next to the server's own shard entries (observed 2026-10-15).
  Validated as a uid-like token (letters, digits, `.` `_` `:` `-`, max 80); an invalid
  value fails the call before anything is written.
- **Server instructions** (MCP `instructions`): the server writes the write log and
  `wiki/index.json` itself; clients never append log lines or edit the index.
- Denials for the bookkeeping files now say why there is nothing to do by hand.

### Changed (breaking for direct JSON consumers)
- **MCP-spec-conform outputSchema**: `search_notes`, `list_notes`, `list_note_paths`,
  `get_backlinks`, `find_orphans` and `get_outlinks` now return `{"results": [...]}`
  instead of a bare list. The MCP spec requires `outputSchema.type == "object"`; strict
  clients (e.g. Hermes) rejected the array schemas and parked the whole server.
  LLM clients adapt automatically; scripts reading the structured output need `["results"]`.

### Tests
- `test_all_output_schemas_are_objects` guards against array-typed output schemas.
- Test suite: 352 tests, all passing.

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
