from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from context_pager.deps import Dependencies
from context_pager.db import acquire_conn
from context_pager.knowledge.embedder import get_embedder

K_RRF = 60


class DocumentHit:
    def __init__(self, document_id: str, fused_score: float, best_chunk_text: str):
        self.document_id = document_id
        self.fused_score = fused_score
        self.best_chunk_text = best_chunk_text


class EntityHit:
    def __init__(self, id: int, name: str, type: str, properties: dict, document_id: str, fused_score: float):
        self.id = id
        self.name = name
        self.type = type
        self.properties = properties
        self.document_id = document_id
        self.fused_score = fused_score


async def retrieve_documents_rrf(query: str, k: int = 10) -> list[DocumentHit]:
    embedder = get_embedder()
    q_dense, q_sparse = await embedder.embed_multi([query])

    async with acquire_conn() as conn:
        # Dense search
        dense_rows = await conn.fetch("""
            SELECT id, document_id, text, 1 - (embedding <=> $1) AS score
            FROM document_chunks
            WHERE tenant_id = current_setting('app.tenant_id', true)
            ORDER BY embedding <=> $1
            LIMIT $2
        """, q_dense[0], k * 20)

        # Sparse search - convert sparse weights to pgvector sparsevec format
        sparse_vec = _dict_to_sparsevec(q_sparse[0])
        sparse_rows = await conn.fetch("""
            SELECT id, document_id, text, 1 - (sparse_weights <=> $1) AS score
            FROM document_chunks
            WHERE tenant_id = current_setting('app.tenant_id', true)
            ORDER BY sparse_weights <=> $1
            LIMIT $2
        """, sparse_vec, k * 20)

    # RRF fusion
    chunk_scores: dict[int, float] = defaultdict(float)
    chunk_meta: dict[int, dict] = {}

    for rank, r in enumerate(dense_rows, 1):
        chunk_scores[r["id"]] += 1.0 / (K_RRF + rank)
        chunk_meta[r["id"]] = dict(r)
    for rank, r in enumerate(sparse_rows, 1):
        chunk_scores[r["id"]] += 1.0 / (K_RRF + rank)
        chunk_meta[r["id"]].update(dict(r))

    # Aggregate to document level (max chunk score per doc)
    doc_scores: dict[str, float] = defaultdict(float)
    doc_meta: dict[str, dict] = {}
    for chunk_id, score in chunk_scores.items():
        meta = chunk_meta[chunk_id]
        doc_id = meta["document_id"]
        if score > doc_scores[doc_id]:
            doc_scores[doc_id] = score
            doc_meta[doc_id] = {"document_id": doc_id, "best_chunk_text": meta["text"]}

    ranked = sorted(doc_scores.items(), key=lambda x: -x[1])[:k]
    return [DocumentHit(doc_id=doc_id, fused_score=score, **doc_meta[doc_id]) for doc_id, score in ranked]


async def retrieve_entities_rrf(query: str, relation_filter: str, k: int = 20) -> list[EntityHit]:
    embedder = get_embedder()
    q_dense, q_sparse = await embedder.embed_multi([query])

    async with acquire_conn() as conn:
        # Entity dense search
        dense_rows = await conn.fetch("""
            SELECT e.id, e.name, e.type, e.properties, e.document_id,
                   1 - (e.embedding <=> $1) AS score
            FROM entities e
            WHERE e.tenant_id = current_setting('app.tenant_id', true)
            ORDER BY e.embedding <=> $1
            LIMIT $2
        """, q_dense[0], k * 2)

        # Entity sparse search (name-only sparse weights)
        sparse_vec = _dict_to_sparsevec(q_sparse[0])
        sparse_rows = await conn.fetch("""
            SELECT e.id, e.name, e.type, e.properties, e.document_id,
                   1 - (e.sparse_weights <=> $1) AS score
            FROM entities e
            WHERE e.tenant_id = current_setting('app.tenant_id', true)
            ORDER BY e.sparse_weights <=> $1
            LIMIT $2
        """, sparse_vec, k * 2)

    # RRF fusion
    entity_scores: dict[int, float] = defaultdict(float)
    entity_meta: dict[int, dict] = {}

    for rank, r in enumerate(dense_rows, 1):
        entity_scores[r["id"]] += 1.0 / (K_RRF + rank)
        entity_meta[r["id"]] = dict(r)
    for rank, r in enumerate(sparse_rows, 1):
        entity_scores[r["id"]] += 1.0 / (K_RRF + rank)
        entity_meta[r["id"]].update(dict(r))

    ranked = sorted(entity_scores.items(), key=lambda x: -x[1])[:k]
    results = []
    for eid, score in ranked:
        meta = entity_meta[eid]
        results.append(EntityHit(
            id=eid,
            name=meta["name"],
            type=meta["type"],
            properties=meta["properties"],
            document_id=meta["document_id"],
            fused_score=score,
        ))
    return results


