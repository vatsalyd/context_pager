from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from context_pager.core import cost
from context_pager.core.chunker import chunk_text
from context_pager.core.compression import count_tokens
from context_pager.core.models import get_models
from context_pager.core.storage import Library, expand, open_library
from context_pager.envelopes import commit_envelope, compress_envelope, error_envelope, search_envelope

SUPPORTED_SUFFIXES = {
    ".txt": "text",
    ".md": "markdown",
    ".mdx": "markdown",
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".tsx": "code",
    ".jsx": "code",
    ".go": "code",
    ".rs": "code",
    ".sql": "code",
    ".sh": "code",
    ".json": "code",
    ".yaml": "code",
    ".yml": "code",
    ".toml": "code",
    ".java": "code",
    ".c": "code",
    ".cpp": "code",
    ".h": "code",
    ".rb": "code",
    ".php": "code",
}

SNIPPET_CHARS = 200


class _PageCache:
    """In-process LRU for compressed pages (Q25). Evicted on reindex/remove, dies with process."""

    def __init__(self, max_bytes: int = 64 * 1024 * 1024):
        self._data: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._bytes = 0
        self._max = max_bytes

    def get(self, doc_id: str, page: int, max_tokens: int) -> dict[str, Any] | None:
        key = (doc_id, page, max_tokens)
        env = self._data.get(key)
        if env is None:
            return None
        del self._data[key]
        self._data[key] = env  # move to MRU end
        return env

    def put(self, doc_id: str, page: int, max_tokens: int, env: dict[str, Any]) -> None:
        key = (doc_id, page, max_tokens)
        size = len(json.dumps(env))
        old = self._data.pop(key, None)
        if old is not None:
            self._bytes -= len(json.dumps(old))
        self._data[key] = env
        self._bytes += size
        while self._bytes > self._max and self._data:
            _, oldest = self._data.pop(next(iter(self._data)))
            self._bytes -= len(json.dumps(oldest))

    def evict_doc(self, doc_id: str) -> None:
        for key in [k for k in self._data if k[0] == doc_id]:
            self._bytes -= len(json.dumps(self._data.pop(key)))


_page_cache = _PageCache()


# ── library lifecycle (CLI) ──────────────────────────────────

async def add_document(file: str, kind: str = "unstructured", settings=None, models=None) -> str:
    """Copy a text/markdown/code file into the library, chunk, embed, index."""
    from context_pager.config import get_bridge_settings

    settings = settings or get_bridge_settings()
    models = models or get_models(settings)
    src = Path(file)
    if not src.is_file():
        raise FileNotFoundError(f"file not found: {file}")
    if src.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"unsupported file type: {src.suffix} — supported: text/markdown/code "
            "(convert PDFs/DOCX to text first)"
        )

    text = _read_text(src)
    chunks = chunk_text(text, settings.chunk_tokens)
    embedder = await models.embedder()
    embeddings = await embedder.embed_dense(chunks)

    root = expand(settings.root_dir)
    root.mkdir(parents=True, exist_ok=True)
    doc_id = _new_doc_id()
    copy_path = root / f"{doc_id}{src.suffix.lower()}"
    copy_path.write_bytes(src.read_bytes())

    with open_library(settings) as lib:
        lib.add_document(src.stem, str(copy_path), kind, chunks, embeddings, doc_id=doc_id)
    return doc_id


async def reindex_document(doc_id: str, settings=None, models=None) -> None:
    """Re-chunk + re-embed a doc from its copy, keeping the same doc_id."""
    from context_pager.config import get_bridge_settings

    settings = settings or get_bridge_settings()
    models = models or get_models(settings)
    with open_library(settings) as lib:
        doc = lib.get_document(doc_id)
        if doc is None:
            raise KeyError(f"document not found: {doc_id}")
        text = _read_text(Path(doc["path"]))
    chunks = chunk_text(text, settings.chunk_tokens)
    embedder = await models.embedder()
    embeddings = await embedder.embed_dense(chunks)
    with open_library(settings) as lib:
        lib.replace_chunks(doc_id, chunks, embeddings)
    _page_cache.evict_doc(doc_id)


