# Changelog

All notable changes to **obsidian-mcp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-09-16

### Added — Initial public release

- **MCP server** exposing an Obsidian vault to LLM clients over stdio
  (22 tools, 3 prompts, 1 resource)
- **Core note operations**: read, search, create, update, delete, append (upsert),
  section-level patching under headings, literal and regex search-and-replace
- **Frontmatter and tag management**: get/set/delete keys, add/remove/list tags
  (frontmatter + inline, deduplicated, case-insensitive)
- **Daily notes**: read/append with YYYY-MM-DD validation
- **Graph analytics**: full wikilink graph, orphan detection, outlink tracing
- **Vault health check**: 0–100 quality score with per-category issue report
  (broken links, untagged notes, empty notes, TODO/FIXME markers, duplicate titles)
- **Vault write policies** enforced at the filesystem choke point: write log
  (`.vault-write-log.jsonl`), raw/ immutability, agent locking (`.vault-lock`),
  wiki/index.json upkeep, advisory link checking — each disableable via env var
- **Archive instead of delete**: `archive_note` moves notes to the archive folder
  and reports incoming backlinks
- **Whoosh-backed full-text index**: incremental BM25 ranking, Lucene-style query
  syntax (field queries, AND/OR/NOT, phrases, wildcards, fuzzy, date ranges,
  did-you-mean), configurable index directory
- **Structured output schemas** on every tool via Pydantic models
- **Actionable error messages** with recovery hints for the calling LLM
- **Mock vault** in `examples/mock-vault/` for instant start without configuration
- **Test suite** (167 tests) covering the filesystem layer, server tools, policies,
  and the search index; CI on Python 3.10–3.12 with pytest + ruff
