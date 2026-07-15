from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from FlagEmbedding import BGEM3FlagModel

from context_pager.config import settings


class BGEEmbedder:
    def __init__(self):
        self._model: BGEM3FlagModel | None = None

    @property
    def model(self) -> BGEM3FlagModel:
        if self._model is None:
            self._model = BGEM3FlagModel(
                settings.embedding_model,
                use_fp16=False,  # ARM CPU
            )
        return self._model

    async def embed_dense(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.model.encode(
                texts,
                batch_size=settings.embedding_batch_size,
                max_length=settings.embedding_max_length,
            )["dense_vecs"].tolist(),
        )

    async def embed_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.model.encode(
                texts,
                return_dense=False,
                return_sparse=True,
                return_colbert_vecs=False,
            )["lexical_weights"],
        )

    async def embed_multi(self, texts: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: (
                self.model.encode(
                    texts,
                    batch_size=settings.embedding_batch_size,
                    max_length=settings.embedding_max_length,
                    return_dense=True,
                    return_sparse=True,
                    return_colbert_vecs=False,
                )["dense_vecs"].tolist(),
                self.model.encode(
                    texts,
                    return_dense=False,
                    return_sparse=True,
                    return_colbert_vecs=False,
                )["lexical_weights"],
            ),
        )


@lru_cache(maxsize=1)
def get_embedder() -> BGEEmbedder:
    return BGEEmbedder()