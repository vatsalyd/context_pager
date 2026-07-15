from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = Field(default="postgresql://pager:password@localhost:5432/pager", alias="DATABASE_URL")
    pg_password: str = Field(default="password", alias="PG_PASSWORD")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # Ollama
    ollama_url: str = Field(default="http://localhost:11434", alias="OLLAMA_URL")
    ollama_enabled: bool = Field(default=False, alias="OLLAMA_ENABLED")
    ollama_model: str = Field(default="llama3:8b-q4_K_M", alias="OLLAMA_MODEL")

    # Embeddings
    embedding_model: str = Field(default="BAAI/bge-m3", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=1024, alias="EMBEDDING_DIM")
    embedding_batch_size: int = Field(default=12, alias="EMBEDDING_BATCH_SIZE")
    embedding_max_length: int = Field(default=8192, alias="EMBEDDING_MAX_LENGTH")

    # Compression
    llmlingua_model: str = Field(default="microsoft/llmlingua-2-xlm-roberta-large-meetingbank", alias="LLMLINGUA_MODEL")
    default_max_return_tokens: int = Field(default=2048, alias="DEFAULT_MAX_RETURN_TOKENS")

    # Auth
    secret_key: str = Field(default="dev-secret-change-me", alias="SECRET_KEY")
    api_key_prefix: str = Field(default="pgr_", alias="API_KEY_PREFIX")

    # Rate Limits (free tier)
    rate_limit_tool_calls_per_hour: int = Field(default=100, alias="RATE_LIMIT_TOOL_CALLS_PER_HOUR")
    rate_limit_tokens_per_day: int = Field(default=500_000, alias="RATE_LIMIT_TOKENS_PER_DAY")
    rate_limit_max_docs: int = Field(default=100, alias="RATE_LIMIT_MAX_DOCS")
    rate_limit_max_sessions: int = Field(default=2, alias="RATE_LIMIT_MAX_SESSIONS")

    # Observability
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO", alias="LOG_LEVEL")

    # Hosting
    public_url: str = Field(default="https://pager.duckdns.org", alias="PUBLIC_URL")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()