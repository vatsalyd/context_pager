from __future__ import annotations

import asyncio
from typing import Protocol

from context_pager.config import get_bridge_settings


class Embedder(Protocol):
    """Dense embedding interface. Both real and fake embedders implement it."""

    @property
    def dim(self) -> int:
        ...

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        ...


class BGEM3Embedder:
    """Full mode: BGE-M3 dense embeddings. ~2.3 GB / ~4 GB RAM."""

    def __init__(self, model_name: str):
        from FlagEmbedding import BGEM3FlagModel

        self._model = BGEM3FlagModel(model_name, use_fp16=False)  # CPU
        self._dim = 1024

    @property
    def dim(self) -> int:
        return self._dim

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._model.encode(
                texts,
                batch_size=12,
                max_length=8192,
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
            )["dense_vecs"].tolist(),
        )


class BGESmallEmbedder:
    """Lite mode: bge-small-en-v1.5 dense embeddings. ~133 MB / ~200 MB RAM."""

    def __init__(self, model_name: str):
        from FlagEmbedding import FlagModel

        # bge retrieval models use an instruction prefix for queries.
        self._model = FlagModel(model_name)
        self._dim = 384
        self._query_prefix = "Represent this sentence for searching relevant passages: "

    @property
    def dim(self) -> int:
        return self._dim

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._model.encode(
                texts,
                batch_size=32,
                max_length=512,
            ).tolist(),
        )

    async def embed_query(self, query: str) -> list[float]:
        return (await self.embed_dense([self._query_prefix + query]))[0]


def build_embedder(settings=None) -> Embedder:
    """Build the embedder per lite/full mode. Settings injected for tests."""
    settings = settings or get_bridge_settings()
    if settings.lite:
        return BGESmallEmbedder("BAAI/bge-small-en-v1.5")
    return BGEM3Embedder(settings.embedding_model)


def dim_for(settings=None) -> int:
    """Embedding dimension without loading the model."""
    settings = settings or get_bridge_settings()
    return 384 if settings.lite else 1024
