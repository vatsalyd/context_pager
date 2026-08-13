# Context Pager

A **context-paging MCP tool** that cuts agent token cost. Agents keep a tiny context
window and pull only the pages they need, the way an OS pages memory.

- **compress_document** — read one compressed page of a document instead of the whole file.
- **search_documents** — find relevant documents (and silently recalled memory).
- **commit_to_long_term_memory** — persist durable insights that resurface on later searches.

## Architecture

```
┌─────────────┐  MCP over HTTPS (bearer: pgr_agent_*)   ┌──────────────┐
│    Agent    │ ───────────────────────────────────────► │   RELAY      │
│ (Claude,    │  POST https://pager.duckdns.org/mcp      │ AWS t3.micro │
│  Cursor, …) │                                          │ dumb router  │
└─────────────┘                                          │ $0/mo, 0 ML  │
                                                         │ SQLite: keys │
                        WSS (handshake: pgr_bridge_*)    │ + usage rollup│
┌─────────────┐  JSON-RPC   ───────────────────────────► │              │
│   BRIDGE    │  wss://pager.duckdns.org/bridge          └──────────────┘
│   (laptop)  │ ◄───────────────────────────────────────
│ models +    │
│ sqlite-vec  │
└─────────────┘
   │ also serves localhost:8000/mcp (no auth, loopback only)
```

- **Relay** (thin, on AWS free tier): authenticates agent keys, rate-limits, and forwards
  tool calls to the user's live bridge over a persistent WSS channel. **Zero ML, zero
  content storage.** It never sees documents; it only sees envelopes and stores daily
  usage rollups (counts, token totals, cost saved).
