# Goldfish Agent

A reference implementation of a "goldfish memory" agent using the Context Pager MCP server.

## Setup

1. Start the Context Pager MCP server:
   ```bash
   docker compose up -d
   ```

2. Set your Google API key:
   ```bash
   export GOOGLE_API_KEY="your-api-key"
   ```

3. Run the agent:
   ```bash
   python agent.py
   ```

## How It Works

The Goldfish Agent uses:
- **Context Pager MCP tools** for fetching entities and compressed documents
- **ProtectLatestToolResultMiddleware** to summarize tool results before context window fills
- **SummarizationMiddleware** (built into LangGraph) to collapse old messages

This creates a "goldfish memory" effect where the agent only remembers recent context but can always re-fetch information from the server.
