# Changelog

All notable changes to context-pager.

## [0.3.2] - 2026-08-22

### Fixed
- **Bridge timeout on cold start**: relay tool timeout increased from 30s to
  120s. Laptops loading bge-m3 + llmlingua-2 can take 30–60s on first call.
- **WebSocket disconnects during model loading**: `build_embedder()` and
  `build_compressor()` now run in `loop.run_in_executor()` to avoid blocking
  the event loop and killing WebSocket ping/pong keepalive.
- **Bridge can't handle concurrent tool calls**: tool handlers in the bridge
  message loop now run as concurrent `asyncio.create_task` instead of blocking
  the message loop sequentially. Slow tool calls no longer prevent the bridge
  from receiving new requests or responding to pings.

## [0.3.0] - 2026-08-14

### Fixed
- **Compressor path was unreachable**: `chunks_per_page` was derived from
  `max_return_tokens`, so every page fit the budget and `compress_document`
  always short-circuited (LLMLingua never ran). Pages are now a fixed canonical
  window (`CHUNKS_PER_PAGE = 4`); `max_return_tokens` controls compression
  effort instead of page sizing.
- **LLMLingua crashed on CPU laptops**: the compressor passed no `device_map`,
  so llmlingua defaulted to `cuda` and raised "Torch not compiled with CUDA".
  It now selects `cpu` unless CUDA is available, and uses llmlingua-2's
  `target_token` instead of `rate`.
- **Stale envelope fields on page-cache hits**: when a `focus_area` call and a
  sequential call resolved to the same content page, the cached response kept
  the first caller's `page`/`pages_total`/`next_page`/`focus_applied`. These are
  now rewritten per request on cache hit.

### Added
- Opt-in real-model smoke tests (`RUN_MODEL_TESTS=1`, plus
  `PAGER_MODEL_SMOKE_FULL=1` for bge-m3 + llmlingua-2): index, search,
  PII-masked compression, focus re-rank, memory commit + recall against real
  models. The full path is verified CPU-safe.
- `deploy/` assets: `setup_relay.sh`, Caddyfile, systemd units (relay + bridge),
  nightly SQLite backup script with optional S3.
- `CONTEXT.md` (authoritative spec) and `CONNECTING_AGENTS.md` (per-client MCP
  configs). README rewritten for the relay/bridge architecture.

## [0.2.0] - 2026-08-12

### Added
- v2 rewrite on the relay/bridge model: a thin AWS-free-tier relay (auth,
  rate limit, WSS channel, usage rollup — zero content) and a laptop bridge
  owning documents (sqlite-vec), embeddings (BGE-m3 / lite bge-small), LLMLingua-2
  compression, Presidio PII masking, long-term memory, and telemetry.
- Two-key security model (`pgr_agent_*` bearer / `pgr_bridge_*` WSS handshake,
  hashed at rest, 5 signups/IP/day).
- MCP tools: `compress_document` (canonical pages, focus re-ranking, LRU cache,
  small-page short-circuit), `search_documents` (dense KNN + silent memory
  recall), `commit_to_long_term_memory`.
- `pager` CLI: `serve`, `bridge`, `docs add|list|reindex|remove`, `stats`.
- 22 tests: unit (fake models), bridge↔relay, full E2E over real HTTP + WSS.
