from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RelaySettings(BaseSettings):
    """Settings for the AWS relay (thin FastMCP router). Loaded by `pager serve`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = Field(default="0.0.0.0", alias="PAGER_RELAY_HOST")
    port: int = Field(default=8000, alias="PAGER_RELAY_PORT")
    mcp_path: str = Field(default="/mcp", alias="PAGER_MCP_PATH")
    bridge_path: str = Field(default="/bridge", alias="PAGER_BRIDGE_PATH")

    sqlite_db: str = Field(default="users.db", alias="PAGER_SQLITE_DB")
    public_url: str = Field(default="https://context-pager.duckdns.org", alias="PAGER_PUBLIC_URL")

    api_key_prefix: str = Field(default="pgr_", alias="PAGER_API_KEY_PREFIX")
    rate_limit_calls_per_hour: int = Field(default=100, alias="PAGER_RATE_LIMIT_CALLS_PER_HOUR")
    max_bridges_per_key: int = Field(default=2, alias="PAGER_MAX_BRIDGES_PER_KEY")
    signup_per_ip_per_day: int = Field(default=5, alias="PAGER_SIGNUP_PER_IP_PER_DAY")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


class BridgeSettings(BaseSettings):
    """Settings for the laptop bridge daemon. Loaded by `pager bridge`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    root_dir: str = Field(default="~/.pager/docs", alias="PAGER_ROOT")
    db_path: str = Field(default="~/.pager/pager.db", alias="PAGER_DB")
    telemetry_db: str = Field(default="~/.pager/telemetry.db", alias="PAGER_TELEMETRY_DB")

    bridge_key: str = Field(default="", alias="PAGER_BRIDGE_KEY")
    relay_ws_url: str = Field(default="wss://context-pager.duckdns.org/bridge", alias="PAGER_BRIDGE_WS_URL")

    lite: bool = Field(default=False, alias="PAGER_LITE")

    local_mcp_host: str = Field(default="127.0.0.1", alias="PAGER_LOCAL_MCP_HOST")
    local_mcp_port: int = Field(default=8000, alias="PAGER_LOCAL_MCP_PORT")

    embedding_model: str = Field(default="BAAI/bge-m3", alias="PAGER_EMBEDDING_MODEL")
    llmlingua_model: str = Field(
        default="microsoft/llmlingua-2-xlm-roberta-large-meetingbank",
        alias="PAGER_LLMLINGUA_MODEL",
    )
    chunk_tokens: int = Field(default=512, alias="PAGER_CHUNK_TOKENS")
    max_return_tokens: int = Field(default=2048, alias="PAGER_MAX_RETURN_TOKENS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_relay_settings() -> RelaySettings:
    return RelaySettings()


@lru_cache
def get_bridge_settings() -> BridgeSettings:
    return BridgeSettings()
