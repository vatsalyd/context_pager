from __future__ import annotations

import logging

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware

from context_pager.config import settings
from context_pager.deps import lifespan
from context_pager.auth.middleware import AuthBackend
from context_pager.rate_limit.middleware import RateLimitMiddleware
from context_pager.governance.middleware import PIIRedactionMiddleware


logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("context_pager")


mcp = FastMCP(
    "pager",
    lifespan=lifespan,
)


@mcp.tool
async def fetch_entity_graph(
    query: str,
    relation: str,
    page_token: str | None = None,
    limit: int = 20,
) -> str:
    """
    Retrieve structured entity graph for a query, filtered by relation type.

    Args:
        query: Natural language query (e.g., "Acme Corp executives")
        relation: Required relation filter (e.g., "EXECUTIVE_LEADERSHIP", "SUBSIDIARY_COMPANIES", "MENTIONED_WITH")
        page_token: Opaque token from previous call to fetch next page
        limit: Max entities to return (default 20, max 100)

    Returns:
        JSON envelope with entities[], relations[], summary, next_page_token, metadata
    """
    from context_pager.tools.fetch_entity_graph import fetch_entity_graph as impl
    return await impl(query, relation, page_token, limit)


@mcp.tool
async def compress_document(
    doc_id: str,
    focus_area: str | None = None,
    max_return_tokens: int = 2048,
) -> str:
    """
    Fetch a compressed 'page' of a document. Zero-copy: raw text never leaves server.

    Args:
        doc_id: Document ID from upload
        focus_area: Optional focus hint (e.g., "quarterly revenue"). Honored only when Ollama fallback enabled.
        max_return_tokens: Target compressed size (default 2048, max 8000)

    Returns:
        JSON envelope with pages[], summary, predecessor, metadata
    """
    from context_pager.tools.compress_document import compress_document as impl
    return await impl(doc_id, focus_area, max_return_tokens)


@mcp.tool
async def commit_to_long_term_memory(
    key: str,
    insights: str,
) -> str:
    """
    Persist a key insight for automatic recall in future entity graph fetches.

    Args:
        key: Short memorable key (e.g., "acme_q3_revenue")
        insights: Dense factual summary the agent wants to remember

    Returns:
        JSON envelope with acknowledgement
    """
    from context_pager.tools.commit_to_long_term_memory import commit_to_long_term_memory as impl
    return await impl(key, insights)


def main():
    """Run MCP server with streamable HTTP transport + HTTP middleware stack."""
    from context_pager.contextvar import tenant_id_var
    from starlette.requests import Request
    from starlette.middleware.base import BaseHTTPMiddleware

    class TenantMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            tid = getattr(request.state, "tenant_id", "")
            token = tenant_id_var.set(tid)
            try:
                return await call_next(request)
            finally:
                tenant_id_var.reset(token)

    http_middleware = [
        Middleware(AuthenticationMiddleware, backend=AuthBackend()),
        Middleware(TenantMiddleware),
        Middleware(RateLimitMiddleware),
        Middleware(PIIRedactionMiddleware),
    ]
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=8000,
        middleware=http_middleware,
    )


if __name__ == "__main__":
    main()
