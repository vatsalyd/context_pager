# Project Brief: Context Pager MCP Server

## The Problem

AI agents deployed for long-running, complex tasks (auditing 10,000 lines of code, analyzing 50 financial reports) currently stuff all data into massive 1-million-token context windows. This causes two enterprise problems:

1. **Context Rot ("Lost in the Middle")**: Agent reasoning degrades; constraints and facts buried in the middle of the prompt are forgotten.
2. **Exponential Inference Costs**: Paying for 500k input tokens on every reasoning step of a loop is financially unscalable.

## The Solution: Algorithmic Context Pager

An MCP server that forces agents to operate with a tiny, ultra-fast context window (~8k tokens). The agent must use MCP tools to securely request "pages" of context, which the server dynamically filters, compresses, and delivers just-in-time — exactly like CPU paging swaps data between RAM and disk.

### Core Architecture

**The Goldfish Agent (Client)**: A LangGraph agent with a strict ~8k token context limit, explicitly prompted: "You have no long-term memory. Use your MCP tools to fetch the state of the world."

**The MCP Server Interface**: Three tools exposed to the agent:
- `fetch_entity_graph(query, depth, relation, page_token)` — structured entity/relationship retrieval with pagination
- `compress_document(doc_id, focus_area, max_return_tokens)` — zero-copy compression with query-conditional focus
- `commit_to_long_term_memory(key, insights)` — persist insights beyond summarization cycles

**The Algorithmic Context Engine (Backend)**:
- Retrieves documents from secure Postgres (agent never holds raw data — **Zero-Copy**)
- Passes through LLMLingua-2 (task-agnostic density compression) + optional Ollama Llama 3 8B (query-aware summarization for large docs)
- Returns only information-dense tokens to the agent
- Implements LRU caching with exponential relevance decay in Redis

**Governance & Redaction**: PII middleware (Presidio + spaCy) masks sensitive data before it ever enters the agent's context window.

**Semantic Routing**: BGE-m3 embeddings route queries to structured Postgres or unstructured vector store transparently via Reciprocal Rank Fusion (RRF).

**Observability**: Per-request telemetry calculates exact tokens and money saved; FastAPI dashboard renders "Without Pager: $45.20 → With Pager: $1.15, 99% accuracy retained."

## Target Users

- **Agent Infrastructure Engineers**: Building long-running autonomous agents that need context management
- **AI Systems Architects**: Designing cost-effective, accurate LLM pipelines for enterprise workloads
- **Self-Hosting Enterprises**: Teams needing full data sovereignty — deploy via `docker-compose up` on own infra

## Access Model

**Hybrid Open-Core + Hosted**:
- **Hosted Free Tier**: `https://pager.example.com/mcp` — capped at 500k compressed tokens/day, 100 tool calls/hour, 50k vectors. Acts as live demo.
- **Self-Hosted (OSS)**: Full source on GitHub, `docker-compose.yml` brings up Postgres+pgvector, Redis, Ollama, MCP server, dashboard in minutes. No limits, user owns data and compute.

## Value Proposition

| Metric | Without Context Pager | With Context Pager |
|--------|----------------------|-------------------|
| Context Window | 500k–1M tokens (stuffed) | 8k tokens (paged) |
| Cost per 10k-line audit | ~$45 | ~$1.15 |
| Accuracy (lost-in-middle) | Degraded | 99% retained |
| PII Leakage Risk | High (raw docs in context) | Zero (server-side redaction) |
| Data Sovereignty | Vendor-dependent | Full self-host option |

---

*This document is the strategic anchor. Technical implementation details are in CONTEXT.md and TECH_STACK.md.*