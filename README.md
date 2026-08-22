# Context Pager

> **[Get Started (5 min) →](GETTING_STARTED.md)** — step-by-step guide with
> screenshots for every MCP client (Claude Desktop, Claude Code, Cursor, OpenCode).

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

## Install

```bash
pip install "context-pager[bridge]"
```

This installs everything you need: the CLI, embedding models, compression, and
PII masking. First run downloads ~5GB of models (bge-m3 + llmlingua-2); subsequent
starts are instant.

**Requirements:** Python 3.11+, ~4GB RAM for models.

## Quick start (2 minutes, no signup)

```bash
# 1. Add some documents
python -m context_pager.cli docs add report.txt
python -m context_pager.cli docs add notes.md
python -m context_pager.cli docs add src/main.py

# 2. Start the bridge (serves on your laptop only)
python -m context_pager.cli bridge

# 3. Connect your MCP client to http://127.0.0.1:8000/mcp (no auth needed)
```

That's it. Your AI can now search and read your documents.

> **Tip:** If `pager` works as a shortcut on your system, use it instead of
> `python -m context_pager.cli`. On Windows Store Python, use the full form.

## Connect to the relay (remote access, 5 minutes)

The **relay** is a free cloud server that lets your AI access documents from
anywhere — your phone, another computer, or a cloud-based AI tool.

```bash
# 1. Get your keys (one-time, free)
curl -X POST https://context-pager.duckdns.org/v1/signup
# Returns: {"agent_key": "pgr_agent_...", "bridge_key": "pgr_bridge_..."}

# 2. Save the bridge key (this lets your laptop talk to the relay)
# On Windows: $env:PAGER_BRIDGE_KEY="pgr_bridge_..."
# On macOS/Linux: export PAGER_BRIDGE_KEY="pgr_bridge_..."
python -m context_pager.cli bridge

# 3. Connect your MCP client to the relay
# URL: https://context-pager.duckdns.org/mcp
# Header: Authorization: Bearer pgr_agent_...
```

See [GETTING_STARTED.md](GETTING_STARTED.md) for detailed setup with every
MCP client (Claude Desktop, Claude Code, Cursor, OpenCode).

## CLI commands

```bash
# Document management
python -m context_pager.cli docs add <file>              # Add a file to your library
python -m context_pager.cli docs list                    # List all indexed documents
python -m context_pager.cli docs reindex <doc_id>       # Re-embed after editing a file
python -m context_pager.cli docs remove <doc_id>        # Remove a document

# Stats
python -m context_pager.cli stats                        # See tokens saved and cost reduction

# Services
python -m context_pager.cli bridge                       # Start the bridge (laptop)
python -m context_pager.cli serve                        # Start the relay (server, for self-hosters)
```

**Supported file types:** `.txt`, `.md`, `.mdx`, `.py`, `.js`, `.ts`, `.tsx`,
`.jsx`, `.go`, `.rs`, `.sql`, `.sh`, `.json`, `.yaml`, `.yml`, `.toml`, `.java`,
`.c`, `.cpp`, `.h`, `.rb`, `.php`

PDFs and DOCX files must be converted to text first (e.g., `pdftotext file.pdf
file.txt`).

## Self-host the relay (optional, $0/month)

If you want full control, deploy your own relay on AWS free tier:

```bash
git clone https://github.com/vatsalyd/context_pager.git
cd context_pager
sudo PAGER_DUCKDNS_DOMAIN=myserver PAGER_DUCKDNS_TOKEN=xxxx bash deploy/setup_relay.sh
```

This provisions a t3.micro, installs Caddy (free TLS), sets up systemd, and
configures nightly backups. See [CONTEXT.md](CONTEXT.md) for details.

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
- **Agent**: your AI client (Claude, Cursor, etc.) connects to the relay via HTTPS.
- **Relay**: routes requests to your bridge. Never sees your documents.
- **Bridge**: runs on your laptop, stores documents, runs AI models. Dials out to the relay.

- **Bridge** (your laptop): stores your documents, runs AI models for
  compression and search, handles PII masking. Never sends documents to the relay.
- **Relay** (cloud): routes requests between your AI agent and your laptop.
  Never sees your documents — only encrypted request envelopes.
- **Agent** (AI client): connects to the relay with a bearer token.

## How it works

1. **Index:** `python -m context_pager.cli docs add file.txt` copies the file, splits it into
   512-token chunks, and creates vector embeddings for search.
2. **Search:** your agent asks a question; the bridge finds the most relevant
   chunks across all documents.
3. **Compress:** the bridge returns a compressed page (~4x smaller) with PII
   (names, emails, phone numbers) automatically masked.
4. **Memory:** your agent can commit insights that resurface on later searches.

## Testing

```bash
python -m pytest tests/
python -m ruff check src tests
```

Opt-in real-model tests: `RUN_MODEL_TESTS=1` (not run in CI).

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
- No admin UI — use `python -m context_pager.cli docs` and `python -m context_pager.cli stats` from the command line.

## Documentation

- [GETTING_STARTED.md](GETTING_STARTED.md) — step-by-step guide for beginners
- [CONNECTING_AGENTS.md](CONNECTING_AGENTS.md) — MCP client configuration
- [CONTEXT.md](CONTEXT.md) — full architecture, tool contracts, and config reference
- [CHANGELOG.md](CHANGELOG.md) — version history
- [examples/goldfish_agent/](examples/goldfish_agent/) — reference agent implementation

## License

MIT
