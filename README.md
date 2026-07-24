# Context Pager MCP Server

An Algorithmic Context Pager for LLM agents. Forces agents to operate with a tiny context window (~8k tokens) and request "pages" of context via MCP tools — exactly like CPU paging swaps data between RAM and disk.

## Architecture

```
┌─────────────────┐     MCP (Streamable HTTP)      ┌──────────────────────┐
│  Goldfish Agent │ ◄─────────────────────────────► │  Context Pager MCP   │
│  (LangGraph)    │  fetch_entity_graph            │  Server (FastMCP)    │
│  8k context     │  compress_document             │  ┌────────────────┐  │
│  Summarization  │  commit_to_long_term_memory    │  │ BGE-m3 Embed   │  │
│  Middleware     │                                │  │ LLMLingua-2    │  │
└─────────────────┘                                │  │ PII Redaction  │  │
                                                   │  │ LRU + Decay    │  │
                                                   │  │ Postgres+pgvec │  │
                                                   │  │ Redis Cache    │  │
                                                   └──────────────────────┘
```

## Quick Start (Self-Hosted)

```bash
git clone https://github.com/vatsalyd/context_pager
cd context_pager
cp .env.example .env
# Edit .env with your values
docker-compose up -d
```

The MCP server will be available at `http://localhost:8000/mcp` (configure your agent to connect here).

Dashboard: `http://localhost:8501/dashboard/`

## API Endpoints

### MCP Tools

| Tool | Description |
|------|-------------|
| `fetch_entity_graph(query, relation, page_token, limit)` | Structured entity/relation retrieval with pagination |
| `compress_document(doc_id, focus_area, max_return_tokens)` | Zero-copy compression; raw text never leaves server |
| `commit_to_long_term_memory(key, insights)` | Persist insights for automatic recall in future queries |

### HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/documents` | POST | Upload document for ingestion (multipart) |
| `/v1/documents/{id}` | GET | Get document status and metadata |
| `/v1/usage` | GET | Get usage statistics for current tenant |
| `/v1/signup` | POST | Create user and get API key |
| `/dashboard/` | GET | Web dashboard UI |
| `/healthz` | GET | Health check |

## Agent Integration (LangGraph)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware

client = MultiServerMCPClient({
    "pager": {"transport": "http", "url": "http://localhost:8000/mcp"}
}, tool_name_prefix=True)

tools = await client.get_tools()

agent = create_agent(
    model="google_genai:gemini-2.5-flash",
    tools=tools,
    system_prompt="You have NO long-term memory. Use fetch_entity_graph and compress_document to retrieve state.",
    middleware=[
        SummarizationMiddleware(
            model="google_genai:gemini-2.5-flash",
            trigger=("tokens", 8000),
            keep=("messages", 6),
        ),
    ],
)
```

See `examples/goldfish_agent/` for a complete working example.

## Deployment

### Docker (Recommended)

```bash
docker-compose up -d
```

This starts all services:
- **postgres**: PostgreSQL 16 with pgvector
- **redis**: Redis 7 for caching
- **pager-mcp**: MCP server on port 8000
- **pager-dashboard**: Dashboard on port 8501

### Oracle Cloud Always Free

1. Create Ampere A1 VM (4 OCPU / 24 GB RAM)
2. Install Docker + Docker Compose
3. `docker-compose up -d`
4. Configure DuckDNS + Caddy for TLS
5. Add keepalive daemon to defeat idle reclamation

## Key Features

- **Zero-Copy**: Raw documents never enter agent context
- **PII Redaction**: Presidio + spaCy pre-compression
- **Hybrid Retrieval**: BGE-m3 dense + sparse + RRF fusion
- **GraphRAG**: Custom BFS over entity relations (no external deps)
- **LRU Cache**: Redis-backed with exponential decay
- **Multi-Tenant**: RLS isolation, API key auth, rate limiting
- **Cost Telemetry**: Per-request token savings → dashboard widget

## Project Structure

```
context_pager/
├── src/context_pager/           # MCP server + tools + knowledge layer
│   ├── agent/                   # Agent middleware
│   ├── cache/                   # Redis caching + decay
│   ├── compression/             # LLMLingua-2 pipeline
│   ├── governance/              # PII redaction middleware
│   ├── ingestion/               # Document chunking + entity extraction
│   ├── knowledge/               # BGE-m3 embedder + RRF retriever
│   ├── rate_limit/              # Token bucket rate limiting
│   ├── serve/                   # Dashboard + HTTP API
│   └── telemetry/               # Audit logging + rollup
├── examples/goldfish_agent/     # Reference agent implementation
├── tests/benchmark/             # Benchmark suite
├── migrations/                  # Database migrations
├── docker-compose.yml           # Full stack
└── Caddyfile                    # Auto-TLS reverse proxy
```

## Running Benchmarks

```bash
python -m tests.benchmark.run_benchmark --api-key YOUR_API_KEY --task all
```

## License

Apache 2.0