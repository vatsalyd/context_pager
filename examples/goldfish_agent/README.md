# Goldfish Agent

A reference implementation of a "goldfish memory" agent using the Context Pager MCP
relay. It has no in-context long-term memory: it searches, reads compressed pages,
restates facts out loud, and commits insights to long-term memory so later tasks recall them.

## Setup

1. Install the agent extras and configure keys:

   ```bash
   pip install "context-pager[agent]"
   export GOOGLE_API_KEY="your-gemini-key"
   export PAGER_AGENT_KEY="pgr_agent_..."   # from /v1/signup (relay) — omit for localhost-only
   export PAGER_MCP_URL="https://pager.duckdns.org/mcp"   # default; or http://127.0.0.1:8000/mcp
   ```

2. Make sure a bridge is connected to the relay (or running locally) and documents are
   indexed (`pager docs add file.txt`).

3. Run the agent:

   ```bash
   python agent.py
   ```

## How It Works

- **search_documents** replaces the old `fetch_entity_graph` — it finds relevant
  documents and silently injects `recalled_insights` from long-term memory.
- **compress_document(doc_id, page=best_page, focus_area=...)** streams a compressed
  page instead of a full document.
- **commit_to_long_term_memory(key, insights)** persists facts; recall threshold 0.78.
- The prompt forces restating facts before fetching the next page, so nothing survives
  summarization unless it is committed to memory.
