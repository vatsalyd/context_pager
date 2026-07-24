from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from context_pager.db import acquire_conn

logger = logging.getLogger("context_pager.rollup")

ROLLUP_INTERVAL_SECONDS = 300  # 5 minutes


async def rollup_audit_events() -> int:
    """Aggregate audit_events into tenant_usage_daily for today. Returns rows upserted."""
    today = datetime.now(timezone.utc).date()
    async with acquire_conn("") as conn:
        # Use raw SQL to bypass RLS for system operation
        await conn.execute("RESET app.tenant_id")
        row = await conn.fetchrow("""
            INSERT INTO tenant_usage_daily (date, tenant_id, tool_calls, tokens_compressed, est_cost_usd)
            SELECT
                $1::date,
                tenant_id,
                count(*) FILTER (WHERE event_type = 'tool_call'),
                coalesce(sum(original_tokens - compressed_tokens), 0),
                coalesce(sum(cost_saved_usd), 0)
            FROM audit_events
            WHERE created_at >= $1::date
              AND created_at < ($1::date + interval '1 day')
            GROUP BY tenant_id
            ON CONFLICT (date, tenant_id) DO UPDATE SET
                tool_calls = EXCLUDED.tool_calls,
                tokens_compressed = EXCLUDED.tokens_compressed,
                est_cost_usd = EXCLUDED.est_cost_usd
        """, today)
        return row.rowcount if row else 0


async def rollup_loop() -> None:
    """Background loop: rollup every 5 minutes."""
    while True:
        try:
            n = await rollup_audit_events()
            if n:
                logger.info("Rollup: upserted %d tenant_usage_daily rows", n)
        except Exception:
            logger.exception("Rollup failed")
        await asyncio.sleep(ROLLUP_INTERVAL_SECONDS)
