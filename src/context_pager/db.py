from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg

from context_pager.deps import Dependencies
from context_pager.contextvar import tenant_id_var


@asynccontextmanager
async def acquire_conn(tenant_id: str | None = None) -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a pooled connection with RLS tenant GUC pre-set.

    Sets `app.tenant_id` so `current_setting('app.tenant_id', true)` in RLS policies
    resolves to the real tenant. If no tenant_id is provided, reads from contextvar.

    Uses SET (session-level) instead of SET LOCAL because asyncpg pool connections
    use autocommit mode where each statement is its own transaction — SET LOCAL
    would not persist to subsequent statements. Session-level SET is safe because
    the pool resets connections on return.
    """
    tid = tenant_id or tenant_id_var.get()
    pool = await Dependencies.pg_pool()
    async with pool.acquire() as conn:
        if tid:
            try:
                await conn.execute(f"SET app.tenant_id = '{tid}'")
            except Exception:
                pass
        yield conn