# Context Pager

**Cut your AI agent's token costs by 4-10x.** Context Pager lets your AI read
compressed pages of documents instead of full files — like an OS pages memory
to RAM, your agent pages through documents on demand.

```
You: "What were the Q3 revenue targets?"
Agent: searches → finds "acme_corp_2024.txt" → reads compressed page 2 →
       gets the answer in 500 tokens instead of 8,000.
```

## What it does

| Tool | What your agent can do |
|---|---|
| `search_documents` | "Find me documents about X" — searches all indexed files and returns the most relevant pages |
| `compress_document` | "Read page 2 of that file" — returns a compressed, PII-masked page |
| `commit_to_long_term_memory` | "Remember this" — stores facts that resurface on later searches |

Your agent connects via [MCP](https://modelcontextprotocol.io/) — the standard
protocol for AI tool use. Works with Claude Desktop, Claude Code, Cursor, and any
MCP-compatible client.

## Three ways to use it

| | Local only | With relay | Self-hosted relay |
|---|---|---|---|
| **Time** | 2 min | 5 min | 15 min |
| **Internet** | Not needed | Required (first time) | Required |
| **Best for** | Trying it out, local dev | Multiple devices, cloud AI | Teams, full control |
| **Limitation** | Same machine only | None | You manage the server |

**[Get Started →](GETTING_STARTED.md)** — full setup guide for all three ways,
every MCP client, troubleshooting, and FAQ.

## Install

```bash
pip install "context-pager[bridge]"
```

First run downloads ~5GB of models (bge-m3 + llmlingua-2); subsequent starts
are instant. **Requirements:** Python 3.11+, ~4GB RAM.

## Architecture

```
┌─────────────┐  MCP over HTTPS       ┌──────────────┐
│    Agent    │ ──────────────────►   │   RELAY      │
│ (Claude,    │  context-pager.       │ AWS t3.micro │
│  Cursor)    │  duckdns.org/mcp      │ $0/mo        │
└─────────────┘                       └──────┬───────┘
                                             │ WSS
┌─────────────┐  localhost:8000/mcp   ┌──────┴───────┐
│   Bridge    │ ◄─────────────────── │   Bridge     │
│ (your       │                      │ (your laptop)│
│  laptop)    │                      └──────────────┘
└─────────────┘
```

- **Bridge** (your laptop): stores your documents, runs AI models for
  compression and search, handles PII masking. Never sends documents to the relay.
- **Relay** (cloud): routes requests between your AI agent and your laptop.
  Never sees your documents — only encrypted request envelopes.
- **Agent** (AI client): connects to the relay with a bearer token.

## Security

- Documents never leave your laptop.
- Two keys per account: `pgr_agent_*` (AI client auth) and `pgr_bridge_*`
  (laptop-to-relay auth). Keys are SHA-256 hashed at rest.
- PII is masked before compression using Microsoft Presidio.
- Rate limited: 100 calls/hour, max 2 bridges per account.
- 5 signups per IP per day.

## Known limitations

- Text/markdown/code only — convert PDFs/DOCX to text first.
- Models run on your laptop; large libraries may need `PAGER_LITE=true`.
- No admin UI — use CLI from the command line.

## Documentation

- [GETTING_STARTED.md](GETTING_STARTED.md) — step-by-step setup for beginners
- [CONNECTING_AGENTS.md](CONNECTING_AGENTS.md) — MCP client configuration
- [CONTEXT.md](CONTEXT.md) — full architecture, tool contracts, and config reference
- [CHANGELOG.md](CHANGELOG.md) — version history
- [examples/goldfish_agent/](examples/goldfish_agent/) — reference agent implementation

## License

MIT
