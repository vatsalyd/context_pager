# TECH_STACK.md — Final Technology Stack

## Overview
This document records every technology choice with its rationale. The stack is split into **Hosted Free Tier** (what runs on our Oracle VM) and **Self-Hosted Default** (what ships in `docker-compose.yml` for users).

---

## Core Protocol & Transport

| Layer | Choice | Version | Why |
|-------|--------|---------|-----|
| **MCP Server Framework** | `fastmcp` | 3.4.4 | `@mcp.tool` decorator auto-generates JSON schemas from type hints + docstrings; powers ~70% of MCP servers; streamable HTTP transport native |
| **MCP Transport** | Streamable HTTP (port 8000 internal) | — | Required for public multi-tenant serving; stdio is local-only |
| **HTTP Reverse Proxy / TLS** | Caddy | 2.x | Auto-HTTPS via Let's Encrypt; 5-line config; handles DuckDNS subdomains |
| **Free Domain** | DuckDNS | — | Free `*.duckdns.org` subdomain + Let's Encrypt support |

---

## Agent Framework & Client Bridge

| Layer | Choice | Version | Why |
|-------|--------|---------|-----|
| **Agent Orchestration** | LangGraph | latest | Stateful graph execution; prebuilt `ToolNode`, `tools_condition` |
| **Agent Factory** | `create_agent` (LangChain) | latest | Implicit ReAct tool-calling loop; middleware support for context limits |
| **Reference Agent LLM** | Google Gemini 2.5 Flash | via `langchain-google-genai` | User-selected; 1M context window, cheap pricing |
| **MCP ↔ LangGraph Bridge** | `langchain-mcp-adapters` | 0.3.0 | Official bridge; `MultiServerMCPClient` converts MCP tools → `BaseTool` |
| **Client Config** | `tool_name_prefix=True`, server alias `"pager"` | — | Avoids collisions if user connects other MCP servers (GitHub, etc.) |

---

## Context Management & Summarization

| Layer | Choice | Version | Why |
|-------|--------|---------|-----|
| **Context Limit Enforcement** | `SummarizationMiddleware` | LangGraph built-in | Only mechanism for strict token limits on `create_agent`; `trigger=("tokens", 8000)`, `keep=("messages", 6)` |
| **Custom Middleware** | `ProtectLatestToolResultMiddleware` (custom) | — | Self-summarizes latest tool result via cheap LLM *before* main summarization runs; guarantees agent never loses the page it just fetched (Q7 Option B) |
| **Recursion Limit** | `config={"recursion_limit": 100}` | — | 50 tool-call budget + reasoning turns; prompt-injected budget rule |

---

## Embeddings & Vector Search

| Layer | Choice | Version | Why |
|-------|--------|---------|-----|
| **Embedding Model** | BGE-m3 via `FlagEmbedding` | 1.4.0 | 1024-dim dense, sparse, ColBERT multi-vector in one `encode()`; 8192 max tokens; 100+ languages |
| **Embedding Runtime** | `BGEM3FlagModel('BAAI/bge-m3', use_fp16=False)` on ARM CPU | — | Oracle VM is ARM; FP16 not beneficial on CPU; batch_size=12 |
| **Vector Database** | PostgreSQL 16 + pgvector | 0.8.x | Self-hosted on Oracle VM (24 GB RAM); no managed-tier limits; HNSW indexes |
| **Dense Vector Type** | `vector(1024)` | — | BGE-m3 dense dimension; HNSW `vector_cosine_ops` (vectors are normalized) |
| **Sparse Vector Type** | `sparsevec(N)` (pgvector native) | — | BGE-m3 lexical weights; HNSW `sparsevec_cosine_ops`; `jsonb`+GIN as fallback |
| **Hybrid Fusion** | Reciprocal Rank Fusion (k_const=60) | — | Fuses dense + sparse scores at **document-level** (for `compress_document`) and **entity-level** (for `fetch_entity_graph`); avoids chunk-unit return problem |

---

## GraphRAG (Custom, No External Dependency)

| Layer | Choice | Why |
|-------|--------|-----|
| **Entity Extraction** | spaCy NER (`en_core_web_sm`) + regex | Already installed for Presidio; extracts PERSON, ORG, GPE, DATE, MONEY, EMAIL, PHONE, SSN, URL, IBAN |
| **Relation Typing** | Optional Ollama Llama 3 8B per entity pair (free tier: `MENTIONED_WITH` catch-all) | Self-hosters with GPU get typed relations; free tier skips LLM call for speed |
| **Graph Traversal** | BFS over `entity_relations` table | Filtered by required `relation` param; bounded by `limit` (default 20) |
| **Pagination** | Opaque `page_token` encoding `(offset, relation_filter_hash)` | Agent can scroll full graph without context overflow (Q18 Fix 1 + Fix 2 combined) |

---

## Context Compression

