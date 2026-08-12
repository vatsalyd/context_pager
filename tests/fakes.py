from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class FakeEmbedder:
    """Deterministic dense embedder: a simple bag-of-words hashing.

    dim must match the real model family (1024 for BGE-M3) so the library's
    vec0 table dimension agrees between indexing and querying.
    """

    dim: int = 1024

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for i, ch in enumerate(text.lower()):
            vec[ord(ch) % self.dim] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class FakeCompressor:
    """Compressor that keeps the first target_tokens*4 chars."""

    async def compress(self, text: str, target_tokens: int) -> str:
        return text[: target_tokens * 4]


@dataclass
class FakeModels:
    settings = None
    call_lock = asyncio.Lock()
    embedder_: FakeEmbedder = None
    compressor_: FakeCompressor = None

    def __post_init__(self):
        if self.embedder_ is None:
            self.embedder_ = FakeEmbedder()
        if self.compressor_ is None:
            self.compressor_ = FakeCompressor()

    async def embedder(self):
        return self.embedder_

    async def compressor(self):
        return self.compressor_

    async def redact(self, text: str) -> tuple[str, dict]:
        """No-op PII in tests: Presidio + spaCy must not load in CI."""
        return text, {}
