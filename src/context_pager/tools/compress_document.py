from __future__ import annotations

import json
import time

from context_pager.db import acquire_conn
from context_pager.contextvar import tenant_id_var
from context_pager.knowledge.retriever import retrieve_documents_rrf
from context_pager.cache.lru import get_cached_compression, set_cached_compression, touch_active_page
from context_pager.telemetry.audit import log_audit_event
from context_pager.telemetry.cost import calculate_savings


async def compress_document(
    doc_id: str,
    focus_area: str | None = None,
    max_return_tokens: int = 2048,
) -> str:
    """Fetch a compressed page of a document."""
    start_time = time.time()
    tenant_id = tenant_id_var.get()

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
    async with acquire_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT content FROM documents WHERE id = $1 AND tenant_id = current_setting('app.tenant_id', true)",
            doc_id,
        )
        if not row:
            return _error_envelope("compress_document", f"Document not found: {doc_id}")
        raw_text = row["content"]

    # Enforce token budget
    from context_pager.rate_limit.middleware import check_token_limit
    from context_pager.compression.pipeline import count_tokens
    if not await check_token_limit(tenant_id, count_tokens(raw_text)):
        return _error_envelope("compress_document", "Token budget exceeded: daily compression limit reached")

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

    result.metadata.cost_saved_usd = calculate_savings(
        result.metadata.original_tokens,
        result.metadata.compressed_tokens,
    )

    # Cache
    await set_cached_compression(tenant_id, doc_id, focus_area, max_return_tokens, result)

    # Track active page
    await touch_active_page(tenant_id, "default", doc_id, 1)

    await log_audit_event(
        tenant_id=tenant_id,
        event_type="tool_call",
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