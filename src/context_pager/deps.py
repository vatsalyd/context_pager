from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import asyncpg
from pgvector.asyncpg import register_vector
import redis.asyncio as redis
from FlagEmbedding import BGEM3FlagModel
from llmlingua import PromptCompressor
from ollama import AsyncClient as OllamaClient

from context_pager.config import settings


class Dependencies:
    """Lazy-initialized singleton dependencies for the MCP server."""

    _pg_pool: Optional[asyncpg.Pool] = None
    _redis: Optional[redis.Redis] = None
    _embedder: Optional[BGEM3FlagModel] = None
    _compressor: Optional[PromptCompressor] = None
    _ollama: Optional[OllamaClient] = None
    _init_lock = asyncio.Lock()

    @classmethod
    async def pg_pool(cls) -> asyncpg.Pool:
        if cls._pg_pool is None:
            async with cls._init_lock:
                if cls._pg_pool is None:
                    cls._pg_pool = await asyncpg.create_pool(
                        settings.database_url,
                        min_size=2,
                        max_size=10,
                        init=cls._init_pg_connection,
                    )
        return cls._pg_pool

    @staticmethod
    async def _init_pg_connection(conn: asyncpg.Connection) -> None:
        await register_vector(conn)
        await conn.execute("SET default_transaction_isolation = 'read committed'")

    @classmethod
    async def redis(cls) -> redis.Redis:
        if cls._redis is None:
            async with cls._init_lock:
                if cls._redis is None:
                    cls._redis = redis.from_url(
                        settings.redis_url,
                        encoding="utf-8",
                        decode_responses=True,
                        max_connections=20,
                    )
        return cls._redis

    @classmethod
    def embedder(cls) -> BGEM3FlagModel:
        if cls._embedder is None:
            with cls._init_lock:
                if cls._embedder is None:
                    cls._embedder = BGEM3FlagModel(
                        settings.embedding_model,
                        use_fp16=False,  # ARM CPU
                    )
        return cls._embedder

    @classmethod
    def compressor(cls) -> PromptCompressor:
        if cls._compressor is None:
            with cls._init_lock:
                if cls._compressor is None:
                    cls._compressor = PromptCompressor(
                        model_name=settings.llmlingua_model,
                        use_llmlingua2=True,
                    )
        return cls._compressor

    @classmethod
    def ollama(cls) -> OllamaClient:
        if cls._ollama is None:
            with cls._init_lock:
                if cls._ollama is None:
                    cls._ollama = OllamaClient(host=settings.ollama_url)
        return cls._ollama

    @classmethod
    async def close(cls) -> None:
        if cls._pg_pool:
            await cls._pg_pool.close()
            cls._pg_pool = None
        if cls._redis:
            await cls._redis.close()
            cls._redis = None


@asynccontextmanager
async def lifespan() -> AsyncGenerator[None, None]:
    """Application lifespan - initialize on startup, cleanup on shutdown."""
    try:
        # Warm up connections
        await Dependencies.pg_pool()
        await Dependencies.redis()
        _ = Dependencies.embedder()
        _ = Dependencies.compressor()
        if settings.ollama_enabled:
            _ = Dependencies.ollama()
        yield
    finally:
        await Dependencies.close()