from __future__ import annotations

from fastmcp import FastMCP

from context_pager.config import BridgeSettings
from context_pager.core import tools


def build_local_server(settings: BridgeSettings, models) -> FastMCP:
    """Localhost MCP server (Q29/Q33): same core underneath, no auth, loopback only."""
    mcp = FastMCP("context-pager-bridge")

    @mcp.tool()
    async def compress_document(
        doc_id: str,
        page: int = 1,
        focus_area: str | None = None,
        max_return_tokens: int | None = None,
    ) -> str:
        """Read one compressed page of a document. Returns a JSON envelope with
        content, page, pages_total, next_page, token_count and metadata.
        Focus on the page most relevant to `focus_area` when set.
        Returns an error envelope if the document is unknown or the page is out of range."""
        return await tools.compress_document(
            doc_id,
            page=page,
            focus_area=focus_area,
            max_return_tokens=max_return_tokens,
            settings=settings,
            models=models,
        )

    @mcp.tool()
    async def search_documents(query: str, top_k: int = 10) -> str:
        """Search the local library and return ranked results plus any silently
        recalled long-term memory. Each result has doc_id, title, snippet,
        best_page (1-indexed) and score. Returns a JSON envelope."""
        return await tools.search_documents(query, top_k=top_k, settings=settings, models=models)

    @mcp.tool()
    async def commit_to_long_term_memory(key: str, insights: str) -> str:
        """Persist a durable insight under `key` that future searches will silently
        recall (cosine-similarity gated). Overwrites any previous value for `key`.
        Returns a JSON envelope."""
        return await tools.commit_to_long_term_memory(key, insights, settings=settings, models=models)

    return mcp


async def serve_local(settings: BridgeSettings, models) -> None:
    """Run the localhost MCP server on 127.0.0.1:8000/mcp until cancelled."""
    mcp = build_local_server(settings, models)
    await mcp.run_http_async(
        host=settings.local_mcp_host,
        port=settings.local_mcp_port,
        path="/mcp",
        log_level=settings.log_level.lower(),
    )