async def expand_entity_graph(
    seed_entity_ids: list[int],
    relation_filter: str,
    limit: int,
    page_token: str | None,
) -> tuple[list[dict], list[dict], str | None]:
    """BFS over entity_relations filtered by relation type, paginated via opaque token.

    Q18: Required `relation` filter, opaque `page_token` encodes offset+filter signature.
    """
    import base64
    import json as _json

    # Decode page_token -> offset (token also encodes filter signature to detect drift)
    offset = 0
    if page_token:
        try:
            decoded = _json.loads(base64.urlsafe_b64decode(page_token.encode()).decode())
            if decoded.get("relation") != relation_filter:
                # Filter changed — restart pagination
                offset = 0
            else:
                offset = int(decoded.get("offset", 0))
        except Exception:
            offset = 0

    # First: include seed entities themselves with their properties
    async with acquire_conn() as conn:
        if seed_entity_ids:
            # Use ANY($1::bigint[]) for IN clause
            seed_rows = await conn.fetch("""
                SELECT id, name, type, properties, document_id
                FROM entities
                WHERE id = ANY($1::bigint[])
                  AND tenant_id = current_setting('app.tenant_id', true)
                ORDER BY id
            """, seed_entity_ids)
        else:
            seed_rows = []

        # BFS depth=1: relations FROM seed entities (filtered by relation type)
        relation_rows = await conn.fetch("""
            SELECT er.from_id, er.to_id, er.relation, er.properties
            FROM entity_relations er
            WHERE er.tenant_id = current_setting('app.tenant_id', true)
              AND er.relation = $1
              AND er.from_id = ANY($2::bigint[])
            ORDER BY er.from_id, er.to_id
            LIMIT $3 OFFSET $4
        """, relation_filter, seed_entity_ids, limit, offset)

        # Collect the "to" entity IDs we need to fetch
        to_ids = list({r["to_id"] for r in relation_rows})
        if to_ids:
            to_rows = await conn.fetch("""
                SELECT id, name, type, properties, document_id
                FROM entities
                WHERE id = ANY($1::bigint[])
                  AND tenant_id = current_setting('app.tenant_id', true)
            """, to_ids)
        else:
            to_rows = []

    # Build entity lookup
    entity_lookup: dict[int, dict] = {}
    for r in list(seed_rows) + list(to_rows):
        entity_lookup[r["id"]] = {
            "id": r["id"],
            "name": r["name"],
            "type": r["type"],
            "properties": r["properties"] if isinstance(r["properties"], dict) else {},
            "document_id": r["document_id"],
        }

    entities = list(entity_lookup.values())
    relations = [
        {
            "from_id": r["from_id"],
            "to_id": r["to_id"],
            "relation": r["relation"],
            "properties": r["properties"] if isinstance(r["properties"], dict) else {},
        }
        for r in relation_rows
    ]

    # Encode next_page_token only if we got a full page (more may exist)
    next_page_token = None
    if len(relations) >= limit:
        next_offset = offset + len(relations)
        token_payload = {"offset": next_offset, "relation": relation_filter}
        next_page_token = base64.urlsafe_b64encode(
            _json.dumps(token_payload).encode()
        ).decode()

    return entities, relations, next_page_token


def _dict_to_sparsevec(sparse_dict: dict[int, float]):
    """Convert {token_id: weight} dict to pgvector sparsevec text literal.

    pgvector sparsevec text format: '{idx1:val1,idx2:val2,...}/dimensions'.
    BGE-m3 XLM-R vocab = 250k tokens. The asyncpg type codec registered in
    deps._init_pg_connection binds this string directly as sparsevec type.
    """
    BGE_M3_VOCAB_DIM = 250002
    sorted_items = sorted(sparse_dict.items())
    inner = ",".join(f"{k}:{v}" for k, v in sorted_items if v != 0.0)
    return "{" + inner + "}/" + str(BGE_M3_VOCAB_DIM)