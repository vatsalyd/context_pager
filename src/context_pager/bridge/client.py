from __future__ import annotations

import asyncio
import json
import logging

import websockets

from context_pager.bridge import telemetry
from context_pager.config import BridgeSettings
from context_pager.core import tools
from context_pager.protocol import AuthMessage, AuthResult, RpcError, RpcRequest, RpcResponse

log = logging.getLogger("context_pager.bridge.client")

MIN_BACKOFF = 1
MAX_BACKOFF = 60


def _handlers(settings: BridgeSettings, models) -> dict[str, object]:
    """method name -> async callable(**params) -> envelope dict."""

    async def call_compress(**params: object) -> dict:
        params.pop("settings", None)
        params.pop("models", None)
        return json.loads(await tools.compress_document(settings=settings, models=models, **params))

    async def call_search(**params: object) -> dict:
        params.pop("settings", None)
        params.pop("models", None)
        return json.loads(await tools.search_documents(settings=settings, models=models, **params))

    async def call_commit(**params: object) -> dict:
        params.pop("settings", None)
        params.pop("models", None)
        return json.loads(await tools.commit_to_long_term_memory(settings=settings, models=models, **params))

    return {
        "compress_document": call_compress,
        "search_documents": call_search,
        "commit_to_long_term_memory": call_commit,
    }


class BridgeClient:
    """Outbound persistent WSS channel to the relay.

    The relay holds no outbound connections, so the bridge dials in, authenticates
    with its bridge key, and serves tool calls relayed from agents.
    """

    def __init__(self, settings: BridgeSettings, models):
        self._settings = settings
        self._models = models
        self._handlers = _handlers(settings, models)
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def run(self) -> None:
        if not self._settings.bridge_key:
            log.warning(
                "PAGER_BRIDGE_KEY not set — serving localhost MCP only. "
                "Set it after signing up to relay agent calls."
            )
            while True:
                await asyncio.sleep(3600)

        backoff = MIN_BACKOFF
        while True:
            try:
                await self._serve_connection()
                backoff = MIN_BACKOFF
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("relay connection error (%s); reconnecting in %ss", e, backoff)
            # Always pause between dials: a clean relay close must not busy-loop.
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _serve_connection(self) -> None:
        ws = await websockets.connect(self._settings.relay_ws_url, ping_interval=30, ping_timeout=60)
        self._ws = ws
        try:
            await self._authenticate(ws)
            async for raw in ws:
                try:
                    req = RpcRequest.model_validate_json(raw)
                except Exception:
                    log.warning("dropping malformed request from relay")
                    continue
                resp = await self._handle(req)
                await ws.send(resp.model_dump_json())
        finally:
            self._ws = None
            await ws.close()

    async def _authenticate(self, ws) -> None:
        await ws.send(AuthMessage(params={"bridge_key": self._settings.bridge_key}).model_dump_json())
        raw = await ws.recv()
        result = AuthResult.model_validate_json(raw)
        if not result.ok:
            raise RuntimeError(f"relay rejected auth: {result.error}")
        log.info("bridge authenticated (user_id=%s)", result.user_id)

    async def _handle(self, req: RpcRequest) -> RpcResponse:
        handler = self._handlers.get(req.method)
        if handler is None:
            return RpcResponse(id=req.id, error=RpcError(code=-32601, message=f"unknown method: {req.method}"))
        try:
            env = await handler(**req.params)  # type: ignore[operator]
            telemetry.record_call(req.method, env, self._settings)
            return RpcResponse(id=req.id, result=env)
        except Exception as e:
            log.exception("rpc %s failed", req.method)
            return RpcResponse(id=req.id, error=RpcError(code=-32000, message=str(e)))
