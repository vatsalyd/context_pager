from __future__ import annotations

import hashlib
import json
from typing import Any

from context_pager.deps import Dependencies
from context_pager.knowledge.retriever import retrieve_documents_rrf
from context_pager.cache.lru import get_cached_compression, set_cached_compression
from context_pager.cache.decay import touch_active_page
from context_pager.telemetry.audit import log_audit_event
from context_pager.telemetry.cost import calculate_savings


async def compress_document(
    doc_id: str,
    focus_area: str | None = None,
    max_return_tokens: int = 2048,
) -> str:
    """Fetch a compressed page of a document."""
    from fastmcp import Context
    import time

    start_time = time.time()
    tenant_id = "default"  # Will be overridden by middleware

    # Check cache
    cached = await get_cached_compression(tenant_id, doc_id, focus_area, max_return_tokens)
    if cached:
        cached.metadata.cache_hit = True
        cached.metadata.elapsed_ms = int((time.time() - start_time) * 1000)
        return _build_envelope(
            tool="compress_document",
            doc_id=doc_id,
            pages=cached.pages,
            summary=cached.summary,
            metadata=cached.metadata,
        )

    # Load raw document
    pool = await Dependencies.pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content FROM documents WHERE id = $1 AND tenant_id = current_setting('app.tenant_id')",
            doc_id,
        )
        if not row:
            return _error_envelope("compress_document", f"Document not found: {doc_id}")

        raw_text = row["content"]

    # Determine if Ollama should be used
    from context_pager.config import settings
    use_ollama = settings.ollama_enabled and focus_area is not None

    # Compress
    from context_pager.compression.pipeline import compress_pipeline
    result = await compress_pipeline(
        raw_text=raw_text,
        focus_area=focus_area,
        max_return_tokens=max_return_tokens,
        use_ollama=use_ollama,
    )

    # Add cost savings
    from context_pager.telemetry.cost import calculate_savings
    result.metadata.cost_saved_usd = calculate_savings(
        result.metadata.original_tokens,
        result.metadata.compressed_tokens,
    )

    # Cache
    await set_cached_compression(tenant_id, doc_id, focus_area, max_return_tokens, result)

    # Track active page
    await touch_active_page("default", doc_id, 1)

    # Log audit
    await log_audit_event(
        tenant_id=tenant_id,
        tool_name="compress_document",
        doc_id=doc_id,
        original_tokens=result.metadata.original_tokens,
        compressed_tokens=result.metadata.compressed_tokens,
        cost_saved_usd=result.metadata.cost_saved_usd,
        metadata={
            "focus_area": focus_area,
            "max_return_tokens": max_return_tokens,
            "skipped_compression": result.metadata.skipped_compression,
            "pii_redacted": result.metadata.pii_redacted,
            "cache_hit": False,
            "elapsed_ms": int((time.time() - start_time) * 1000),
        },
    )

    return _build_envelope(
        tool="compress_document",
        doc_id=doc_id,
        pages=result.pages,
        summary=result.summary,
        metadata=result.metadata,
    )


def _build_envelope(
    tool: str,
    doc_id: str,
    pages: list,
    summary: str,
    metadata,
    predecessor: str | None = None,
) -> str:
    envelope = {
        "tool": tool,
        "doc_id": doc_id,
        "pages": [
            {"page": p.page, "content": p.content, "token_count": p.token_count}
            for p in pages
        ],
        "summary": summary,
        "predecessor": predecessor,
        "refs": [],
        "metadata": {
            "original_tokens": metadata.original_tokens,
            "compressed_tokens": metadata.compressed_tokens,
            "compression_ratio": metadata.compression_ratio,
            "cost_saved_usd": round(metadata.cost_saved_usd, 4),
            "pii_redacted": metadata.pii_redacted,
            "cache_hit": metadata.cache_hit,
            "skipped_compression": metadata.skipped_compression,
            "elapsed_ms": metadata.elapsed_ms,
        },
    }
    return json.dumps(envelope)


def _error_envelope(tool: str, message: str) -> str:
    return json.dumps({
        "tool": tool,
        "error": message,
        "metadata": {},
    })