def remove_document(doc_id: str, settings=None) -> None:
    from context_pager.config import get_bridge_settings

    settings = settings or get_bridge_settings()
    with open_library(settings) as lib:
        doc = lib.get_document(doc_id)
        if doc is None:
            raise KeyError(f"document not found: {doc_id}")
        lib.remove_document(doc_id)
    Path(doc["path"]).unlink(missing_ok=True)
    _page_cache.evict_doc(doc_id)


# ── MCP tools ────────────────────────────────────────────────

async def search_documents(query: str, top_k: int = 10, settings=None, models=None) -> str:
    """Search the library. Returns ranked docs + silent memory recall (Q5/Q10)."""
    from context_pager.config import get_bridge_settings

    start = time.time()
    settings = settings or get_bridge_settings()
    models = models or get_models(settings)
    embedder = await models.embedder()

    async with models.call_lock:
        query_emb = (await embedder.embed_dense([query]))[0]

    with open_library(settings) as lib:
        hits = lib.search_chunks(query_emb, top_k)
        results = []
        for hit in hits:
            doc = lib.get_document(hit["doc_id"])
            if doc is None:
                continue
            results.append({
                "doc_id": hit["doc_id"],
                "title": doc["title"],
                "snippet": hit["text"][:SNIPPET_CHARS],
                "best_page": _chunk_idx_to_page(hit["chunk_idx"], lib.chunks_per_page),
                "score": hit["score"],
            })
        recalled = lib.recall_memory(query_emb)
    recalled_insights = [f"Recalled insight: {r['key']}: {r['insights']}" for r in recalled]

    metadata = {
        "results_returned": len(results),
        "elapsed_ms": int((time.time() - start) * 1000),
    }
    return search_envelope(query, results, recalled_insights, metadata)


