from __future__ import annotations

import hashlib
import json
import time

from context_pager.deps import Dependencies
from context_pager.contextvar import tenant_id_var
from context_pager.knowledge.retriever import retrieve_entities_rrf, expand_entity_graph
from context_pager.cache.lru import get_cached_entities, set_cached_entities
from context_pager.cache.decay import touch_active_entity
from context_pager.telemetry.audit import log_audit_event


async def fetch_entity_graph(
    query: str,
    relation: str,
    page_token: str | None = None,
    limit: int = 20,
) -> str:
    """Retrieve structured entity graph filtered by relation type."""
    start_time = time.time()
    tenant_id = tenant_id_var.get()
    session_id = "default"

    # Q18: Cache key includes relation + page_token for filter-aware pagination
    cache_key = hashlib.sha256(
        f"{query}:{relation}:{page_token or ''}".encode()
    ).hexdigest()[:16]
    cached = await get_cached_entities(tenant_id, cache_key)
    if cached:
        cached.setdefault("metadata", {})["cache_hit"] = True
        cached["metadata"]["elapsed_ms"] = int((time.time() - start_time) * 1000)
        return json.dumps(cached)

    # RRF retrieval over entities (uses dense + sparse, k=60)
    entity_hits = await retrieve_entities_rrf(query, relation, limit * 3)

    # Q18: BFS expansion with `relation` filter + pagination
    entities, relations, next_page_token = await expand_entity_graph(
        [h.id for h in entity_hits],
        relation,
        limit,
        page_token,
    )

    # Q3: Silent recall from agent_memory
    from context_pager.tools.commit_to_long_term_memory import recall_relevant_insights
    recalled = await recall_relevant_insights(query, tenant_id)
    # Q3 spec: prepend "Recalled insight: {key}: {insights}" block
    recalled_insights = [f"Recalled insight: {r['key']}: {r['insights']}" for r in recalled]

    # Build response envelope per spec §4
    envelope = {
        "tool": "fetch_entity_graph",
        "query": query,
        "relation": relation,
        "entities": entities,
        "relations": relations,
        "summary": _generate_graph_summary(entities, relations),
        "recalled_insights": recalled_insights,
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
    types: dict[str, int] = {}
    for e in entities:
        types[e["type"]] = types.get(e["type"], 0) + 1
    type_str = ", ".join(f"{v} {k}" for k, v in types.items())
    return f"Found {len(entities)} entities ({type_str}) with {len(relations)} relations."
