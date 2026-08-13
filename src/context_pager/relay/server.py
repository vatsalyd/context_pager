from __future__ import annotations

import logging
import secrets
import uuid

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from context_pager.config import RelaySettings, get_relay_settings
from context_pager.protocol import AuthMessage, AuthResult
from context_pager.relay.connections import BridgeConnection, ConnectionManager
from context_pager.relay.db import RelayDB, sha256
from context_pager.relay.tools import register_relay_tools

log = logging.getLogger("context_pager.relay")


async def bridge_endpoint(ws: WebSocket, settings: RelaySettings, db: RelayDB, manager: ConnectionManager) -> None:
    """Bridge WSS handshake + serve loop. Authenticates the bridge key (Q16), then
    relays agent tool calls by routing RPC requests over this channel."""
    await ws.accept()
    conn: BridgeConnection | None = None
    try:
        raw = await ws.receive_text()
        try:
            msg = AuthMessage.model_validate_json(raw)
        except Exception:
            await ws.send_json(AuthResult(ok=False, error="malformed auth message").model_dump())
            await ws.close()
            return
        key_hash = sha256(str(msg.params.get("bridge_key", "")))
        user = db.user_by_bridge_hash(key_hash)
        if user is None:
            await ws.send_json(AuthResult(ok=False, error="invalid bridge key").model_dump())
            await ws.close()
            return
        if manager.count_for(user["user_id"]) >= settings.max_bridges_per_key:
            await ws.send_json(AuthResult(ok=False, error="too many bridges for this key").model_dump())
            await ws.close()
            return

        conn = BridgeConnection(ws, user["user_id"])
        manager.register(conn)
        await ws.send_json(AuthResult(ok=True, user_id=user["user_id"]).model_dump())
        log.info("bridge connected: user=%s", user["user_id"])
        async for raw in ws.iter_text():
            conn.handle_message(raw)
    except WebSocketDisconnect:
        pass
    finally:
        if conn is not None:
            manager.unregister(conn)


async def signup(request: Request, settings: RelaySettings, db: RelayDB) -> JSONResponse:
    """POST /v1/signup — issue one agent key + one bridge key, shown once (Q16/Q27)."""
    ip = request.client.host if request.client else "unknown"
    if db.signup_count(ip) >= settings.signup_per_ip_per_day:
        return JSONResponse({"error": "signup limit reached for this IP today"}, status_code=429)

    user_id = uuid.uuid4().hex[:16]
    agent_key = f"{settings.api_key_prefix}agent_" + secrets.token_hex(16)
    bridge_key = f"{settings.api_key_prefix}bridge_" + secrets.token_hex(16)
    db.create_user(user_id, sha256(agent_key), sha256(bridge_key), ip)
    db.bump_signup(ip)
    return JSONResponse({
        "user_id": user_id,
        "agent_key": agent_key,
        "bridge_key": bridge_key,
        "note": "Store these now — keys are shown once and stored only as hashes.",
    })


def build_app(
    settings: RelaySettings | None = None,
    db: RelayDB | None = None,
    manager: ConnectionManager | None = None,
):
    """Single starlette app: FastMCP at /mcp, bridge WSS at /bridge, signup at /v1/signup (Q21)."""
    settings = settings or get_relay_settings()
    db = db or RelayDB(settings.sqlite_db)
    manager = manager or ConnectionManager(settings.max_bridges_per_key)

    mcp = FastMCP("context-pager-relay")
    register_relay_tools(mcp, settings, db, manager)

    app = mcp.http_app(path=settings.mcp_path, host_origin_protection=False)

    async def ws_route(ws: WebSocket) -> None:
        await bridge_endpoint(ws, settings, db, manager)

    async def signup_route(request: Request) -> JSONResponse:
        return await signup(request, settings, db)

    app.router.add_websocket_route(settings.bridge_path, ws_route)
    app.add_route("/v1/signup", signup_route, methods=["POST"])
    return app


def run_relay() -> None:
    logging.basicConfig(
        level=get_relay_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import uvicorn

    settings = get_relay_settings()
    app = build_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