async def compress_document(
    doc_id: str,
    page: int = 1,
    focus_area: str | None = None,
    max_return_tokens: int | None = None,
    settings=None,
    models=None,
) -> str:
    """Read one compressed page of a document (Q3/Q4/Q6/Q13)."""
    from context_pager.config import get_bridge_settings

    start = time.time()
    settings = settings or get_bridge_settings()
    models = models or get_models(settings)
    max_return_tokens = max_return_tokens or settings.max_return_tokens

    if page < 1:
        return error_envelope("compress_document", f"page must be >= 1, got {page}")
    if max_return_tokens < 1:
        return error_envelope("compress_document", f"max_return_tokens must be >= 1, got {max_return_tokens}")

    with open_library(settings) as lib:
        doc = lib.get_document(doc_id)
        if doc is None:
            return error_envelope("compress_document", f"document not found: {doc_id}")
        pages_total = lib.pages_total(doc_id)
        if page > pages_total:
            return error_envelope(
                "compress_document", f"page {page} out of range (doc has {pages_total} pages)"
            )
        path = Path(doc["path"])
        if not path.is_file():
            return error_envelope(
                "compress_document", f"source file missing — run `pager docs reindex {doc_id}`"
            )

        # Focus re-ranking (Q6): order pages by best-chunk similarity to focus.
        page_order = None
        if focus_area:
            page_order = await _focus_page_order(lib, models, doc_id, focus_area)
            pages_total = len(page_order)

        # Resolve the requested page's chunk window.
        if page_order is None:
            page_chunks = lib.page_chunks(doc_id, page)
            focused = False
        else:
            target_page = page_order[page - 1]
            page_chunks = lib.page_chunks(doc_id, target_page)
            focused = True

        # Cache key uses the *resolved* content page so focus/sequential agree.
        cache_key_page = page if page_order is None else page_order[page - 1]
        cached = _page_cache.get(doc_id, cache_key_page, max_return_tokens)
        if cached:
            cached["metadata"]["cache_hit"] = True
            cached["metadata"]["elapsed_ms"] = int((time.time() - start) * 1000)
            return json.dumps(cached)

        raw_text = "\n\n".join(page_chunks)
        original_tokens = count_tokens(raw_text)

        # Short-circuit for small pages (Q13).
        if original_tokens <= max_return_tokens:
            redacted, pii_counts = await models.redact(raw_text)
            compressed = redacted
            ratio = "1.0x"
            skipped = True
        else:
            redacted, pii_counts = await models.redact(raw_text)
            compressor = await models.compressor()
            async with models.call_lock:
                compressed = await compressor.compress(redacted, max_return_tokens)
            final, pii2 = await models.redact(compressed)
            for k, v in pii2.items():
                pii_counts[k] = pii_counts.get(k, 0) + v
            compressed = final
            ratio = f"{original_tokens / max(count_tokens(compressed), 1):.1f}x"
            skipped = False

        compressed_tokens = count_tokens(compressed)
        metadata = {
            "original_tokens": original_tokens,
            "compressed_tokens": compressed_tokens,
            "compression_ratio": ratio,
            "cost_saved_usd": round(cost.calculate_savings(original_tokens, compressed_tokens), 4),
            "pii_redacted": pii_counts,
            "cache_hit": False,
            "skipped_compression": skipped,
            "elapsed_ms": int((time.time() - start) * 1000),
        }
        if focused:
            metadata["focus_applied"] = True

        env = json.loads(
            compress_envelope(
                doc_id=doc_id,
                page=page,
                pages_total=pages_total,
                content=compressed,
                token_count=compressed_tokens,
                next_page=page + 1 if page < pages_total else None,
                metadata=metadata,
            )
        )
        _page_cache.put(doc_id, cache_key_page, max_return_tokens, env)
        return json.dumps(env)


async def commit_to_long_term_memory(key: str, insights: str, settings=None, models=None) -> str:
    """Persist an insight for silent recall on future searches (Q10)."""
    from context_pager.config import get_bridge_settings

    start = time.time()
    settings = settings or get_bridge_settings()
    models = models or get_models(settings)
    embedder = await models.embedder()
    async with models.call_lock:
        emb = (await embedder.embed_dense([insights]))[0]
    with open_library(settings) as lib:
        lib.upsert_memory(key, insights, emb)
    metadata = {"elapsed_ms": int((time.time() - start) * 1000)}
    return commit_envelope(key, metadata)


async def _focus_page_order(lib: Library, models, doc_id: str, focus_area: str) -> list[int]:
    """Return page numbers (1-indexed) sorted by best chunk similarity to focus."""
    import numpy as np

    embedder = await models.embedder()
    async with models.call_lock:
        focus_emb = np.array((await embedder.embed_dense([focus_area]))[0], dtype=np.float32)
    focus_emb = focus_emb / max(np.linalg.norm(focus_emb), 1e-9)

    scores: dict[int, float] = {}
    for page in range(1, lib.pages_total(doc_id) + 1):
        chunks = lib.page_chunks(doc_id, page)
        emb = await embedder.embed_dense(chunks)
        emb_np = np.array(emb, dtype=np.float32)
        norms = np.maximum(np.linalg.norm(emb_np, axis=1, keepdims=True), 1e-9)
        emb_np = emb_np / norms
        sims = (emb_np @ focus_emb).tolist()
        scores[page] = max(sims) if sims else 0.0
    return sorted(scores, key=lambda p: scores[p], reverse=True)


def _chunk_idx_to_page(chunk_idx: int, chunks_per_page: int) -> int:
    return chunk_idx // chunks_per_page + 1


def _new_doc_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"cannot decode {path.name} as UTF-8 text — convert to plain text first"
        ) from e
