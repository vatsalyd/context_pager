from __future__ import annotations

import time
from typing import Literal

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from starlette.types import ASGIApp

from context_pager.config import settings
from context_pager.deps import Dependencies


Resource = Literal["tool_calls", "tokens", "docs", "sessions"]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.lua_script = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local window = tonumber(ARGV[2])
        local now = tonumber(ARGV[3])
        local weight = tonumber(ARGV[4])
        
        redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window * 1000)
        local count = redis.call('ZCARD', key)
        if count + weight > limit then
            return {0, count}
        end
        redis.call('ZADD', key, now, now .. '-' .. math.random())
        redis.call('EXPIRE', key, math.ceil(window / 1000) + 1)
        return {1, count + weight}
        """

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks and dashboard
        if request.url.path.startswith("/healthz") or request.url.path.startswith("/dashboard"):
            return await call_next(request)

        tenant_id = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            return await call_next(request)

        # Only apply to MCP tool endpoints
        if not request.url.path.startswith("/mcp"):
            return await call_next(request)

        redis = await Dependencies.redis()
        now_ms = int(time.time() * 1000)

        # Tool calls per hour
        allowed, _ = await redis.eval(
            self.lua_script,
            1,
            f"ratelimit:{tenant_id}:tool_calls",
            settings.rate_limit_tool_calls_per_hour,
            3600 * 1000,
            now_ms,
            1,
        )
        if not allowed:
            return JSONResponse(
                {"error": "Rate limit exceeded: tool calls per hour"},
                status_code=429,
            )

        return await call_next(request)


async def check_token_limit(tenant_id: str, tokens: int) -> bool:
    """Check if tenant has token budget for compression."""
    redis = await Dependencies.redis()
    key = f"ratelimit:{tenant_id}:tokens"
    now_ms = int(time.time() * 1000)
    window = 86400 * 1000  # 24 hours

    script = """
    local key = KEYS[1]
    local limit = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local weight = tonumber(ARGV[4])
    
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
    local count = 0
    local entries = redis.call('ZRANGE', key, 0, -1, 'WITHSCORES')
    for i = 1, #entries, 2 do
        count = count + tonumber(entries[i + 1])
    end
    if count + weight > limit then
        return 0
    end
    redis.call('ZADD', key, now, tostring(now) .. '-' .. weight)
    redis.call('EXPIRE', key, math.ceil(window / 1000) + 1)
    return 1
    """
    return bool(await redis.eval(script, 1, key, settings.rate_limit_tokens_per_day, window, now_ms, tokens))