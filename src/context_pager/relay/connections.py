from __future__ import annotations

import asyncio
import logging

from context_pager.protocol import RpcError, RpcRequest, RpcResponse

log = logging.getLogger("context_pager.relay.connections")


class BridgeConnection:
    """One live bridge WSS channel. The reader loop resolves pending RPC futures by id."""

    def __init__(self, websocket, user_id: str):
        self.ws = websocket
        self.user_id = user_id
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[RpcResponse]] = {}
        self._next_id = 0

    async def call(self, method: str, params: dict, timeout: float = 30.0) -> RpcResponse:
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[RpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            async with self._write_lock:
                await self.ws.send_json(RpcRequest(id=req_id, method=method, params=params).model_dump())
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                return RpcResponse(id=req_id, error=RpcError(code=-32000, message="bridge timed out"))
        finally:
            self._pending.pop(req_id, None)

    def handle_message(self, raw: str) -> None:
        try:
            resp = RpcResponse.model_validate_json(raw)
        except Exception:
            log.warning("dropping malformed message from bridge")
            return
        fut = self._pending.pop(resp.id, None)
        if fut is not None and not fut.done():
            fut.set_result(resp)


class ConnectionManager:
    """Registry of connected bridges, keyed by user_id. Max N bridges per user (Q15)."""

    def __init__(self, max_per_user: int = 2):
        self._conns: dict[str, list[BridgeConnection]] = {}
        self._max_per_user = max_per_user

    def register(self, conn: BridgeConnection) -> None:
        self._conns.setdefault(conn.user_id, []).append(conn)

    def unregister(self, conn: BridgeConnection) -> None:
        lst = self._conns.get(conn.user_id, [])
        if conn in lst:
            lst.remove(conn)
        if not lst:
            self._conns.pop(conn.user_id, None)

    def count_for(self, user_id: str) -> int:
        return len(self._conns.get(user_id, []))

    def pick(self, user_id: str) -> BridgeConnection | None:
        lst = self._conns.get(user_id, [])
        return lst[0] if lst else None
