from __future__ import annotations

from typing import Literal

from context_pager.config import settings
from context_pager.knowledge.retriever import retrieve_documents_rrf, retrieve_entities_rrf


async def route_query(query: str) -> Literal["structured", "unstructured"]:
    """Route query to structured (entity graph) or unstructured (document) search."""
    doc_hits = await retrieve_documents_rrf(query, k=1)
    entity_hits = await retrieve_entities_rrf(query, "MENTIONED_WITH", k=1)

    doc_score = doc_hits[0].fused_score if doc_hits else 0.0
    entity_score = entity_hits[0].fused_score if entity_hits else 0.0

    # Bias toward structured if entity score clearly wins
    if entity_score > doc_score + settings.routing_margin:
        return "structured"
    return "unstructured"