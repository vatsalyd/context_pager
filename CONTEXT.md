# CONTEXT.md — Complete Technical Specification for Context Pager MCP Server

> **Purpose**: This is the single source of truth for any AI or human implementer. It contains the full technical architecture, every design decision with rationale, and the complete implementation roadmap. Read this top-to-bottom before touching code.

---

## Table of Contents

1. [Final Tech Stack Summary](#1-final-tech-stack-summary)
2. [All 32 Grilling Questions & Decisions](#2-all-32-grilling-questions--decisions)
3. [Data Model (SQL + RLS)](#3-data-model-sql--rls)
4. [MCP Tools (4 Tools + HTTP Ingestion)](#4-mcp-tools-4-tools--http-ingestion)
5. [Compression Pipeline](#5-compression-pipeline)
6. [Retrieval + RRF + GraphRAG](#6-retrieval--rrf--graphrag)
7. [Cache + Decay](#7-cache--decay)
8. [Agent + Summarization + Custom Middleware](#8-agent--summarization--custom-middleware)
9. [Goldfish Reference Agent](#9-goldfish-reference-agent)
10. [Benchmark Suite (3 Tasks + Ground Truth)](#10-benchmark-suite-3-tasks--ground-truth)
11. [Deployment (Oracle VM + Self-Host)](#11-deployment-oracle-vm--self-host)
12. [Phase Plan (22 Days)](#12-phase-plan-22-days)
13. [Open Items](#13-open-items)

---

## 1. Final Tech Stack Summary

| Layer | Choice | Rationale |
|-------|--------|-----------|
| **Protocol** | MCP via FastMCP 3.4.4 | `@mcp.tool` decorator auto-generates schemas; streamable HTTP native |
| **Transport** | Streamable HTTP (port 8000) + Caddy TLS reverse proxy | Public multi-tenant MCP needs real HTTP, not stdio |
| **Agent Bridge** | `langchain-mcp-adapters` 0.3.0 + `MultiServerMCPClient` | Official LangChain ↔ MCP bridge; `tool_name_prefix=True` for collision avoidance |
| **Agent Framework** | LangGraph + `create_agent` | Implicit ReAct tool-calling loop; context limit via middleware |
| **Reference LLM** | Google Gemini 2.5 Flash via `langchain-google-genai` | User-selected; cheap summarization model |
| **Custom Middleware** | `ProtectLatestToolResultMiddleware` (self-summarizes latest tool result) | Q7 Option B — bulletproof protection of latest fetched page |
| **Embeddings** | BGE-m3 via `FlagEmbedding` 1.4.0 (`BGEM3FlagModel`, `use_fp16=False` on ARM) | 1024-dim, dense+sparse+ColBERT in one encode; up to 8192 tokens |
| **Vector Store** | PostgreSQL 16 + pgvector 0.8.x (self-hosted) | Multi-tenant free-tier storage; HNSW on `vector_cosine_ops` + `sparsevec_cosine_ops` |
| **Sparse Vectors** | pgvector `sparsevec` type (BGE-m3 lexical weights) | Native SQL sparse cosine; `jsonb`+GIN as documented fallback |
| **Hybrid Retrieval** | Reciprocal Rank Fusion (k=60) over dense+sparse | Fused at **document-level** for `compress_document`, **entity-level** for `fetch_entity_graph` |
| **GraphRAG Backend** | Custom BFS over `entity_relations` + RRF on `entities` | Avoids `microsoft/graphrag` dep; reuses pgvector |
| **Compression (Free Tier)** | LLMLingua-2 via `PromptCompressor("microsoft/llmlingua-2-xlm-roberta-large-meetingbank", use_llmlingua2=True)` | CPU-feasible pure density filtering |
| **Compression (Self-Host)** | Two-stage: LLMLingua-2 density reduction → Ollama Llama 3 8B query-focused extraction over *reduced* text | Q4 Option C; only when `config.ollama_url` set |
| **PG Driver** | `asyncpg` + `pgvector.asyncpg.register_vector` (init on pool) | Highest-perf async driver |
| **Cache** | Redis 7-alpine (LRU via ZSET) | Content-addressed memoization + per-session decay tracking |
| **Rate Limiting** | Redis leaky-bucket per `(tenant_id, resource)` | Hard caps on free tier |
| **Auth** | Bearer API keys (`pgr_xxx`, SHA-256 stored) + Starlette middleware wrapping FastMCP | Sets `request.state.tenant_id` + `SET app.tenant_id` on PG connection |
| **Tenancy** | `tenant_id` column on every table + Postgres RLS `tenant_isolation` | Gold standard multi-tenant isolation |
| **Multi-Tenant Storage** | Self-hosted Postgres on Oracle VM + nightly `pg_dump` to Object Storage | Avoids Neon 500MB cap; no cold-start latency |
| **PII Middleware** | Presidio + spaCy `en_core_web_sm` **pre-compression** on raw text; defense-in-depth post-compression scan | Q1 redact-before-compress |
| **Extraction** | spaCy NER + regex at ingestion; optional Ollama relation-typing (free tier = `MENTIONED_WITH` catch-all) | Q26 hybrid heuristic + optional SLM |
| **Ingestion Surface** | HTTP REST on dashboard server (`POST /v1/documents`), **not** MCP tool | Q25 — agent never holds raw data |
| **TLS/Routing** | Caddy auto-HTTPS (Let's Encrypt) + DuckDNS free domain | Q28 zero-config TLS |
| **Observability** | **No OTel, no Grafana**. Per-tool audit row in `audit_events`; cron rolls up to `tenant_usage_daily`; FastAPI dashboard queries Postgres directly | Q10 + Q30 |
| **Dashboard Server** | Single FastAPI app: `/v1/*` (JSON API) + `/dashboard/*` (Jinja HTML) | Q31 combined service |
| **Reference Agent** | `examples/goldfish_agent/` with `langgraph.json` | Q32 demo + self-host test |
| **Deployment (Hosted)** | Oracle Cloud Always Free Ampere A1 (4 OCPU/24 GB), single docker-compose, keepalive daemon | Q12 + Q29 |
| **Backup** | Nightly `pg_dump | gzip` → Oracle Object Storage (7-day retention) | Q29 |

---

## 2. All 32 Grilling Questions & Decisions

### Q1 — Zero-Copy Contract & PII Ordering
**Decision**: Zero-Copy = "no raw, uncompressed document text crosses the tool boundary." Compressed text *does* flow back. **PII redaction runs pre-compression** on raw text inside server; second post-compression scan is defense-in-depth.
**Rationale**: Masking before compression saves tokens wasted on PII; satisfies "before it ever enters agent's context" strictly.

### Q2 — LRU Cache: Server Session vs Stateless Memoization
**Decision**: **Hybrid** — content-addressed memoization key `(tenant_id, doc_id, hash(focus_area), max_return_tokens)` for hot compressed-page cache (perf, shared across sessions) + per-session `active_pages` ZSET tracking `(doc_id, page_id, last_touched)` for decay (UX). Server remains stateless in MCP sense (no tool output depends on session).

### Q3 — `commit_to_long_term_memory` Recall
**Decision**: **Option C — Silent injection into `fetch_entity_graph`**. Writer tool is explicit; recall is automatic. When `fetch_entity_graph` runs, server queries `agent_memory` for rows with cosine > 0.78 to query embedding; prepends "Recalled insight: {key}: {insights}" block to response.
**Rationale**: Keeps API at 3 tools (per PRD); agent gets past insights automatically; decay applies naturally.

### Q4 — LLMLingua-2 Query-Conditional Mode
**Decision**: **Two-stage compression for self-hosted only** (when `config.ollama_url` set). Stage 1: LLMLingua-2 density reduction (~5x). Stage 2: Ollama Llama 3 8B over reduced text with `focus_area` as query. **Free hosted tier drops Stage 2 entirely** — `focus_area` is accepted but ignored (honest: "focus_area only honored when SLM available").
**Rationale**: LLMLingua-2 is task-agnostic; `focus_area` does nothing. Two-stage keeps it honest. Ollama on ARM CPU too slow for free tier.

### Q5 — Search Unit: Chunks vs Documents vs Entities
**Decision**: **RRF fusion rank unit = document for `compress_document`, entity for `fetch_entity_graph`**. Chunk-level scoring used internally to compute document/entity scores (max-chunk-score → doc-score). Agent receives ranked doc_ids or entity_ids, then pages via `compress_document` or `fetch_entity_graph`.
**Rationale**: Chunk-level return forces agent to page one chunk at a time — terrible UX. Document/entity is the natural page unit.

### Q6 — BGE-m3 Sparse Storage
**Decision**: **pgvector `sparsevec(N)` with HNSW `sparsevec_cosine_ops`** (N = XLM-R vocab ~250k). Native SQL sparse cosine. Fallback: `jsonb` + GIN index.
**Rationale**: pgvector 0.7+ added `sparsevec`; Neon/Oracle images are 0.8+. Exact lexical-match scoring approximated by cosine on BGE-m3 weights.

### Q7 — Summarization Middleware vs Latest Tool Result
**Decision**: **Option B — Custom `ProtectLatestToolResultMiddleware` that self-summarizes the latest ToolMessage via Gemini 2.5 Flash into a compact AIMessage *before* `SummarizationMiddleware` runs**. No reliance on prompt rules or undocumented flags.
**Rationale**: Production-grade guarantee; 100% certainty latest fetched page survives summarization.

### Q8 — Decay: Agent-Facing vs Server-Internal
**Decision**: **Option A — Decay is server-internal cache eviction only**. `active_pages` ZSET score decays exponentially (λ=0.01, half-life ~70 min); entries below threshold deleted from hot cache. Agent sees nothing — no `stale_pages` metadata. Agent's memory decay handled by `SummarizationMiddleware` (Q7).
**Rationale**: Stateless server principle (Q2); surfacing decay requires tracking agent's working set, which violates statelessness.

### Q9 — Cost-Savings Headline Numbers
**Decision**: **Build 3-task benchmark suite with ground truth** (Q13). Run greedy (full context) vs goldfish (paged) agents on each. Log real token costs + accuracy F1 to `audit_events`. Dashboard renders *actual* median numbers: "Without Pager: $X → With Pager: $Y, Accuracy: Z%". No invented figures.
**Rationale**: Honest demo; the benchmark *is* the proof.

### Q10 — Observability Backend
**Decision**: **No OTel, no Grafana, no collector**. Per-tool audit → `audit_events` table → 5-min cron rollup → `tenant_usage_daily` → FastAPI dashboard renders widget. Single source of truth, zero extra infra.
**Rationale**: Saves 1-2 GB RAM on Oracle VM; dashboard already needs DB for cost widget.

### Q11 — Recursion Limit & Tool Budget
**Decision**: `recursion_limit=100` on `agent.ainvoke(config={"recursion_limit": 100})` + prompt-injected "Budget: max 50 tool calls per task; on 50th, you MUST summarize and stop."
**Rationale**: 100 super-steps covers ~40-50 reasoning turns; prompt budget is agent discipline matching cost narrative.

### Q12 — Oracle Idle Reclamation
**Decision**: Continuous keepalive daemon: (1) 5% CPU busy-loop + 5 GB resident memory block permanently allocated; (2) every 2 min, `curl https://<public-url>/healthz` via public LB (registers as outbound network + LB traffic).
**Rationale**: Oracle reclaims if 7-day 95th-pctile CPU/memory/network < 20%. Continuous low load keeps metrics above threshold.

### Q13 — Benchmark Corpus
**Decision**: 3 hand-authored tasks (see Section 10):
- **Code audit**: 10k-line generated Python file, 15 planted anti-patterns
- **Financial reports**: 3 fictional annual report PDFs (30-50 pp each), 15 KPIs each
- **Meeting transcripts**: 3 transcripts with planted PII, decisions list

### Q14 — Server Name & Tool Prefix
**Decision**: Server alias `"pager"` (not `"context_pager"`), `tool_name_prefix=True` → tools become `pager_fetch_entity_graph`, `pager_compress_document`, `pager_commit_to_long_term_memory`. Future-proofs multi-server agent configs.

### Q15 — Short-Circuit for Small Docs
**Decision**: In `compression/pipeline.py`: `if input_tokens <= target_tokens: redact PII, return raw text, metadata.skipped_compression=True, compression_ratio=1.0`. Prevents LLMLingua confusion on tiny inputs.

### Q16 — Default Return Token Budget
**Decision**: `max_return_tokens: int = 2048` (default), agent-tunable via tool param. 2048 = 25% of 8k agent budget, leaves room for reasoning + next tool call.
**Rationale**: 4096 consumed half the agent's context; 2048 is the safe default.

### Q17 — Entity Sparse Embedding Source
**Decision**: `entities.sparse_weights` computed from `f"{name} ({type})"` only — **not** from properties JSON. Matches keyword-search behavior; avoids noise from property values.
**Rationale**: Sparse excels at exact name match; dense handles semantic paraphrase.

### Q18 — `fetch_entity_graph` Pagination + Filtering
**Decision**: **Combined Fix 1 + Fix 2** (per your answer):
- Required `relation: str` filter (e.g., `EXECUTIVE_LEADERSHIP`, `SUBSIDIARY_COMPANIES`)
- Pagination via opaque `page_token` (encodes offset + filter signature); `limit: int = 20` default
- Agent can "scroll" with `page_token` if entity has >20 matching relations

### Q19 — Goldfish Prompt Reformulation
**Decision**: Approved prompt language:
> "You have **no in-context long-term memory**; on summarization, prior conversation collapses to a short summary. To persist key insights beyond summarization, use `commit_to_long_term_memory(key, insights)` — those insights will be surfaced back to you automatically when you fetch a relevant entity graph. Do not assume any prior conversation is intact; always re-fetch pages you need."

### Q20 — Documentation Set
**Decision**: Three docs to produce:
- `PROJECT_BRIEF.md` — PRD: problem, value prop, users, access model
- `CONTEXT.md` — This document (technical spec + all grilling Q&A)
- `TECH_STACK.md` — Final stack table with rationale

---

### Q21 — Tenancy Model
**Decision**: **Hybrid Open-Core + Hosted** (Option C). Open-source self-host via `docker-compose up`; small free hosted tier on Oracle VM as live demo + freemium entry. Future paid tier = monetization without re-architect.

### Q22 — Data Isolation
**Decision**: **Shared schema + `tenant_id` column + Postgres RLS** (Option A1). Every table gets `tenant_id text NOT NULL`; RLS policy `tenant_isolation USING (tenant_id = current_setting('app.tenant_id'))`. Connection middleware runs `SET LOCAL app.tenant_id = '<hash>'` per request. Redis keys prefixed `pager:cache:{tenant_id}:...`.

### Q23 — Authentication
**Decision**: **Bearer API keys (`pgr_xxx`, 32 random bytes, SHA-256 stored with prefix index)** validated in **Starlette HTTP middleware wrapping FastMCP**. Middleware extracts bearer, validates, sets `request.state.tenant_id`, asyncpg connection runs `SET LOCAL app.tenant_id`. API keys issued via web signup (`/v1/signup`), CLI (`pager signup`), or admin script.

### Q24 — Rate Limiting
**Decision**: **Redis leaky-bucket (sliding window)** per `(tenant_id, resource)`. Free tier caps:
| Resource | Free Tier |
|----------|-----------|
| Tool calls / hour | 100 |
| Compression tokens / day | 500,000 |
| Stored documents | 100 |
| Concurrent sessions | 2 |
Returns MCP `429 TOO_MANY_REQUESTS` with friendly message.

### Q25 — Document Ingestion
**Decision**: **HTTP REST on dashboard server, NOT MCP tool**. `POST /v1/documents` (multipart), async chunk+embed+extract worker, `GET /v1/documents/{id}` for status. CLI: `pager docs upload my.pdf --doc-id mypdf --kind code`. Agent never uploads; Zero-Copy preserved.

### Q26 — Entity Extraction
**Decision**: **Hybrid (Option C)**:
- spaCy NER (`PERSON`, `ORG`, `GPE`, `DATE`, `MONEY`) + regex (`EMAIL`, `PHONE`, `SSN`, `URL`, `IBAN`)
- Optional Ollama per-chunk relation typing: "WORKS_FOR | INVESTED_IN | SUBSIDIARY_OF | LOCATED_IN | MENTIONED_WITH | OTHER"
- **Free tier skips LLM typing** → all relations = `MENTIONED_WITH` (fast, no LLM cost)
**Rationale**: GraphRAG quality without `microsoft/graphrag` dep; free tier stays CPU-only.

### Q27 — Compute Allocation (Free Tier)
**Decision**: **LLMLingua-2 only on free hosted tier** (drop Q4 two-stage). Pure CPU density filtering scales well. Self-hosters with Ollama get full two-stage. Free tier serves ~10-20 tenants on Oracle VM before CPU saturation.

### Q28 — TLS & Domain
**Decision**: **Caddy auto-HTTPS (Let's Encrypt) + DuckDNS free subdomain** (`pager.duckdns.org`). Documented for self-hosters too (they supply own domain or use DuckDNS).

### Q29 — Database Location
**Decision**: **Postgres in docker-compose on Oracle VM** (pgvector/pgvector:pg16). Nightly `pg_dump | gzip` → Oracle Object Storage (20 GB free, 7-day rotation). Documented restore script. Avoids Neon 500 MB cap + cold-start latency.

### Q30 — Observability for Multi-Tenant
**Decision**: **Per-tenant daily rollup table `tenant_usage_daily`** (date, tenant_id, tool_calls, tokens_compressed, storage_bytes, est_cost_usd). Cron job every 5 min aggregates `audit_events` → rollup. Dashboard reads rollup for cost-savings widget (per-tenant own view + admin view).

### Q31 — Management UI
**Decision**: **All in single FastAPI dashboard server**, two route groups:
- `/v1/*` JSON API: documents, api_keys, usage, signup, login, admin tenants
- `/dashboard/*` HTML (Jinja): cost widget, doc list, usage, per-tenant view, admin overview

### Q32 — Backward Compatibility
**Decision**: 
1. **Goldfish agent** kept as `examples/goldfish_agent/` with `langgraph.json` — reference "Hello World" for self-hosters.
2. **Self-host `docker-compose up` = zero-config** (bundles Postgres, Redis, MCP server, Dashboard, optional Ollama). `docker-compose.override.yml` documents pointing at external infra.

---

## 3. Data Model (SQL + RLS)

### Extensions & Types
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

### Core Tables (all have `tenant_id` + RLS)

```sql
-- Users / API Keys
CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           text UNIQUE NOT NULL,
    hashed_api_key  text NOT NULL,          -- SHA-256(pgr_xxx...)
    api_key_prefix  text NOT NULL,          -- first 8 chars for fast lookup
    plan            text NOT NULL DEFAULT 'free',
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX users_prefix_idx ON users (api_key_prefix);

-- Documents (source corpus)
CREATE TABLE documents (
    id              text PRIMARY KEY,       -- SHA-256 of content or UUID
    tenant_id       text NOT NULL,
    title           text NOT NULL,
    content         text NOT NULL,          -- raw, server-side only
    source_kind     text NOT NULL,          -- 'unstructured' | 'structured' | 'mixed'
    metadata        jsonb NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'processing',  -- 'processing' | 'ready' | 'failed'
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Document Chunks + Dense Embeddings
CREATE TABLE document_chunks (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    document_id     text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     int NOT NULL,
    text            text NOT NULL,
    embedding       vector(1024) NOT NULL,  -- BGE-m3 dense
    sparse_weights  sparsevec,              -- BGE-m3 sparse (pgvector sparsevec type)
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

-- Entities (GraphRAG nodes)
CREATE TABLE entities (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    document_id     text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    type            text NOT NULL,          -- 'function', 'transaction', 'person', 'org', ...
    name            text NOT NULL,
    properties      jsonb NOT NULL DEFAULT '{}',
    embedding       vector(1024),           -- dense: name+type+description
    sparse_weights  sparsevec,              -- sparse: name (+type) only (Q17)
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Entity Relations (GraphRAG edges)
CREATE TABLE entity_relations (
    from_id         bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id           bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation        text NOT NULL,          -- 'WORKS_FOR', 'SUBSIDIARY_OF', 'MENTIONED_WITH', ...
    properties      jsonb NOT NULL DEFAULT '{}',
    tenant_id       text NOT NULL,
    PRIMARY KEY (from_id, to_id, relation)
);

-- Agent Long-Term Memory (silent recall source)
CREATE TABLE agent_memory (
    key             text PRIMARY KEY,
    tenant_id       text NOT NULL,
    insights        text NOT NULL,
    embedding       vector(1024) NOT NULL,  -- for semantic recall
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_recalled   timestamptz
);

-- Audit Events (immutable log)
CREATE TABLE audit_events (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    event_type      text NOT NULL,          -- 'tool_call', 'pii_redacted', 'cache_hit', 'cache_miss', 'doc_upload'
    tool_name       text,
    session_id      text,
    doc_id          text,
    original_tokens int,
    compressed_tokens int,
    cost_saved_usd  numeric(10, 4),
    metadata        jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Daily Rollups (fast dashboard queries)
CREATE TABLE tenant_usage_daily (
    date            date NOT NULL,
    tenant_id       text NOT NULL,
    tool_calls      int NOT NULL DEFAULT 0,
    tokens_compressed bigint NOT NULL DEFAULT 0,
    storage_bytes   bigint NOT NULL DEFAULT 0,
    est_cost_usd    numeric(10, 4) NOT NULL DEFAULT 0,
    PRIMARY KEY (date, tenant_id)
);

-- Indexes
CREATE INDEX documents_tenant_idx ON documents (tenant_id);
CREATE INDEX document_chunks_tenant_idx ON document_chunks (tenant_id);
CREATE INDEX document_chunks_doc_idx ON document_chunks (document_id);
CREATE INDEX entities_tenant_idx ON entities (tenant_id);
CREATE INDEX entities_doc_idx ON entities (document_id);
CREATE INDEX entity_relations_tenant_idx ON entity_relations (tenant_id);
CREATE INDEX agent_memory_tenant_idx ON agent_memory (tenant_id);
CREATE INDEX audit_events_tenant_created_idx ON audit_events (tenant_id, created_at);
CREATE INDEX tenant_usage_daily_date_idx ON tenant_usage_daily (date);

-- HNSW Indexes (built after initial data load)
CREATE INDEX document_chunks_embedding_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX document_chunks_sparse_idx
    ON document_chunks USING hnsw (sparse_weights sparsevec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX entities_embedding_idx
    ON entities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX entities_sparse_idx
    ON entities USING hnsw (sparse_weights sparsevec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Row-Level Security
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_usage_daily ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON documents
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON document_chunks
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON entities
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON entity_relations
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON agent_memory
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON audit_events
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY tenant_isolation ON tenant_usage_daily
    USING (tenant_id = current_setting('app.tenant_id', true));
```

---

## 4. MCP Tools (4 Tools + HTTP Ingestion)

All tools return `CallToolResult` with `TextContent` containing **JSON envelope**:

```json
{
  "tool": "compress_document",
  "doc_id": "abc123",
  "pages": [{ "page": 1, "content": "...", "token_count": 320 }],
  "summary": "This page covers X, Y, Z.",
  "predecessor": null,
  "refs": [],
  "metadata": {
    "original_tokens": 24000,
    "compressed_tokens": 9600,
    "compression_ratio": "2.5x",
    "cost_saved_usd": 0.42,
    "pii_redacted": ["EMAIL:1", "PHONE:1"],
    "cache_hit": false,
    "skipped_compression": false,
    "elapsed_ms": 320
  }
}
```

### 4.1 `pager_fetch_entity_graph`
```python
@mcp.tool
async def fetch_entity_graph(
    query: str,
    relation: str,                    # REQUIRED per Q18
    page_token: str | None = None,    # opaque token for pagination
    limit: int = 20,
    ctx: Context = None
) -> str:
    """
    Retrieve structured entity graph for a query, filtered by relation type.
    
    Args:
        query: Natural language query (e.g., "Acme Corp executives")
        relation: Required relation filter (e.g., "EXECUTIVE_LEADERSHIP", "SUBSIDIARY_COMPANIES", "MENTIONED_WITH")
        page_token: Opaque token from previous call to fetch next page
        limit: Max entities to return (default 20, max 100)
    
    Returns:
        JSON envelope with entities[], relations[], summary, next_page_token, metadata
    """
```

**Implementation**:
1. Embed `query` via BGE-m3 (dense + sparse in one `encode()`)
2. **RRF retrieval** over `entities` table (dense cosine + sparse cosine, k=60) filtered by `relation` on `entity_relations`
3. **BFS expansion** over `entity_relations` (depth=1, filtered by `relation`) up to `limit` entities
4. **Silent recall**: query `agent_memory` for rows with cosine > 0.78 to query embedding; prepend "Recalled insight: {key}: {insights}"
5. Paginate: encode `(offset + limit, relation, query_hash)` as `page_token` for next call
6. PII redact output, log audit, return envelope

### 4.2 `pager_compress_document`
```python
@mcp.tool
async def compress_document(
    doc_id: str,
    focus_area: str | None = None,
    max_return_tokens: int = 2048,
    ctx: Context = None
) -> str:
    """
    Fetch a compressed 'page' of a document. Zero-copy: raw text never leaves server.
    
    Args:
        doc_id: Document ID from upload
        focus_area: Optional focus hint (e.g., "quarterly revenue"). Honored only when Ollama fallback enabled.
        max_return_tokens: Target compressed size (default 2048, max 8000)
    
    Returns:
        JSON envelope with pages[], summary, predecessor, metadata
    """
```

**Implementation**:
1. Check content-addressed cache: key = `(tenant_id, doc_id, hash(focus_area or ''), max_return_tokens)`. If hit → return cached envelope with `cache_hit=true`.
2. Load `documents.content` (raw, server-side only).
3. **Short-circuit** (Q15): if `input_tokens <= max_return_tokens` → PII redact raw text, return with `skipped_compression=true`.
4. **PII Redaction (pre-compression, Q1)**: run Presidio on raw text → redacted text.
5. **Compression**:
   - **Free tier / no Ollama**: LLMLingua-2 only. `PromptCompressor.compress_prompt(redacted_text, target_token=max_return_tokens, use_llmlingua2=True)`. `focus_area` accepted but ignored (logged).
   - **Self-hosted (Ollama enabled)**: Two-stage (Q4). Stage 1: LLMLingua-2 to ~5x reduction. Stage 2: Ollama Llama 3 8B with prompt "Extract sentences relevant to: {focus_area} from the following text, under {max_return_tokens} tokens."
6. Cache result with content-addressed key.
7. Update session `active_pages` ZSET: `ZADD pager:active:{session_id} <ts> {doc_id}:{page_id}`.
8. Log audit with token counts, cost saved, PII counts.
9. Return envelope.

### 4.3 `pager_commit_to_long_term_memory`
```python
@mcp.tool
async def commit_to_long_term_memory(
    key: str,
    insights: str,
    ctx: Context = None
) -> str:
    """
    Persist a key insight for automatic recall in future entity graph fetches.
    
    Args:
        key: Short memorable key (e.g., "acme_q3_revenue")
        insights: Dense factual summary the agent wants to remember
    
    Returns:
        JSON envelope with acknowledgement
    """
```

**Implementation**:
1. Embed `insights` via BGE-m3 (dense only).
2. Upsert into `agent_memory` (ON CONFLICT key DO UPDATE).
3. Return envelope with `metadata: {persisted: true}`.
4. Silent recall happens automatically in `fetch_entity_graph` (Q3).

### 4.4 HTTP Ingestion (NOT an MCP Tool)
```
POST   /v1/documents              # multipart: file + optional doc_id, source_kind
GET    /v1/documents/{id}         # status: {status, chunks, entities, error}
DELETE /v1/documents/{id}         # cascade delete
GET    /v1/documents              # list tenant's docs
```

**Async Worker** (spawned on upload):
1. Store raw in `documents` (status='processing').
2. Chunk text (target ~512 tokens, overlap 50).
3. BGE-m3 `encode()` each chunk → dense + sparse.
4. Batch insert `document_chunks`.
5. Run extraction (Q26): spaCy NER + regex → `entities`; optional Ollama relation typing → `entity_relations`.
6. Update `documents` status='ready', `chunks=N`, `entities=M`.

---

## 5. Compression Pipeline

```python
# compression/pipeline.py

async def compress_pipeline(
    raw_text: str,
    focus_area: str | None,
    max_return_tokens: int,
    tenant_id: str,
    doc_id: str,
    session_id: str | None,
    use_ollama: bool,
) -> CompressedResult:
    """
    Returns CompressedResult with:
        pages: list[Page]  # each has content, token_count
        summary: str
        metadata: CompressionMetadata
    """
    original_tokens = count_tokens(raw_text)
    
    # Q15: Short-circuit for small docs
    if original_tokens <= max_return_tokens:
        redacted = await pii_redact(raw_text)
        return CompressedResult(
            pages=[Page(page=1, content=redacted, token_count=original_tokens)],
            summary=redacted[:500],
            metadata=CompressionMetadata(
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio="1.0x",
                skipped_compression=True,
                pii_redacted=extract_pii_types(raw_text),
            )
        )
    
    # Q1: PII redaction PRE-compression
    redacted_text, pii_counts = await pii_redact_with_counts(raw_text)
    
    if use_ollama and focus_area:
        # Q4 Two-stage (self-hosted only)
        # Stage 1: LLMLingua-2 density reduction
        stage1 = llmlingua_compress(redacted_text, target_tokens=max_return_tokens * 3)
        # Stage 2: Ollama query-focused extraction
        compressed = await ollama_extract(stage1, focus_area, max_return_tokens)
    else:
        # Free tier / no focus: LLMLingua-2 only
        compressed = llmlingua_compress(redacted_text, target_tokens=max_return_tokens)
        if focus_area:
            logger.info("focus_area ignored (Ollama not enabled)")
    
    # Q1 defense-in-depth: post-compression PII scan
    final_text, pii_counts_2 = await pii_redact_with_counts(compressed)
    pii_counts = merge_counts(pii_counts, pii_counts_2)
    
    compressed_tokens = count_tokens(final_text)
    
    return CompressedResult(
        pages=[Page(page=1, content=final_text, token_count=compressed_tokens)],
        summary=generate_summary(final_text),  # first 500 chars or LLM summary
        metadata=CompressionMetadata(
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=f"{original_tokens / compressed_tokens:.1f}x",
            cost_saved_usd=calculate_savings(original_tokens, compressed_tokens),
            pii_redacted=format_pii_counts(pii_counts),
            skipped_compression=False,
        )
    )
```

---

## 6. Retrieval + RRF + GraphRAG

### 6.1 BGE-m3 Embedding (Single Call, Multi-Output)
```python
# knowledge/embedder.py
class BGEEmbedder:
    def __init__(self):
        self.model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)  # ARM CPU
    
    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, batch_size=12, max_length=8192)["dense_vecs"].tolist()
    
    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        out = self.model.encode(texts, return_dense=False, return_sparse=True, return_colbert_vecs=False)
        return out["lexical_weights"]  # list of {token_id: weight}
    
    async def embed_multi(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        out = self.model.encode(texts, return_dense=True, return_sparse=True, return_colbert_vecs=False)
        return out["dense_vecs"].tolist(), out["lexical_weights"]
```

### 6.2 RRF Fusion (Document-Level for compress_document)
```python
# knowledge/retriever.py
K_RRF = 60

async def retrieve_documents_rrf(query: str, k: int = 10) -> list[DocumentHit]:
    q_dense, q_sparse = await embedder.embed_multi([query])
    
    # Parallel pgvector searches (fetch 2k candidates each for robust fusion)
    dense_task = pool.fetch("""
        SELECT id, document_id, text, 1 - (embedding <=> $1) AS score
        FROM document_chunks
        WHERE tenant_id = current_setting('app.tenant_id')
        ORDER BY embedding <=> $1
        LIMIT $2
    """, q_dense[0], k * 20)
    
    sparse_task = pool.fetch("""
        SELECT id, document_id, text, 1 - (sparse_weights <=> $1) AS score
        FROM document_chunks
        WHERE tenant_id = current_setting('app.tenant_id')
        ORDER BY sparse_weights <=> $1
        LIMIT $2
    """, q_sparse[0], k * 20)
    
    dense_rows, sparse_rows = await asyncio.gather(dense_task, sparse_task)
    
    # RRF over chunk scores, then aggregate to document-level (max chunk score per doc)
    chunk_scores: dict[int, float] = defaultdict(float)
    chunk_meta: dict[int, dict] = {}
    
    for rank, r in enumerate(dense_rows, 1):
        chunk_scores[r["id"]] += 1.0 / (K_RRF + rank)
        chunk_meta[r["id"]] = dict(r)
    for rank, r in enumerate(sparse_rows, 1):
        chunk_scores[r["id"]] += 1.0 / (K_RRF + rank)
        chunk_meta[r["id"]].update(dict(r))
    
    # Aggregate to document level
    doc_scores: dict[str, float] = defaultdict(float)
    doc_meta: dict[str, dict] = {}
    for chunk_id, score in chunk_scores.items():
        meta = chunk_meta[chunk_id]
        doc_id = meta["document_id"]
        if score > doc_scores[doc_id]:
            doc_scores[doc_id] = score
            doc_meta[doc_id] = {"document_id": doc_id, "best_chunk_text": meta["text"]}
    
    # Sort and return top-k documents
    ranked = sorted(doc_scores.items(), key=lambda x: -x[1])[:k]
    return [DocumentHit(document_id=doc_id, fused_score=score, **doc_meta[doc_id]) for doc_id, score in ranked]
```

### 6.3 Entity-Level RRF for `fetch_entity_graph`
```python
async def retrieve_entities_rrf(query: str, relation_filter: str, k: int = 20) -> list[EntityHit]:
    q_dense, q_sparse = await embedder.embed_multi([query])
    
    # Entity dense search
    dense_sql = """
        SELECT e.id, e.name, e.type, e.properties, e.document_id,
               1 - (e.embedding <=> $1) AS score
        FROM entities e
        WHERE e.tenant_id = current_setting('app.tenant_id')
        ORDER BY e.embedding <=> $1
        LIMIT $2
    """
    # Entity sparse search (name-only sparse weights per Q17)
    sparse_sql = """
        SELECT e.id, e.name, e.type, e.properties, e.document_id,
               1 - (e.sparse_weights <=> $1) AS score
        FROM entities e
        WHERE e.tenant_id = current_setting('app.tenant_id')
        ORDER BY e.sparse_weights <=> $1
        LIMIT $2
    """
    
    # RRF fuse → then BFS over entity_relations filtered by relation_filter
    # Return top-k entities with their outgoing relations (depth=1)
```

### 6.4 GraphRAG: BFS over `entity_relations`
```python
async def expand_entity_graph(
    seed_entity_ids: list[int],
    relation_filter: str,
    limit: int,
    page_token: str | None
) -> tuple[list[Entity], list[Relation], str | None]:
    """
    Breadth-first expansion from seed entities over entity_relations
    filtered by relation type. Paginated via opaque token.
    """
    # Decode page_token -> (offset, filter_sig)
    # BFS queue with visited set
    # Stop when visited reaches limit
    # Encode next_page_token if more exist
```

---

## 7. Cache + Decay

### 7.1 Content-Addressed Memoization (Hot Cache)
**Key**: `pager:cache:{tenant_id}:{doc_id}:{focus_hash}:{max_return_tokens}`
**Value**: JSON serialized `CompressedResult` + `metadata.cached_at`
**TTL**: 7 days (Redis `EXPIRE`)

```python
async def get_cached_compression(tenant_id, doc_id, focus_area, max_tokens) -> CompressedResult | None:
    focus_hash = hashlib.sha256((focus_area or "").encode()).hexdigest()[:16]
    key = f"pager:cache:{tenant_id}:{doc_id}:{focus_hash}:{max_tokens}"
    data = await redis.get(key)
    if data:
        result = CompressedResult.parse_raw(data)
        result.metadata.cache_hit = True
        return result
    return None

async def set_cached_compression(tenant_id, doc_id, focus_area, max_tokens, result):
    focus_hash = hashlib.sha256((focus_area or "").encode()).hexdigest()[:16]
    key = f"pager:cache:{tenant_id}:{doc_id}:{focus_hash}:{max_tokens}"
    await redis.set(key, result.json(), ex=60*60*24*7)
```

### 7.2 Per-Session Active Pages (Decay Tracking)
**Key**: `pager:active:{session_id}` — Redis **Sorted Set**
**Member**: `{doc_id}:{page_id}` (page_id = 1 for single-page, or chunk index)
**Score**: `last_accessed_timestamp` (epoch ms)

```python
async def touch_active_page(session_id: str, doc_id: str, page_id: int):
    member = f"{doc_id}:{page_id}"
    await redis.zadd(f"pager:active:{session_id}", {member: time.time() * 1000})

# Decay job (runs every 10 min)
async def decay_active_pages():
    for session_key in await redis.keys("pager:active:*"):
        # Exponential decay: score = score * exp(-lambda * age_minutes)
        # Implemented via Lua script for atomicity
        await redis.eval(DECAY_LUA_SCRIPT, 1, session_key, LAMBDA=0.01)
        # Remove entries below threshold
        await redis.zremrangebyscore(session_key, "-inf", MIN_RELEVANCE_SCORE)
```

**Decay is server-internal only** (Q8). Agent never sees `stale_pages`. Agent memory decay = `SummarizationMiddleware`.

---

## 8. Agent + Summarization + Custom Middleware

### 8.1 LangGraph Agent Config
```python
# graphs/goldfish_agent.py
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "pager": {
        "transport": "http",
        "url": os.getenv("PAGER_MCP_URL", "http://localhost:8000/mcp"),
    }
}, tool_name_prefix=True)

tools = await client.get_tools()

agent = create_agent(
    model="google_genai:gemini-2.5-flash",
    tools=tools,
    system_prompt=GOLDFISH_PROMPT,  # Q19 reformulated
    middleware=[
        ProtectLatestToolResultMiddleware(),   # Custom — runs FIRST
        SummarizationMiddleware(
            model="google_genai:gemini-2.5-flash",
            trigger=("tokens", 8000),
            keep=("messages", 6),              # Q7: tight keep budget
        ),
    ],
)

# Invoke with recursion limit + tool budget
config = {"recursion_limit": 100}
result = await agent.ainvoke({"messages": [HumanMessage(content=task)]}, config=config)
```

### 8.2 `ProtectLatestToolResultMiddleware` (Q7 Option B)
```python
# graphs/middleware.py
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage, AIMessage

class ProtectLatestToolResultMiddleware(AgentMiddleware):
    """
    Runs BEFORE SummarizationMiddleware. Finds the latest ToolMessage,
    self-summarizes it via a cheap model call into a compact AIMessage,
    and replaces the ToolMessage with the summary. SummarizationMiddleware
    then sees the summary (which it keeps under `keep=("messages", 6)`).
    """
    
    def __init__(self):
        self.summarizer = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)
    
    async def abefore_model(self, state, config) -> dict:
        messages = state["messages"]
        
        # Find last ToolMessage (from our MCP tools)
        last_tool_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], ToolMessage) and messages[i].name.startswith("pager_"):
                last_tool_idx = i
                break
        
        if last_tool_idx is None:
            return {}  # No tool result to protect
        
        tool_msg = messages[last_tool_idx]
        
        # Summarize the tool result
        summary_prompt = f"""Condense this tool result to its essential facts in <= 500 tokens.
        Preserve specific numbers, names, decisions. Drop filler.
        
        Tool: {tool_msg.name}
        Result: {tool_msg.content}"""
        
        summary = await self.summarizer.ainvoke([HumanMessage(content=summary_prompt)])
        
        # Replace ToolMessage with AIMessage(summary) — this is what SummarizationMiddleware will keep
        new_messages = messages[:last_tool_idx] + [AIMessage(content=summary.content)] + messages[last_tool_idx+1:]
        
        return {"messages": new_messages}
```

### 8.3 Goldfish Prompt (Q19 Final)
```python
GOLDFISH_PROMPT = """You are a Goldfish Agent. You have NO in-context long-term memory; 
on summarization, prior conversation collapses to a short summary.

To persist key insights beyond summarization, use commit_to_long_term_memory(key, insights) — 
those insights will be surfaced back to you automatically when you fetch a relevant entity graph.

Do not assume any prior conversation is intact; always re-fetch pages you need.

Workflow:
1. Use fetch_entity_graph(query, relation) to find relevant entities (filter by relation type!)
2. Use compress_document(doc_id, focus_area, max_return_tokens) to read compressed pages
3. After reading a page, IMMEDIATELY restate the salient facts in YOUR OWN WORDS before fetching the next page. 
   Facts you do not restate get lost on the next summarization.
4. Commit critical insights via commit_to_long_term_memory.

Budget: You may make at most 50 tool calls per task. On the 50th, you MUST summarize and stop."""
```

---

## 9. Goldfish Reference Agent

| Location | `examples/goldfish_agent/` |
|----------|---------------------------|
| **Config** | `MultiServerMCPClient({"pager": {"transport": "http", "url": "..."}}, tool_name_prefix=True)` |
| **Prompt** | Reformulated Q19 (above) |
| **Limits** | `recursion_limit=100`; `SummarizationMiddleware(trigger=("tokens", 8000), keep=("messages", 6))` + `ProtectLatestToolResultMiddleware` |
| **Deployment** | `langgraph.json` for LangGraph Platform; `docker-compose.yml` for self-contained demo |

---

## 10. Benchmark Suite (3 Tasks + Ground Truth)

### 10.1 Code Audit Task
- **Fixture**: `tests/fixtures/generate_codebase.py` → `tests/fixtures/code_audit.py` (exactly 10,000 lines)
- **Planted Anti-Patterns** (15 total):
  1. N+1 DB query in loop (lines 1240-1260)
  2. Mutable default argument `def foo(items=[])` (line 342)
  3. Global state mutation in `Config` class (line 567)
  4. Bare `except:` (lines 890, 4120, 7830)
  5. `time.sleep()` in async function (line 2100)
  6. SQL injection via f-string (line 4500)
  7. Unbounded list growth in cache (line 6200)
  8. Thread-unsafe singleton (line 7100)
  9. Duplicate code blocks ×3 (lines 1000, 5500, 9200)
  10. Missing input validation on API endpoint (line 8800)
  11. Hardcoded secrets (line 9500)
  12. Blocking I/O in event loop (line 9800)
  13. Exception swallowing in retry loop (line 3300)
  14. Race condition in file write (line 6700)
  15. Recursive function without base case guard (line 1200)
- **Ground Truth**: `tests/fixtures/code_audit_ground_truth.json` — list of `{pattern, line_number, severity}`

### 10.2 Financial Reports Task
- **Fixtures**: 3 PDFs in `tests/fixtures/financial/` (hand-authored LaTeX → PDF)
  - `acme_corp_2023.pdf` (42 pp)
  - `globex_2023.pdf` (38 pp)
  - `initech_2023.pdf` (47 pp)
- **KPIs per report** (15 each):
  - Revenue, Net Income, EPS (Basic/Diluted), Gross Margin, Operating Margin
  - Debt/Equity, Current Ratio, Free Cash Flow, CapEx, R&D Spend
  - Segments: Revenue by geography, Revenue by product line
  - Guidance: Next quarter revenue estimate, FY outlook
- **Ground Truth**: `tests/fixtures/financial_ground_truth.json` — `{company, kpi, value, page_ref}`

### 10.3 Meeting Transcripts Task
- **Fixtures**: 3 `.txt` files in `tests/fixtures/transcripts/` (~15-20k tokens each)
- **Planted PII**: Names, emails, phones, SSNs, addresses
- **Decisions List**: 8-10 explicit decisions per transcript
- **Ground Truth**: `tests/fixtures/transcripts_ground_truth.json` — `{pii_count_by_type, decisions}`

### 10.4 Benchmark Runner
```python
# tests/benchmark/run_benchmark.py
async def run_benchmark():
    tasks = load_all_tasks()  # returns list of (name, task_prompt, ground_truth)
    
    for name, prompt, truth in tasks:
        # Greedy baseline: full doc in context (simulated)
        greedy_result = await run_greedy_agent(prompt, docs_for_task)
        greedy_cost = calculate_cost(greedy_result)
        greedy_f1 = score_against_truth(greedy_result, truth)
        
        # Goldfish agent
        goldfish_result = await goldfish_agent.ainvoke({"messages": [HumanMessage(content=prompt)]})
        goldfish_cost = sum(m.metadata.get("cost_saved_usd", 0) for m in goldfish_result.messages if hasattr(m, "metadata"))
        goldfish_f1 = score_against_truth(goldfish_result, truth)
        
        # Log to audit_events (dashboard will render)
        await log_benchmark(name, greedy_cost, goldfish_cost, greedy_f1, goldfish_f1)
```

**Dashboard Headline** (computed from actual runs):
> "Without Context Pager: **$X.XX** per run → With Context Pager: **$Y.YY** per run. **Accuracy retained: Z%**"

---

## 11. Deployment (Oracle VM + Self-Host)

### 11.1 `docker-compose.yml` (Single File, Zero-Config Start)
```yaml
version: "3.9"

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: pager
      POSTGRES_USER: pager
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./migrations:/docker-entrypoint-initdb.d:ro
    secrets:
      - pg_password
    healthcheck: ["CMD-SHELL", "pg_isready -U pager -d pager"]

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    healthcheck: ["CMD", "redis-cli", "ping"]

  ollama:
    image: ollama/ollama:latest
    profiles: ["self-host"]  # excluded by default on free tier
    volumes:
      - ollama_data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

  pager-mcp:
    build:
      context: .
      dockerfile: Dockerfile.mcp
    environment:
      - DATABASE_URL=postgresql://pager:${PG_PASSWORD}@postgres:5432/pager
      - REDIS_URL=redis://redis:6379/0
      - OLLAMA_URL=http://ollama:11434
      - OLLAMA_ENABLED=${OLLAMA_ENABLED:-false}
      - LOG_LEVEL=INFO
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 4G

  pager-dashboard:
    build:
      context: .
      dockerfile: Dockerfile.dashboard
    environment:
      - DATABASE_URL=postgresql://pager:${PG_PASSWORD}@postgres:5432/pager
      - REDIS_URL=redis://redis:6379/0
      - SECRET_KEY_FILE=/run/secrets/secret_key
    ports:
      - "8501:8501"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    secrets:
      - secret_key

  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - pager-mcp
      - pager-dashboard

  keepalive:
    build:
      context: .
      dockerfile: Dockerfile.keepalive
    environment:
      - PUBLIC_URL=https://pager.duckdns.org  # or custom domain
    deploy:
      resources:
        limits:
          cpus: "0.2"   # ~5% of 4 OCPU
          memory: 5G    # 5 GB resident block

volumes:
  pg_data:
  redis_data:
  ollama_data:
  caddy_data:
  caddy_config:

secrets:
  pg_password:
    file: ./secrets/pg_password.txt
  secret_key:
    file: ./secrets/secret_key.txt
```

### 11.2 `Caddyfile`
```caddyfile
{
    email admin@pager.duckdns.org
}

pager.duckdns.org {
    reverse_proxy /mcp/* pager-mcp:8000
    reverse_proxy /dashboard/* pager-dashboard:8501
    reverse_proxy /v1/* pager-dashboard:8501
    reverse_proxy /healthz pager-dashboard:8501
}

# Self-hosters: replace pager.duckdns.org with their domain
```

### 11.3 Keepalive Daemon (Dockerfile.keepalive)
```python
# keepalive/main.py
import asyncio, os, httpx

PUBLIC_URL = os.getenv("PUBLIC_URL")
CPU_TARGET = 0.05  # 5% of 4 OCPU
MEM_TARGET_GB = 5

# Allocate 5 GB resident
_resident = bytearray(MEM_TARGET_GB * 1024**3)

async def cpu_burn():
    while True:
        # Busy loop calibrated to ~5% CPU
        await asyncio.sleep(0.95)
        _ = sum(i*i for i in range(10000))

async def ping_loop():
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                await client.get(f"{PUBLIC_URL}/healthz")
            except Exception:
                pass
            await asyncio.sleep(120)

async def main():
    await asyncio.gather(cpu_burn(), ping_loop())

if __name__ == "__main__":
    asyncio.run(main())
```

### 11.4 Self-Host Override (`.env` + `docker-compose.override.yml`)
```yaml
# docker-compose.override.yml (user creates)
version: "3.9"
services:
  postgres:
    image: postgres:16  # or their managed PG
    environment:
      POSTGRES_DB: pager
    # user supplies their own DATABASE_URL via .env

  ollama:
    profiles: ["self-host"]  # enable with --profile self-host
```

### 11.5 Nightly Backup (systemd timer on Oracle VM or cron in keepalive container)
```bash
#!/bin/bash
# backup.sh
DATE=$(date +%F)
pg_dump -h localhost -U pager -d pager | gzip > /backups/pager_${DATE}.sql.gz
oci os object put -bn pager-backups --file /backups/pager_${DATE}.sql.gz --name pager_${DATE}.sql.gz
# Retain 7 days
find /backups -name "pager_*.sql.gz" -mtime +7 -delete
oci os object list -bn pager-backups --query "data[?timeCreated < '$(date -d '7 days ago' -Iseconds)'].name" | xargs -I {} oci os object delete -bn pager-backups --name {}
```

---

## 12. Phase Plan (22 Days)

| Phase | Days | Deliverable |
|-------|------|-------------|
| **0. Bootstrap** | 1 | `git init`, `pyproject.toml`, `docker-compose.yml`, `.env.example`, directory scaffolding, first commit |
| **1. MCP Skeleton** | 2 | FastMCP server with 3 stub tools, streamable HTTP on :8000, `deps.py` lazy singletons, first `MultiServerMCPClient` test |
| **2. Postgres + pgvector + RLS** | 2 | Alembic migration (full schema §3), RLS policies, `asyncpg` pool with `SET app.tenant_id`, seed script with 3 sample docs |
| **3. Auth + Rate Limiting** | 2 | Starlette middleware (bearer validation, `request.state.tenant_id`), Redis leaky-bucket, `users` table + `/v1/signup` |
| **4. BGE-m3 + Hybrid RRF Retrieval** | 3 | `BGEM3FlagModel` embedder, dense+sparse `encode()`, RRF fusion (doc-level + entity-level), HNSW indexes |
| **5. GraphRAG + `fetch_entity_graph`** | 2 | Entity extraction (spaCy + regex), `entity_relations` population, BFS expansion with `relation` filter + pagination |
| **6. Compression Pipeline** | 2 | LLMLingua-2 adapter, PII pre-compression (Presidio), short-circuit guard (Q15), two-stage Ollama path (feature flag) |
| **7. `compress_document` + Cache** | 2 | Content-addressed Redis cache, `active_pages` ZSET + decay Lua script, audit logging |
| **8. `commit_to_long_term_memory` + Silent Recall** | 1 | `agent_memory` upsert, cosine > 0.78 recall injection into `fetch_entity_graph` |
| **9. Ingestion HTTP API** | 2 | `POST /v1/documents` (multipart), async worker (chunk → embed → extract), status endpoint, CLI `pager docs upload` |
| **10. Dashboard + Observability** | 2 | FastAPI app (Jinja HTML + `/v1/*` JSON), `tenant_usage_daily` rollup cron, cost-savings widget, admin view |
| **11. Custom Middleware** | 1 | `ProtectLatestToolResultMiddleware` (self-summarize latest tool result via Gemini 2.5 Flash) |
| **12. Goldfish Agent + Benchmarks** | 2 | `examples/goldfish_agent/`, 3-task benchmark fixtures + runner, `langgraph.json` |
| **13. Oracle Deploy + Hardening** | 2 | VM provision, Docker images → GHCR, compose up, keepalive daemon, backup cron, TLS via Caddy, load test |
| **14. Documentation Polish** | 1 | `PROJECT_BRIEF.md`, finalize `CONTEXT.md`, `TECH_STACK.md`, README, demo script |

---

## 13. Open Items (Non-Blocking)

1. **Sparsevec indexing performance** on pgvector 0.8.x — verify with seed data in Phase 4.
2. **Gemini 2.5 Flash pricing** — confirm $0.075 input / $0.30 output per 1M tokens at launch; baked into `CostCalculator`.
3. **`ProtectLatestToolResultMiddleware` hook order** — verify `abefore_model` runs before `SummarizationMiddleware` in LangGraph's interceptor chain (Phase 11).
4. **DuckDNS rate limits** — ensure Caddy's Let's Encrypt requests don't hit DuckDNS API limits (use wildcard or manual DNS challenge if needed).
5. **Oracle ARM `FlagEmbedding` wheels** — confirm `torch` + `FlagEmbedding` have aarch64 manylinux wheels; if not, build from source in Dockerfile.