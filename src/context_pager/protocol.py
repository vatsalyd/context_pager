from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AuthMessage(BaseModel):
    """Bridge -> relay handshake. The bridge key authenticates the WSS channel."""

    method: str = "auth"
    params: dict[str, Any] = Field(default_factory=dict)


class AuthResult(BaseModel):
    """Relay's reply to the handshake. `ok=true` registers the connection."""

    ok: bool
    user_id: str | None = None
    error: str | None = None


class RpcRequest(BaseModel):
    """A tool invocation forwarded relay -> bridge."""

    id: int | str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcError(BaseModel):
    code: int
    message: str


class RpcResponse(BaseModel):
    """Reply to an RpcRequest. Exactly one of result/error is set."""

    id: int | str
    result: dict[str, Any] | None = None
    error: RpcError | None = None
