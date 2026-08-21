# Connecting Agents

Every agent config is the same shape: **one MCP URL + one bearer token**
(`pgr_agent_*`). Point it at the relay (`https://context-pager.duckdns.org/mcp`)
for remote use, or at your laptop bridge (`http://127.0.0.1:8000/mcp`, no auth)
for local-only.

## Get keys

1. Get the relay's public URL (default `https://context-pager.duckdns.org`).
2. `POST /v1/signup` once per user:
   ```bash
   curl -X POST https://context-pager.duckdns.org/v1/signup
   # {"user_id":"...","agent_key":"pgr_agent_...","bridge_key":"pgr_bridge_...",...}
   ```
3. Put `agent_key` in your MCP config below; put `bridge_key` in the bridge's env
   (`PAGER_BRIDGE_KEY`) so it can relay.

## Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

**Local only** (no auth needed):

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

**With relay** (remote access):

```json
{
  "mcpServers": {
    "pager": {
      "type": "http",
      "url": "https://context-pager.duckdns.org/mcp",
      "headers": { "Authorization": "Bearer pgr_agent_..." }
    }
  }
}
```

## Claude Code

CLI add:

```bash
claude mcp add pager --transport http http://127.0.0.1:8000/mcp
```

Or `.mcp.json` (project root):

**Local only:**

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

**With relay:**

```json
{
  "mcpServers": {
    "pager": {
      "type": "http",
      "url": "https://context-pager.duckdns.org/mcp",
      "headers": { "Authorization": "Bearer pgr_agent_..." }
    }
  }
}
```

## Cursor

Settings → MCP → **Add new MCP server**:

- **Name:** `pager`
- **Type:** `http`
- **URL:** `http://127.0.0.1:8000/mcp` (local) or `https://context-pager.duckdns.org/mcp` (relay)
- **Headers:** `{ "Authorization": "Bearer pgr_agent_..." }` (relay mode only)

## OpenCode

Add to `opencode.json` in your project root:

**Local only:**

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

**With relay:**

```json
{
  "mcp": {
    "pager": {
      "type": "http",
      "url": "https://context-pager.duckdns.org/mcp",
      "headers": { "Authorization": "Bearer pgr_agent_..." }
    }
  }
}
```

## Any MCP client (raw HTTP)

Streamable HTTP transport, bearer header:

```bash
curl -X POST https://context-pager.duckdns.org/mcp \
  -H "Authorization: Bearer pgr_agent_..." \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"1.0"}}}'
```

For local mode, replace the URL with `http://127.0.0.1:8000/mcp` and omit the
`Authorization` header.

## Reference agent

See `examples/goldfish_agent/` — a LangGraph agent that treats every search as a fresh
start, reads compressed pages, restates facts, and commits insights to memory.

## Rules of thumb for the agent

- **Always `json.loads` the tool result** before reading fields.
- Read `metadata.cache_hit` / `metadata.skipped_compression` before trusting token counts.
- If `metadata.retryable` is `true`, retry with backoff (bridge may be reconnecting).
- Treat `recalled_insights` from `search_documents` as ground truth you wrote earlier.
- Keep `focus_area` on `compress_document` to jump to the relevant page.
