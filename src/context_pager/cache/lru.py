from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from typing import Optional

import redis.asyncio as redis

from context_pager.deps import Dependencies
from context_pager.compression.pipeline import CompressedResult, CompressionMetadata, Page


async def get_cached_compression(
    tenant_id: str,
    doc_id: str,
    focus_area: Optional[str],
    max_return_tokens: int,
) -> Optional[CompressedResult]:
    """Get cached compressed result."""
    focus_hash = hashlib.sha256((focus_area or "").encode()).hexdigest()[:16]
    key = f"pager:cache:{tenant_id}:{doc_id}:{focus_hash}:{max_return_tokens}"

    r = await Dependencies.redis()
    data = await r.get(key)
    if data:
        result = json.loads(data)
        # Reconstruct dataclass instances from plain dicts
        pages = [Page(**p) for p in result["pages"]]
        metadata = CompressionMetadata(**result["metadata"])
        metadata.cache_hit = True
        return CompressedResult(pages=pages, summary=result["summary"], metadata=metadata)
    return None


async def set_cached_compression(
    tenant_id: str,
    doc_id: str,
    focus_area: Optional[str],
    max_return_tokens: int,
    result: CompressedResult,
) -> None:
    """Cache compressed result (7-day TTL)."""
    focus_hash = hashlib.sha256((focus_area or "").encode()).hexdigest()[:16]
    key = f"pager:cache:{tenant_id}:{doc_id}:{focus_hash}:{max_return_tokens}"

    r = await Dependencies.redis()
    # dataclasses.asdict produces plain dicts that json can serialize
    data = {
        "pages": [asdict(p) for p in result.pages],
        "summary": result.summary,
        "metadata": asdict(result.metadata),
    }
    await r.set(key, json.dumps(data), ex=60 * 60 * 24 * 7)  # 7 days


async def touch_active_page(tenant_id: str, session_id: str, doc_id: str, page_id: int) -> None:
    """Track active page in a Sorted Set for decay tracking (Q7.2)."""
    r = await Dependencies.redis()
    member = f"{doc_id}:{page_id}"
    await r.zadd(f"pager:active:{tenant_id}:{session_id}", {member: time.time() * 1000})


async def get_cached_entities(tenant_id: str, cache_key: str) -> Optional[dict]:
    """Get cached entity graph result."""
    r = await Dependencies.redis()
    key = f"pager:cache:{tenant_id}:entity_graph:{cache_key}"
    data = await r.get(key)
    if data:
        return json.loads(data)
    return None


async def set_cached_entities(tenant_id: str, cache_key: str, envelope: dict) -> None:
    """Cache entity graph result (1-hour TTL — short, since entities can change)."""
    r = await Dependencies.redis()
    key = f"pager:cache:{tenant_id}:entity_graph:{cache_key}"
    await r.set(key, json.dumps(envelope), ex=60 * 60)  # 1 hour
