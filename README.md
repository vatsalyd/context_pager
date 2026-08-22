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

---

There are **three ways** to use Context Pager. Pick the one that fits your
setup — each one is a complete, self-contained guide from zero to working.

| | Local only | With relay | Self-hosted relay |
|---|---|---|---|
| **Time** | 2 min | 5 min | 15 min |
| **Internet** | Not needed | Required (first time) | Required |
| **Best for** | Trying it out, local dev | Multiple devices, cloud AI | Teams, full control |
| **Limitation** | Same machine only | None | You manage the server |

---

## What is Context Pager?

When you ask an AI like Claude to answer a question about your files, it normally
has to read the **entire file** into its context window. A 10,000-word document
costs you tokens for all 10,000 words, even if the answer is in one paragraph.

**Context Pager fixes this.** It:

1. **Indexes** your documents (splits them into chunks and creates searchable embeddings)
2. **Searches** for the most relevant chunks when you ask a question
3. **Compresses** the relevant pages so your AI reads ~4x fewer tokens
4. **Masks PII** (names, emails, phone numbers) before compression

The result: your AI gets the same answer for a fraction of the token cost.

---

## Prerequisites

- **Python 3.11 or newer** — check with `python --version`
- **~4GB of free disk space** — for the AI models (bge-m3 for embeddings, llmlingua-2 for compression)
- **~4GB of RAM** — the models need memory to run
- **A terminal/command prompt** — any will work (Terminal, PowerShell, iTerm, etc.)

> **Tip:** If `pager` works as a shortcut on your system, you can use `pager` instead
> of `python -m context_pager.cli` in all commands below. On Windows Store Python,
> the `pager` shortcut may not be on PATH — use the full `python -m` form.

---

## Way 1: Local only (simplest, 2 minutes)

No signup, no internet required, no keys. Your AI connects directly to the
bridge on your laptop.

### Step 1: Install

```bash
pip install "context-pager[bridge]"
```

The `[bridge]` part installs everything you need: the CLI, AI models, and
all dependencies. The first install takes a few minutes because it downloads
the models. Subsequent installs are fast.

Verify it worked:

```bash
python -m context_pager.cli --help
```

You should see the available commands: `bridge`, `serve`, `docs`, `stats`.

### Step 2: Add your documents

Context Pager works with **text files**: `.txt`, `.md`, `.py`, `.js`, `.ts`,
`.go`, `.rs`, `.java`, `.json`, `.yaml`, and many more code/text formats.

**Convert PDFs and Word docs to text first:**

```bash
pdftotext report.pdf report.txt
```

**Add files one at a time:**

```bash
python -m context_pager.cli docs add report.txt
# -> added report.txt -> doc_id=a1b2c3d4e5f6
```

**Or add multiple files:**

```bash
python -m context_pager.cli docs add notes.md
python -m context_pager.cli docs add src/main.py
python -m context_pager.cli docs add README.md
```

Each file gets a unique `doc_id` (a short ID like `a1b2c3d4e5f6`). You'll
need this ID when your AI reads the file.

**See what's indexed:**

```bash
python -m context_pager.cli docs list
# -> a1b2c3d4e5f6  report  text  chunks=12  indexed=2026-08-20T10:00:00
# -> f6e5d4c3b2a1  notes   text  chunks=4   indexed=2026-08-20T10:01:00
```

### Step 3: Start the bridge

```bash
python -m context_pager.cli bridge
```

On the first run, this downloads and loads the AI models (takes ~30 seconds).
After that, it starts instantly.

You'll see output like:

```
INFO:context_pager.bridge.client:PAGER_BRIDGE_KEY not set -- serving localhost MCP only.
```

This means the bridge is running and serving on `http://127.0.0.1:8000/mcp`.
Your AI can now connect to it locally.

**Keep this terminal open** — the bridge needs to stay running while your AI
uses it.

### Step 4: Connect your AI client

#### Claude Desktop

1. Open Claude Desktop
2. Go to **Settings** → **Developer** → **Edit Config**
3. Add the `mcpServers` section:

