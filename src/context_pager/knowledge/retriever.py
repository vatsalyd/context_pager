from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from context_pager.deps import Dependencies
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

    pool = await Dependencies.pg_pool()
    async with pool.acquire() as conn:
        # Dense search
        dense_rows = await conn.fetch("""
            SELECT id, document_id, text, 1 - (embedding <=> $1) AS score
            FROM document_chunks
            WHERE tenant_id = current_setting('app.tenant_id')
            ORDER BY embedding <=> $1
            LIMIT $2
        """, q_dense[0], k * 20)

        # Sparse search - convert sparse weights to pgvector sparsevec format
        sparse_vec = _dict_to_sparsevec(q_sparse[0])
        sparse_rows = await conn.fetch("""
            SELECT id, document_id, text, 1 - (sparse_weights <=> $1) AS score
            FROM document_chunks
            WHERE tenant_id = current_setting('app.tenant_id')
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

    pool = await Dependencies.pg_pool()
    async with pool.acquire() as conn:
        # Entity dense search
        dense_rows = await conn.fetch("""
            SELECT e.id, e.name, e.type, e.properties, e.document_id,
                   1 - (e.embedding <=> $1) AS score
            FROM entities e
            WHERE e.tenant_id = current_setting('app.tenant_id')
            ORDER BY e.embedding <=> $1
            LIMIT $2
        """, q_dense[0], k * 2)

        # Entity sparse search (name-only sparse weights)
        sparse_vec = _dict_to_sparsevec(q_sparse[0])
        sparse_rows = await conn.fetch("""
            SELECT e.id, e.name, e.type, e.properties, e.document_id,
                   1 - (e.sparse_weights <=> $1) AS score
            FROM entities e
            WHERE e.tenant_id = current_setting('app.tenant_id')
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
    return [EntityHit(id=eid, fused_score=score, **entity_meta[eid]) for eid, score in ranked]


def _dict_to_sparsevec(sparse_dict: dict[int, float]) -> list[dict]:
    """Convert {token_id: weight} dict to pgvector sparsevec format."""
    # pgvector sparsevec expects list of {index: int, value: float} 
    # but asyncpg handles this via the sparsevec type
    return [{"index": int(k), "value": float(v)} for k, v in sparse_dict.items()]