| Layer | Choice | Version | Why |
|-------|--------|---------|-----|
| **Primary (Hosted Free Tier)** | LLMLingua-2 via `PromptCompressor` | `microsoft/llmlingua-2-xlm-roberta-large-meetingbank`, `use_llmlingua2=True` | Task-agnostic token classification; 3–6x faster than v1; CPU-feasible on Oracle ARM |
| **Fallback (Self-Hosted Only)** | Two-stage: LLMLingua-2 density reduction → Ollama Llama 3 8B query-focused extraction over reduced text | Only when `config.ollama_url` is set | Honest about capabilities: hosted free = density only; self-hosted + GPU = query-conditional |
| **Short-Circuit Guard** | Skip compression if `input_tokens <= max_return_tokens` | — | Prevents crash/confusion on tiny docs (Q15) |
| **Default Return Budget** | `max_return_tokens=2048` (exposed as tool param) | — | Leaves room in 8k agent context for reasoning + next tool call (Q16) |
| **Focus Area Handling** | Ignored under LLMLingua-2 (task-agnostic); honored under two-stage mode | Q4 honesty preserved |

---

## PII Redaction

| Layer | Choice | Why |
|-------|--------|-----|
| **Engine** | Presidio Analyzer + spaCy `en_core_web_sm` | Context-aware NER; catches names, emails, phones, SSNs, credit cards, etc. |
| **Placement** | **Pre-compression** on raw text (Q1) | Compressor never sees PII; agent never receives PII; cost savings improve |
| **Defense-in-Depth** | Second scan on compressed output | Catches any residual PII that survived compression |

---

## Data Layer (PostgreSQL + pgvector)

| Table | Key Columns | Isolation |
|-------|-------------|-----------|
| `documents` | `id`, `tenant_id`, `title`, `content`, `source_kind`, `metadata`, `created_at` | RLS on `tenant_id` |
| `document_chunks` | `id`, `tenant_id`, `document_id`, `chunk_index`, `text`, `embedding vector(1024)`, `sparse_weights sparsevec`, `created_at` | RLS; HNSW indexes on both dense + sparse |
| `entities` | `id`, `tenant_id`, `document_id`, `type`, `name`, `properties`, `embedding vector(1024)`, `sparse_weights sparsevec`, `created_at` | RLS; sparse on `f"{name} ({type})"` only (Q17) |
| `entity_relations` | `from_id`, `to_id`, `relation`, `properties`, `created_at` | RLS via join to `entities` |
| `agent_memory` | `key`, `tenant_id`, `insights`, `embedding vector(1024)`, `created_at`, `last_recalled` | RLS; silent recall in `fetch_entity_graph` (cosine > 0.78) |
| `audit_events` | `id`, `tenant_id`, `event_type`, `tool_name`, `session_id`, `doc_id`, `original_tokens`, `compressed_tokens`, `cost_saved_usd`, `metadata`, `created_at` | RLS; per-request telemetry |
| `tenant_usage_daily` | `date`, `tenant_id`, `tool_calls`, `tokens_compressed`, `storage_bytes`, `est_cost_usd` | RLS; 5-min cron rollup |
| `users` | `id`, `email`, `hashed_api_key` (SHA-256), `api_key_prefix`, `tenant_id`, `plan`, `created_at` | Auth lookup |

**RLS Policy (all tables):**
```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <table>
  USING (tenant_id = current_setting('app.tenant_id', true));
```

**Connection Pattern (per request):**
```python
async with pool.acquire() as conn:
    await conn.execute("SET LOCAL app.tenant_id = $1", tenant_id)
    # all subsequent queries auto-filtered by RLS
```

---

## Caching & Decay

| Layer | Choice | Why |
|-------|--------|-----|
| **Hot Page Cache** | Redis 7-alpine, ZSET keys `pager:cache:{tenant_id}:{doc_id}:{focus_hash}:{max_tokens}` | Content-addressed memoization; identical `(doc_id, focus_area, target_tokens)` across sessions reuses work |
| **Per-Session Decay Tracking** | Redis ZSET `pager:active:{tenant_id}:{session_id}` with score = `last_touched_ts` | Server-internal only; tracks which pages are "live" for decay |
| **Decay Function** | `relevance = base_score * exp(-λ * age_minutes)` with λ=0.01 (~70 min half-life) | Server-internal; no agent-facing surface (Q8) |
| **Eviction** | Background job removes cache entries when decay score < threshold | Keeps Redis bounded |

---

## Rate Limiting

| Resource | Free Tier Limit | Implementation |
|----------|----------------|----------------|
| Tool calls / hour | 100 | Redis sliding-window leaky bucket per `(tenant_id, "tool_calls")` |
| Compression tokens / day | 500,000 | Redis counter per `(tenant_id, "tokens_today")` with TTL 24h |
| Documents stored | 100 | `COUNT(*) FROM documents WHERE tenant_id = ?` |
| Concurrent sessions | 2 | Redis ZSET `pager:active:{tenant_id}` cardinality check |

Returns MCP error `429 TOO_MANY_REQUESTS` with friendly message.

---

## Authentication & Tenancy

