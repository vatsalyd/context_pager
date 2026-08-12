from __future__ import annotations

import json
from typing import Any


def compress_envelope(
    doc_id: str,
    page: int,
    pages_total: int,
    content: str,
    token_count: int,
    next_page: int | None,
    metadata: dict[str, Any],
) -> str:
    """Envelope for `compress_document`. See CONTEXT.md for the contract."""
    return json.dumps({
        "tool": "compress_document",
        "doc_id": doc_id,
        "page": page,
        "pages_total": pages_total,
        "content": content,
        "token_count": token_count,
        "next_page": next_page,
        "metadata": metadata,
    })


def search_envelope(
    query: str,
    results: list[dict[str, Any]],
    recalled_insights: list[str],
    metadata: dict[str, Any],
) -> str:
    """Envelope for `search_documents`."""
    return json.dumps({
        "tool": "search_documents",
        "query": query,
        "results": results,
        "recalled_insights": recalled_insights,
        "metadata": metadata,
    })


def commit_envelope(key: str, metadata: dict[str, Any]) -> str:
    """Envelope for `commit_to_long_term_memory`."""
    return json.dumps({
        "tool": "commit_to_long_term_memory",
        "key": key,
        "status": "persisted",
        "metadata": metadata,
    })


def error_envelope(tool: str, error: str, retryable: bool = False, metadata: dict[str, Any] | None = None) -> str:
    """Uniform error envelope. `retryable` tells capable agents whether retrying may help."""
    return json.dumps({
        "tool": tool,
        "error": error,
        "metadata": {
            "retryable": retryable,
            **(metadata or {}),
        },
    })
