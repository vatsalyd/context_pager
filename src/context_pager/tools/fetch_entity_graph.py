from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from context_pager.deps import Dependencies
from context_pager.knowledge.retriever import retrieve_entities_rrf, expand_entity_graph
from context_pager.knowledge.semantic_router import route_query
from context_pager.cache.lru import get_cached_entities, set_cached_entities
from context_pager.cache.decay import touch_active_entity
from context_pager.telemetry.audit import log_audit_event
from context_pager.telemetry.cost import calculate_savings


async def fetch_entity_graph(
    query: str,
    relation: str,
    page_token: str | None = None,
    limit: int = 20,
) -> str:
    start_time = time.time()
    tenant_id = "default"  # Will be overridden by middleware
    session_id = "default"

    # Check cache first
    cache_key = f"entity_graph:{hashlib.sha256(f'{query}:{relation}:{page_token}'.encode()).hexdigest()[:16]}"
    cached = await get_cached_entities(tenant_id, cache_key)
    if cached:
        cached["metadata"]["cache_hit"] = True
        cached["metadata"]["elapsed_ms"] = int((time.time() - start_time) * 1000)
        return json.dumps(cached)

    # Route query - for entity graph we search entities
    route = await route_query(query)
    if route != "structured":
        # Still try entity search, but log
        pass

    # RRF retrieval over entities
    entity_hits = await retrieve_entities_rrf(query, relation, limit * 3)

    # BFS expansion with pagination
    entities, relations, next_page_token = await expand_entity_graph(
        [h["id"] for h in entity_hits],
        relation,
        limit,
        page_token,
    )

    # Silent recall from agent_memory
    from context_pager.tools.commit_to_long_term_memory import recall_relevant_insights
    recalled = await recall_relevant_insights(query, tenant_id)

    # Build response envelope
    envelope = {
        "tool": "fetch_entity_graph",
        "query": query,
        "relation": relation,
        "entities": entities,
        "relations": relations,
        "summary": _generate_graph_summary(entities, relations),
        "recalled_insights": recalled,
        "next_page_token": next_page_token,
        "metadata": {
            "entities_returned": len(entities),
            "relations_returned": len(relations),
            "cache_hit": False,
            "elapsed_ms": int((time.time() - start_time) * 1000),
        },
    }

    # Cache the result
    await set_cached_entities(tenant_id, cache_key, envelope)

    # Track active entities for decay
    for entity in entities:
        await touch_active_entity(tenant_id, session_id, entity["id"])

    # Audit log
    await log_audit_event(
        tenant_id=tenant_id,
        event_type="tool_call",
        tool_name="fetch_entity_graph",
        session_id=session_id,
        metadata={"query": query, "relation": relation, "entities": len(entities)},
    )

    return json.dumps(envelope)


def _generate_graph_summary(entities: list[dict], relations: list[dict]) -> str:
    if not entities:
        return "No entities found matching query."
    types = {}
    for e in entities:
        types[e["type"]] = types.get(e["type"], 0) + 1
    type_str = ", ".join(f"{v} {k}" for k, v in types.items())
    return f"Found {len(entities)} entities ({type_str}) with {len(relations)} relations."