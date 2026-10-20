# Semantic search (optional)

`hybrid_search` finds notes by meaning and by words at the same time: the
Whoosh full-text index and a vector index in [Qdrant](https://qdrant.tech/)
each produce a ranked list, and Reciprocal Rank Fusion merges them.

The feature is **off by default** and **optional**. Without it the server runs
exactly as before: same 22 tools, no extra dependencies, no network calls,
`qdrant-client` is never imported.

## Why inside the server

A vector index is a copy of your notes. Any copy that agents can query
directly is a path around the server's policies (layers, deny-list,
restricted folders). So the vectors live behind the same choke point as every
other read and write:

- every write through a tool updates the note's vectors in the same call
- folder restrictions are applied **inside Qdrant**, before the top-k cut, so
  hidden notes can never crowd out allowed ones
- excluded folders are never embedded at all

Give only this server the Qdrant API key, and do not expose the collection to
other tools.

## Setup

```bash
pip install 'obsidian-mcp[semantic]'      # adds qdrant-client
ollama pull bge-m3                          # multilingual embedding model
```

Environment:

| Variable | Default | Purpose |
|---|---|---|
| `OBSIDIAN_SEMANTIC` | `0` | `1` enables the semantic index and registers `hybrid_search` and `semantic_sync` |
| `OBSIDIAN_QDRANT_URL` | `http://localhost:6333` | Qdrant server (>= 1.10) |
| `OBSIDIAN_QDRANT_API_KEY` | – | Qdrant API key (recommended) |
| `OBSIDIAN_QDRANT_COLLECTION` | `obsidian_mcp_<model>` | Collection name; the default contains the model, so a model switch never mixes vector spaces |
| `OBSIDIAN_EMBED_API` | `ollama` | `ollama` (`/api/embed`) or `openai` (any OpenAI-compatible `/embeddings` endpoint) |
| `OBSIDIAN_EMBED_URL` | `http://localhost:11434` (`…/v1` for `openai`) | Embedding endpoint |
| `OBSIDIAN_EMBED_MODEL` | `bge-m3` | Embedding model |
| `OBSIDIAN_EMBED_API_KEY` | – | Bearer token for OpenAI-compatible endpoints |
| `OBSIDIAN_EMBED_QUERY_PREFIX` / `OBSIDIAN_EMBED_DOC_PREFIX` | empty | Instruction prefixes for models that need them (e.g. E5, Qwen3-Embedding) |
| `OBSIDIAN_SEARCH_RESTRICTED` | empty | Comma-separated folders hidden from `hybrid_search` unless `include_restricted=true` (still embedded) |
| `OBSIDIAN_SEMANTIC_EXCLUDE` | empty | Comma-separated folders that are never embedded |
| `OBSIDIAN_SEMANTIC_MIN_SCORE` | `0` (off) | Drop vector hits below this cosine similarity; tune per model |
| `OBSIDIAN_SEMANTIC_CHUNK_CHARS` | `2000` | Max characters per chunk |
| `OBSIDIAN_EMBED_BATCH` | `8` | Chunks per embedding request; a request that times out is split in halves and retried |
| `OBSIDIAN_EMBED_TIMEOUT` / `OBSIDIAN_QDRANT_TIMEOUT` | `20` / `10` s | Request timeouts (per request, so per batch) |
| `OBSIDIAN_SEMANTIC_BACKOFF` | `60` s | Pause after a backend error before the next write tries again |
| `OBSIDIAN_SEMANTIC_INLINE_MAX` | `32` | A write needing more new chunk embeddings than this is queued for `semantic_sync` instead of blocking the tool call |

Folder values are case-insensitive and match whole folders: `05 People`
covers `05 People/…` but not `05 Peoplex/…`.

## First indexing

Run the bulk indexing once from the shell (it can take a while on a CPU-only
embedding server), with the same environment as the MCP server:

```bash
obsidian-mcp-semantic sync          # embed everything new or changed
obsidian-mcp-semantic status        # collection, points, pending notes
```

`sync` is a delta: unchanged notes cost no embedding call, so running it
again (e.g. from cron every 15 minutes on one machine) is cheap. It also
removes vectors of notes deleted or moved outside the server, e.g. by a file
sync. Several machines sharing one vault can share one collection: point ids
are derived from path and content, so concurrent writers converge.

## How it works

- **Chunks**: a note is split at H1–H3 headings (`#` inside code fences is
  ignored); long sections at paragraph boundaries; tiny sections are merged
  into the previous chunk. Title and heading path are embedded with the text.
- **Delta updates**: each chunk's id is `uuid5(path + sha256(embedded text))`.
  An edit embeds only the changed sections and deletes vanished ones (after
  the upsert, so a note is never without vectors). Tag or frontmatter changes
  only relabel the payload. `archive_note` reuses the stored vectors.
- **Failures never break writes**: if Ollama or Qdrant is down the write
  completes, the note is recorded in `semantic-pending.json` (next to the
  Whoosh index, machine-local) and the next `semantic_sync` catches it up.
  `hybrid_search` answers from full text meanwhile and says so in `mode` and
  `warning`. After three failures in a row a sync probes both backends with
  one tiny request: if they are down it stops instead of waiting out a timeout
  per note, otherwise the failures were note-specific and it carries on. The
  sync result reports `last_error`. A sync that reached the backend resumes
  inline indexing at once.
- **Slow embedding servers**: Ollama embeds the inputs of one request one
  after another, so on a busy CPU a batch of long sections can outlast the
  timeout. Such a batch is split in halves until it fits; only a single
  section that still times out counts as a failure (raise
  `OBSIDIAN_EMBED_TIMEOUT` then). Connection errors are not split.
- **Fusion**: `score = Σ 1 / (60 + rank)` over both lists. A note found by
  both retrievers ranks above one found by a single retriever at the same
  rank. Natural-language questions use OR semantics on the full-text side.

## Tools

| Tool | Description |
|---|---|
| `hybrid_search(query, limit?, folder?, include_restricted?)` | Notes ranked by full text + meaning; each hit names the best-matching section, a snippet and which retriever found it |
| `semantic_sync(max_seconds?, status_only?)` | Catch up pending or out-of-band changes within a time budget; reports collection status |

`search_notes`, `search_query`, `rebuild_index` and `index_status` are
unchanged. `rebuild_index` rebuilds the full-text index only.