| Layer | Choice | Why |
|-------|--------|-----|
| **Auth Scheme** | Bearer API keys (`pgr_xxx...`, 32 random bytes) | Simple, standard, works over HTTP |
| **Key Storage** | `users` table: `hashed_api_key = SHA256(key)`, `api_key_prefix = key[:8]` for fast lookup | Prefix index for O(1) candidate fetch; full hash verify |
| **Middleware** | Starlette HTTP middleware wrapping FastMCP | Extracts `Authorization: Bearer`, validates, sets `request.state.tenant_id`; FastMCP tools read via `ctx.request.state.tenant_id` |
| **PG Session Variable** | `SET LOCAL app.tenant_id = $1` on connection acquire | RLS auto-filters all queries; zero leakage risk |

---

## Ingestion (HTTP REST, Not MCP)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/documents` | POST (multipart) | Upload document; returns `{doc_id, status: "processing"}` |
| `/v1/documents/{id}` | GET | Check embedding/extraction status → `{status: "ready", chunks: N, entities: M}` |
| `/v1/documents` | GET | List tenant's documents |
| `/v1/documents/{id}` | DELETE | Cascade delete doc + chunks + entities |

Async worker (in-process `asyncio` task) handles chunking + BGE-m3 embedding + entity extraction after upload.

---

## Management API (Dashboard Server, Same Process)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/api_keys` | POST | Issue new API key (max 3 per tenant) |
| `/v1/api_keys/{id}` | DELETE | Revoke key |
| `/v1/usage` | GET | Own usage stats (from `tenant_usage_daily`) |
| `/v1/signup` | POST | Create tenant + first key |
| `/v1/login` | POST | Email/password for dashboard (optional) |
| `/v1/admin/tenants` | GET | Admin overview (all tenants' usage) |

---

## Observability (No Grafana, No OTel Export)

| Component | Implementation |
|-----------|----------------|
| **Per-Request Audit** | Every tool call inserts row into `audit_events` (asyncpg, non-blocking) |
| **Rollup Job** | `asyncio` cron every 5 min: aggregates `audit_events` → `tenant_usage_daily` |
| **Dashboard Widget** | FastAPI reads `tenant_usage_daily` → renders "Without Pager: $X → With Pager: $Y, Accuracy: Z%" |
| **Admin View** | Aggregates all tenants in `tenant_usage_daily` |

---

## Reference Agent (Example)

| Location | `examples/goldfish_agent/` |
|----------|---------------------------|
| **Config** | `MultiServerMCPClient({"pager": {"transport": "http", "url": "..."}}, tool_name_prefix=True)` |
| **Prompt** | Reformulated Q19: no in-context memory; `commit_to_long_term_memory` explicit; silent recall automatic; 50-tool-call budget |
| **Limits** | `recursion_limit=100`; `SummarizationMiddleware(trigger=("tokens", 8000), keep=("messages", 6))` + custom `ProtectLatestToolResultMiddleware` |

---

## Deployment

| Environment | Composition |
|-------------|-------------|
| **Hosted Free Tier (Oracle VM)** | `docker-compose.yml`: postgres (pgvector/pgvector:pg16), redis:7-alpine, **ollama skipped** (`OLLAMA_ENABLED=false`), pager-mcp (FastMCP :8000), pager-dashboard (FastAPI :8501), caddy (443 → 8000/8501), keepalive-daemon (5% CPU loop + 5GB resident + 2-min curl public URL) |
| **Self-Hosted Default** | Same compose, `OLLAMA_ENABLED=true` by default; user can override `DATABASE_URL`/`REDIS_URL` via `docker-compose.override.yml` |
| **Backups** | Nightly `pg_dump \| gzip` → Oracle Object Storage (7-day retention); restore script documented |

---

## Dependencies (from `pyproject.toml` — authoritative)

```
# Core
fastmcp>=3.4.4
langchain-mcp-adapters>=0.3.0
langgraph>=0.2.0
langchain-google-genai>=1.0.0

# Data
asyncpg>=0.29.0
pgvector>=0.2.0
redis>=5.0.0

# Embeddings & Compression
FlagEmbedding>=1.4.0
llmlingua>=0.2.0
ollama>=0.3.0  # optional, self-hosted only

# PII
presidio-analyzer>=2.2.0
presidio-analyzer[spacy]>=2.2.0

# Web
fastapi>=0.110.0
uvicorn>=0.29.0
jinja2>=3.1.0
python-multipart>=0.0.9

# Auth
python-jose>=3.3.0  # for API key hashing verification
passlib>=1.7.0

# Utilities
pydantic>=2.7.0
pydantic-settings>=2.3.0
python-dotenv>=1.0.0
pyyaml>=6.0.0
```

---

## Open Items (Non-Blocking)

1. **Sparsevec indexing performance** on pgvector 0.8.x — verify with seed data in Phase 4.
2. **Gemini 2.5 Flash pricing** — confirm $0.075 input / $0.30 output per 1M tokens at launch.
3. **`ProtectLatestToolResultMiddleware` hook order** — verify `abefore_model` runs before `SummarizationMiddleware` in LangGraph's interceptor chain (Phase 8).