```json
{
  "mcpServers": {
    "pager": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

4. Restart Claude Desktop

#### Claude Code

```bash
claude mcp add pager --transport http http://127.0.0.1:8000/mcp
```

Or edit `.mcp.json`:

```json
{
  "mcpServers": {
    "pager": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

#### Cursor

1. Open Cursor → **Settings** → **MCP**
2. Click **Add new MCP server**
3. Fill in: **Name:** `pager`, **Type:** `http`, **URL:** `http://127.0.0.1:8000/mcp`
4. Click **Save**

#### OpenCode

Add to your `opencode.json`:

```json
{
  "mcp": {
    "pager": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

#### Any other MCP client

Context Pager works with **any MCP-compatible client**. You need:

- **Transport:** HTTP (Streamable HTTP)
- **URL:** `http://127.0.0.1:8000/mcp`

---

## Way 2: With relay (remote access, 5 minutes)

Access your documents from anywhere — your phone, another computer, or
cloud-based AI tools.

### Step 1: Install (same as Way 1)

```bash
pip install "context-pager[bridge]"
python -m context_pager.cli --help
```

### Step 2: Add your documents (same as Way 1)

```bash
python -m context_pager.cli docs add report.txt
python -m context_pager.cli docs add notes.md
```

### Step 3: Get your keys (one-time, free)

```bash
curl -X POST https://context-pager.duckdns.org/v1/signup
```

This returns:

```json
{
  "user_id": "your-user-id",
  "agent_key": "pgr_agent_...",
  "bridge_key": "pgr_bridge_...",
  "note": "Store these now — keys are shown once and stored only as hashes."
}
```

**Save both keys.** You won't see them again.

### Step 4: Start the bridge with relay key

Set the bridge key so your laptop can talk to the relay:

**Windows PowerShell:**

```powershell
$env:PAGER_BRIDGE_KEY="pgr_bridge_..."
python -m context_pager.cli bridge
```

**macOS / Linux:**

```bash
export PAGER_BRIDGE_KEY="pgr_bridge_..."
python -m context_pager.cli bridge
```

You should see the bridge connect to the relay. Keep this terminal open.

### Step 5: Connect your AI client to the relay

The relay URL is `https://context-pager.duckdns.org/mcp`. Every client needs
the `Authorization: Bearer pgr_agent_...` header.

#### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "pager": {
      "type": "http",
      "url": "https://context-pager.duckdns.org/mcp",
      "headers": {
        "Authorization": "Bearer pgr_agent_..."
      }
    }
  }
}
```

Replace `pgr_agent_...` with your actual agent key. Restart Claude Desktop.

#### Claude Code

```bash
claude mcp add pager --transport http https://context-pager.duckdns.org/mcp --header "Authorization: Bearer pgr_agent_..."
```

Or edit `.mcp.json`:

```json
{
  "mcpServers": {
    "pager": {
      "type": "http",
      "url": "https://context-pager.duckdns.org/mcp",
      "headers": {
        "Authorization": "Bearer pgr_agent_..."
      }
    }
  }
}
```

#### Cursor

1. Open Cursor → **Settings** → **MCP**
2. Click **Add new MCP server**
3. Fill in:
   - **Name:** `pager`
   - **Type:** `http`
   - **URL:** `https://context-pager.duckdns.org/mcp`
   - **Headers:** `{ "Authorization": "Bearer pgr_agent_..." }`
4. Click **Save**

#### OpenCode

Add to `opencode.json`:

```json
{
  "mcp": {
    "pager": {
      "type": "http",
      "url": "https://context-pager.duckdns.org/mcp",
      "headers": {
        "Authorization": "Bearer pgr_agent_..."
      }
    }
  }
}
```

#### Any other MCP client

- **Transport:** HTTP (Streamable HTTP)
- **URL:** `https://context-pager.duckdns.org/mcp`
- **Auth header:** `Authorization: Bearer pgr_agent_...`

### How it works

Your laptop's bridge connects *outward* to the relay. The relay never connects
*to* your laptop — this means it works behind firewalls and NAT without any
port forwarding.

---

## Way 3: Self-hosted relay (full control, 15 minutes)

Deploy your own relay on AWS free tier ($0/month). You control the server,
the keys, and the data flow.

### Step 1: Launch an AWS instance

1. Launch a **t3.micro** with Ubuntu 22.04 or 24.04
2. Open port 80 and 443 in the security group
3. SSH into the instance

### Step 2: Get a free DuckDNS domain

1. Go to [duckdns.org](https://www.duckdns.org)
2. Sign in with GitHub/Google
3. Create a subdomain (e.g., `myserver.duckdns.org`)
4. Point it to your EC2 instance's public IP

### Step 3: Run the setup script

Clone this repo on the EC2 instance and run the setup:

```bash
git clone https://github.com/vatsalyd/context_pager.git
cd context_pager
sudo PAGER_DUCKDNS_DOMAIN=myserver PAGER_DUCKDNS_TOKEN=your-token bash deploy/setup_relay.sh
```

This script:

- Installs the relay package in a Python venv
- Sets up Caddy (free automatic TLS certificates)
- Creates a systemd service (auto-starts on boot)
- Configures nightly database backups
- Points your DuckDNS domain to the server

### Step 4: Get your keys

```bash
curl -X POST https://myserver.duckdns.org/v1/signup
```

This returns your `agent_key` and `bridge_key`. Save both.

### Step 5: Start your bridge

```bash
# Windows PowerShell:
$env:PAGER_BRIDGE_KEY="pgr_bridge_..."
python -m context_pager.cli bridge

# macOS / Linux:
export PAGER_BRIDGE_KEY="pgr_bridge_..."
python -m context_pager.cli bridge
```

### Step 6: Connect your AI client

Use your own relay URL instead of `context-pager.duckdns.org`:

```
URL: https://myserver.duckdns.org/mcp
Header: Authorization: Bearer pgr_agent_...
```

Follow the client-specific instructions from [Way 2](#way-2-with-relay-remote-access-5-minutes)
replacing the URL and key.

---

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