- **Bridge** (on the user's laptop): owns the document library (sqlite-vec), embeddings
  (BGE-m3), compression (LLMLingua-2), PII masking (Presidio), long-term memory, and
  telemetry. It dials **out** to the relay (the relay holds no outbound connections).
- **Agent**: any MCP client. Config is a single URL + bearer token.

## Tool Contracts

All tools return a **JSON envelope string**. Agents should `json.loads` the result and
read the fields below. Tools never fail loudly for recoverable conditions — they return
an error envelope with `metadata.retryable`.

### compress_document(doc_id, page=1, focus_area=None, max_return_tokens=2048)

```
{
  "tool": "compress_document",
  "doc_id": "abc123def456",
  "page": 1,
  "pages_total": 3,
  "content": "<compressed text>",
  "token_count": 512,
  "next_page": 2,            // null on the last page
  "metadata": {
    "original_tokens": 2200,
    "compressed_tokens": 512,
    "compression_ratio": "4.3x",
    "cost_saved_usd": 0.0007,
    "pii_redacted": {"PERSON": 2},
    "cache_hit": false,
    "skipped_compression": false,  // true when the page already fit the budget
    "elapsed_ms": 812
  }
}
```

- **Pages are canonical**: `page` maps to a fixed chunk window regardless of
  `max_return_tokens`, so a `best_page` from search stays valid for later calls.
- `focus_area` re-ranks pages by semantic similarity to the query, then returns the
  requested page **in the new order** (page 1 = most relevant to the focus).
- Small pages short-circuit (no LLM compression, `skipped_compression: true`).
- PII is redacted before compression and re-checked after.

### search_documents(query, top_k=10)

```
{
  "tool": "search_documents",
  "query": "revenue targets",
  "results": [
    {"doc_id": "abc123def456", "title": "acme_corp_2024", "snippet": "…",
     "best_page": 2, "score": 0.91}
  ],
  "recalled_insights": ["Recalled insight: acme_q3: Q3 revenue target is 42M."],
  "metadata": {"results_returned": 3, "elapsed_ms": 45}
}
```

- `recalled_insights` is **silently injected** long-term memory matching the query
  (cosine similarity ≥ 0.78). Treat it as ground truth the agent wrote earlier.
- `best_page` is the 1-indexed canonical page containing the best chunk.

### commit_to_long_term_memory(key, insights)

```
{"tool": "commit_to_long_term_memory", "key": "acme_q3", "status": "persisted", "metadata": {...}}
```

- Overwrites any prior value for `key`. Recall threshold 0.78.

### Error envelope

```
{"tool": "<tool>", "error": "message", "metadata": {"retryable": true}}
```

`retryable: true` means the failure is transient (bridge offline, rate limit, bridge
timeout) — retry with backoff. `false` means permanent (bad key, missing doc, bad args).

## Data Flow

1. **Index**: `pager docs add file.txt` copies the file into `~/.pager/docs/`, chunks it
   into 512-token non-overlapping windows, embeds them, and writes rows to the sqlite-vec
   index. Reindex/remove use the same `doc_id`.
2. **Search**: query is embedded once; KNN over chunks; best chunk per doc wins.
3. **Compress**: page → chunk window → PII mask → LLMLingua-2 (or truncation in lite
   mode) → re-mask. Results cached in a 64 MB in-process LRU.
4. **Memory**: upsert = embed + store; recall = embed query + cosine filter.

## Configuration

| Variable | Used by | Default |
|---|---|---|
| `PAGER_BRIDGE_KEY` | bridge (WSS auth) | — (required to relay) |
| `PAGER_BRIDGE_WS_URL` | bridge | `wss://pager.duckdns.org/bridge` |
| `PAGER_LITE` | bridge | `false` (bge-small + truncation when `true`) |
| `PAGER_ROOT` | bridge | `~/.pager/docs` |
| `PAGER_DB` | bridge | `~/.pager/pager.db` |
| `PAGER_TELEMETRY_DB` | bridge | `~/.pager/telemetry.db` |
| `PAGER_LOCAL_MCP_HOST/PORT` | bridge | `127.0.0.1:8000` |
| `PAGER_EMBEDDING_MODEL` / `PAGER_LLMLINGUA_MODEL` | bridge | BGE-m3 / llmlingua-2 |
| `PAGER_CHUNK_TOKENS` / `PAGER_MAX_RETURN_TOKENS` | bridge | `512` / `2048` |
| `PAGER_RELAY_HOST/PORT` | relay | `0.0.0.0:8000` |
| `PAGER_MCP_PATH` / `PAGER_BRIDGE_PATH` | relay | `/mcp` / `/bridge` |
| `PAGER_SQLITE_DB` | relay | `users.db` |
| `PAGER_PUBLIC_URL` | relay | `https://pager.duckdns.org` |
| `PAGER_RATE_LIMIT_CALLS_PER_HOUR` | relay | `100` |
| `PAGER_MAX_BRIDGES_PER_KEY` | relay | `2` |
| `PAGER_SIGNUP_PER_IP_PER_DAY` | relay | `5` |

## Security Model

- **Two keys per user** (issued once at signup, stored as sha256, never shown again):
  - `pgr_agent_*` — bearer token authenticating MCP calls to the relay.
  - `pgr_bridge_*` — handshake key for the bridge's outbound WSS channel.
- Agent key → user → bridge routing; a bridge only serves its own user's agents.
- In-memory token-bucket rate limit (100 calls/hour/key); max 2 bridges per key.
- **localhost MCP has no auth** — it binds `127.0.0.1` only.
- No documents ever transit the relay. PII masking happens on the laptop.

## Deployment

- **Relay** (AWS, ~$0/mo): `deploy/setup_relay.sh` provisions a t3.micro, installs
  `context-pager[relay]` in a venv, runs it under systemd behind Caddy with a DuckDNS
  domain. Nightly SQLite backup with 7-day retention (optional S3). No Docker.
- **Bridge** (laptop): `pip install context-pager[bridge]`, set `PAGER_BRIDGE_KEY`, run
  `pager bridge`. It preloads models before connecting, reconnects with backoff, and can
  run bridge-only (no key) for pure-local use.
- See `CONNECTING_AGENTS.md` for per-client MCP config snippets.

## Testing

| Layer | How |
|---|---|
| Unit (core) | `tests/test_core.py` — fake embedder/compressor, real sqlite-vec |
| Bridge↔relay | `tests/test_bridge.py`, `tests/test_relay.py` — real code, scripted fake peers |
| E2E | `tests/test_e2e.py` — real relay + real bridge + real MCP client over HTTP/WSS |
| Real models | opt-in smoke tests behind `RUN_MODEL_TESTS=1` (not run in CI) |

Run: `python -m pytest tests/` and `python -m ruff check src tests`.

## Known Limitations (v1)

- Text/markdown/code only; PDFs/DOCX must be converted to text first.
- Dense-only retrieval (no sparse/RRF — the payoff was never measured).
- v1 models run on the user's laptop; very large libraries need lite mode or a beefier box.
- No admin UI; manage documents via `pager docs add|list|reindex|remove` and see
  `pager stats` for cost savings.
