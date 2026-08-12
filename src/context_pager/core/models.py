from __future__ import annotations

import asyncio

from context_pager.config import get_bridge_settings
from context_pager.core.compression import Compressor, build_compressor
from context_pager.core.embedder import Embedder, build_embedder


class Models:
    """Lazy singletons for the heavy models. Nothing loads until first use."""

    _embedder: Embedder | None = None
    _compressor: Compressor | None = None
    _lock = asyncio.Lock()
    # Q34: CPU-bound model invocations serialize so the laptop never runs two
    # model jobs at once, while async I/O (search, storage) interleaves freely.
    call_lock = asyncio.Lock()

    def __init__(self, settings=None):
        self.settings = settings or get_bridge_settings()

    async def embedder(self) -> Embedder:
        if self._embedder is None:
            async with self._lock:
                if self._embedder is None:
                    self._embedder = build_embedder(self.settings)
        return self._embedder

    async def compressor(self) -> Compressor:
        if self._compressor is None:
            async with self._lock:
                if self._compressor is None:
                    self._compressor = build_compressor(self.settings)
        return self._compressor

    async def redact(self, text: str) -> tuple[str, dict[str, int]]:
        """PII masking. Lives on the models object so tests can stub it out."""
        from context_pager.core.pii import redact_text

        return await redact_text(text)

    async def preload(self) -> None:
        """Load all models at bridge startup so the first tool call is fast (Q18)."""
        await self.embedder()
        await self.compressor()


_models: Models | None = None


def get_models(settings=None) -> Models:
    global _models
    if _models is None:
        _models = Models(settings)
    return _models


async def redact(text: str) -> tuple[str, dict[str, int]]:
    """PII masking entry point (Presidio, both lite and full mode)."""
    from context_pager.core.pii import redact_text

    return await redact_text(text)
