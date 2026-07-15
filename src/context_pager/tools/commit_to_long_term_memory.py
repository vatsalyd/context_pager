from __future__ import annotations

import json
import time
from typing import Any

from context_pager.deps import Dependencies
from context_pager.telemetry.audit import log_audit_event


async def commit_to_long_term_memory(key: str, insights: str) -> str:
    start_time = time.time()
    tenant_id = "default"
    session_id = "default"

    # Embed insights for future recall
    pool = await Dependencies.pg_pool()
    embedder = await Dependencies.embedder()
    embedding = (await embedder.embed_dense([insights]))[0]

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO agent_memory (key, tenant_id, insights, embedding)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (key) DO UPDATE SET
                insights = EXCLUDED.insights,
                embedding = EXCLUDED.embedding,
                last_recalled = NULL
        """, key, tenant_id, insights, embedding)

    await log_audit_event(
        tenant_id=tenant_id,
        event_type="tool_call",
        tool_name="commit_to_long_term_memory",
        session_id=session_id,
        metadata={"key": key, "insights_length": len(insights)},
    )

    return json.dumps({
        "tool": "commit_to_long_term_memory",
        "key": key,
        "status": "persisted",
        "metadata": {"elapsed_ms": int((time.time() - start_time) * 1000)},
    })


async def recall_relevant_insights(query: str, tenant_id: str, threshold: float = 0.78) -> list[dict]:
    """Query agent_memory for insights similar to query embedding."""
    embedder = await Dependencies.embedder()
    query_emb = (await embedder.embed_dense([query]))[0]

    pool = await Dependencies.pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT key, insights, 1 - (embedding <=> $1) AS similarity
            FROM agent_memory
            WHERE tenant_id = $2 AND 1 - (embedding <=> $1) > $3
            ORDER BY embedding <=> $1
            LIMIT 5
        """, query_emb, tenant_id, threshold)

    return [{"key": r["key"], "insights": r["insights"], "similarity": r["similarity"]} for r in rows]