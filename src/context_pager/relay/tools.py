from __future__ import annotations

import json

from fastmcp import Context, FastMCP

from context_pager.config import RelaySettings
from context_pager.envelopes import error_envelope
from context_pager.relay.connections import ConnectionManager
from context_pager.relay.db import RelayDB, sha256
from context_pager.relay.ratelimit import TokenBucket

_TOOL_TIMEOUT = 120.0


async def authorize(ctx, tool: str, db: RelayDB, bucket: TokenBucket) -> dict | str:
    """Validate the bearer agent key and apply the per-key rate limit.

    Returns the user dict on success, or an error-envelope string on failure.
    """
    request = ctx.request_context.request if ctx is not None and ctx.request_context is not None else None
    if request is None:
        return error_envelope(tool, "unauthorized: no request context", retryable=False)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return error_envelope(tool, "unauthorized: missing bearer token", retryable=False)
    token = header.split(" ", 1)[1].strip()
    user = db.user_by_agent_hash(sha256(token))
    if user is None:
        return error_envelope(tool, "unauthorized: invalid agent key", retryable=False)
    if not bucket.allow(user["agent_key_hash"]):
        return error_envelope(tool, "rate limit exceeded (100 calls/hour)", retryable=True)
    return user


async def route_call(user: dict, method: str, params: dict, manager: ConnectionManager, db: RelayDB) -> str:
    """Forward a tool call to the user's live bridge and return the envelope.

    The relay is a dumb router: it stores no content, only daily usage rollups.
    """
    conn = manager.pick(user["user_id"])
    if conn is None:
        return error_envelope(method, "bridge_offline: your bridge is not connected", retryable=True)
    resp = await conn.call(method, params, timeout=_TOOL_TIMEOUT)
    if resp.error:
        return error_envelope(method, resp.error.message, retryable=True)
    env = resp.result
    db.record_usage(user["user_id"], env)
    return json.dumps(env)


def register_relay_tools(mcp: FastMCP, settings: RelaySettings, db: RelayDB, manager: ConnectionManager) -> None:
    bucket = TokenBucket(settings.rate_limit_calls_per_hour, settings.rate_limit_calls_per_hour / 3600.0)

    @mcp.tool()
    async def compress_document(
        doc_id: str,
        page: int = 1,
        focus_area: str | None = None,
        max_return_tokens: int | None = None,
        ctx: Context = None,
    ) -> str:
        """Read one compressed page of a document. Returns a JSON envelope with
        content, page, pages_total, next_page, token_count and metadata.
        Focus on the page most relevant to `focus_area` when set."""
        user = await authorize(ctx, "compress_document", db, bucket)
        if isinstance(user, str):
            return user
        return await route_call(
            user,
            "compress_document",
            {"doc_id": doc_id, "page": page, "focus_area": focus_area, "max_return_tokens": max_return_tokens},
            manager,
            db,
        )

    @mcp.tool()
    async def search_documents(query: str, top_k: int = 10, ctx: Context = None) -> str:
        """Search the library and return ranked results plus any silently recalled
        long-term memory. Each result has doc_id, title, snippet, best_page and score."""
        user = await authorize(ctx, "search_documents", db, bucket)
        if isinstance(user, str):
            return user
        return await route_call(user, "search_documents", {"query": query, "top_k": top_k}, manager, db)

    @mcp.tool()
    async def commit_to_long_term_memory(key: str, insights: str, ctx: Context = None) -> str:
        """Persist a durable insight under `key` that future searches will silently
        recall (cosine-similarity gated). Overwrites any previous value for `key`."""
        user = await authorize(ctx, "commit_to_long_term_memory", db, bucket)
        if isinstance(user, str):
            return user
        return await route_call(user, "commit_to_long_term_memory", {"key": key, "insights": insights}, manager, db)
