from __future__ import annotations

import json
import time
from typing import Any

from context_pager.deps import Dependencies


async def log_audit_event(
    tenant_id: str,
    event_type: str,
    tool_name: str | None = None,
    session_id: str | None = None,
    doc_id: str | None = None,
    original_tokens: int | None = None,
    compressed_tokens: int | None = None,
    cost_saved_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert audit event into Postgres."""
    pool = await Dependencies.pg_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO audit_events (
                tenant_id, event_type, tool_name, session_id, doc_id,
                original_tokens, compressed_tokens, cost_saved_usd, metadata
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
            tenant_id,
            event_type,
            tool_name,
            session_id,
            doc_id,
            original_tokens,
            compressed_tokens,
            cost_saved_usd,
            json.dumps(metadata or {}),
        )