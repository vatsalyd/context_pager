# Context Pager

A **context-paging MCP tool** that cuts agent token cost. Agents keep a tiny context
window and pull only the pages they need — the way an OS pages memory.

- `compress_document` — read one compressed page of a document instead of the whole file.
- `search_documents` — find relevant documents (and silently recalled memory).
- `commit_to_long_term_memory` — persist durable insights that resurface on later searches.

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

- **Relay** (thin, AWS free tier): authenticates agent keys, rate-limits, and forwards
  tool calls to your live bridge over a persistent WSS channel. **Zero ML, zero content
  storage** — it never sees documents, only envelopes and daily usage rollups.
- **Bridge** (on your laptop): owns the document library (sqlite-vec), embeddings
  (BGE-m3), compression (LLMLingua-2), PII masking (Presidio), long-term memory, and
  telemetry. It dials **out** to the relay.
- **Agent**: any MCP client. Config is a single URL + bearer token
  (see [`CONNECTING_AGENTS.md`](CONNECTING_AGENTS.md)).

## Quick Start

### Bridge (your laptop — owns your documents)

```bash
pip install "context-pager[bridge]"
pager bridge                       # preloads models, serves 127.0.0.1:8000/mcp
```

Local-only works with no keys. To relay through the internet, sign up and set the
bridge key:

```bash
curl -X POST https://pager.duckdns.org/v1/signup   # returns agent_key + bridge_key
export PAGER_BRIDGE_KEY="pgr_bridge_..."           # PAGER_BRIDGE_WS_URL defaults to the relay
pager bridge
```

Manage documents and see cost savings:

```bash
pager docs add report.txt                          # copies + chunks + embeds
pager docs list | reindex <id> | remove <id>
pager stats                                        # tokens saved, cost saved
```

### Agent (any MCP client)

Connect to `https://pager.duckdns.org/mcp` with `Authorization: Bearer pgr_agent_...`
(or `http://127.0.0.1:8000/mcp` for local-only). Claude Code / Cursor / curl snippets
are in [`CONNECTING_AGENTS.md`](CONNECTING_AGENTS.md). A complete reference agent is in
[`examples/goldfish_agent/`](examples/goldfish_agent/).

### Relay (your own $0/mo server, optional)

If you don't want to use the shared relay, provision your own:

```bash
PAGER_DUCKDNS_DOMAIN=pager PAGER_DUCKDNS_TOKEN=... bash deploy/setup_relay.sh
```

See [`CONTEXT.md`](CONTEXT.md) for the full architecture, tool contracts, configuration,
and security model, and `deploy/` for the relay provisioning script, systemd units,
Caddyfile, and backup script.

## Tools

All tools return a JSON envelope string (agent-friendly, never raises for recoverable
conditions). Full contracts with examples live in [`CONTEXT.md`](CONTEXT.md).

| Tool | Purpose |
|---|---|
| `compress_document(doc_id, page=1, focus_area=None, max_return_tokens=2048)` | Return one compressed, PII-masked page. Pages are canonical; `focus_area` re-ranks by relevance. |
| `search_documents(query, top_k=10)` | KNN over chunk embeddings; best chunk per doc wins; `best_page` + `recalled_insights` included. |
| `commit_to_long_term_memory(key, insights)` | Persist durable insights; recalled on later searches (cosine ≥ 0.78). |

## Testing

```bash
python -m pytest tests/          # unit + bridge↔relay + full E2E over real HTTP/WSS
python -m ruff check src tests
```

Opt-in real-model smoke tests: `RUN_MODEL_TESTS=1` (not run in CI).

## Known Limitations (v1)

- Text/markdown/code only; PDFs/DOCX must be converted to text first.
- Dense-only retrieval (no sparse/RRF).
- Models run on your laptop; very large libraries need `PAGER_LITE=true` or a beefier box.
- No admin UI; manage documents via `pager docs`.

## License

MIT
