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

## Tools Exposed

| Tool | Description |
|------|-------------|
| `fetch_entity_graph(query, relation, page_token, limit)` | Structured entity/relation retrieval with pagination |
| `compress_document(doc_id, focus_area, max_return_tokens)` | Zero-copy compression; raw text never leaves server |
| `commit_to_long_term_memory(key, insights)` | Persist insights for automatic recall in future queries |

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

## Deployment

### Oracle Cloud Always Free (Recommended)

1. Create Ampere A1 VM (4 OCPU / 24 GB RAM)
2. Install Docker + Docker Compose
3. `docker-compose up -d`
4. Configure DuckDNS + Caddy for TLS
5. Add keepalive daemon to defeat idle reclamation

### Free Tier Components

| Component | Service | Free Tier |
|-----------|---------|-----------|
| Postgres + pgvector | Self-hosted on Oracle VM | Unlimited (disk-bound) |
| Redis | Self-hosted on Oracle VM | Unlimited (RAM-bound) |
| Compute (MCP + Dashboard) | Oracle VM | 24 GB RAM, 4 OCPU |
| LLM (optional) | Ollama on Oracle VM | CPU-only, slow |
| TLS / Domain | Caddy + DuckDNS | Free |

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
├── src/context_pager/     # MCP server + tools + knowledge layer
├── dashboard/             # FastAPI + Jinja dashboard
├── migrations/            # Alembic migrations
├── docker-compose.yml     # Full stack
└── Caddyfile              # Auto-TLS reverse proxy
```

## License

Apache 2.0