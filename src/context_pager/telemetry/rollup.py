from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from context_pager.db import acquire_conn

logger = logging.getLogger("context_pager.rollup")

ROLLUP_INTERVAL_SECONDS = 300  # 5 minutes


async def rollup_audit_events(since: date | None = None) -> int:
    """Aggregate audit_events into tenant_usage_daily.

    If `since` is None, processes all dates from the earliest unrolled
    audit_event date up to yesterday. Today's events are NOT rolled up
    (they're still live in audit_events).
    """
    async with acquire_conn("") as conn:
        await conn.execute("RESET app.tenant_id")

        # Find the date range to roll up
        if since is not None:
            start_date = since
        else:
            row = await conn.fetchrow(
                "SELECT min(created_at::date) as d FROM audit_events"
            )
            if not row or not row["d"]:
                return 0
            start_date = row["d"]

        # Yesterday is the last day we roll up (today is still live)
        yesterday = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=1)).date()

        if start_date > yesterday:
            return 0

        # Roll up day by day
        total = 0
        current = start_date
        while current <= yesterday:
            result = await conn.fetchrow("""
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
            """, current)
            total += result.rowcount if result else 0
            current = __import__("datetime").date.fromordinal(current.toordinal() + 1)

        return total


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
