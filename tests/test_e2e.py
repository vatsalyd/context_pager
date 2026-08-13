from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from context_pager.bridge.client import BridgeClient
from context_pager.config import BridgeSettings, RelaySettings
from context_pager.core.tools import add_document
from context_pager.relay.connections import ConnectionManager
from context_pager.relay.db import RelayDB
from context_pager.relay.server import build_app
from tests.fakes import FakeModels

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "q3_strategy_review.txt"


def _secrets_hex() -> str:
    import secrets

    return secrets.token_hex(16)


async def _start_relay(settings: RelaySettings, db: RelayDB, manager: ConnectionManager) -> tuple[uvicorn.Server, asyncio.Task, int]:
    app = build_app(settings, db, manager)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, port


async def _signup(port: int) -> dict:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.post("/v1/signup")
    return r.json()


async def _wait_bridge(manager: ConnectionManager, user_id: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while manager.pick(user_id) is None:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("bridge never connected")
        await asyncio.sleep(0.05)


async def _call(session: ClientSession, name: str, params: dict) -> dict:
    res = await session.call_tool(name, params)
    text = res.content[0].text
    return json.loads(text)


async def test_full_stack_roundtrip(tmp_path):
    """Real relay + real bridge (fake models) + real MCP client over HTTP+WSS."""
    relay_settings = RelaySettings(
        PAGER_SQLITE_DB=str(tmp_path / "relay.db"),
        PAGER_RELAY_HOST="127.0.0.1",
        PAGER_RELAY_PORT=0,
        PAGER_RATE_LIMIT_CALLS_PER_HOUR=1000,
    )
    db = RelayDB(relay_settings.sqlite_db)
    manager = ConnectionManager(max_per_user=2)

    server, server_task, port = await _start_relay(relay_settings, db, manager)

    bridge_settings = BridgeSettings(
        PAGER_ROOT=str(tmp_path / "bridge_docs"),
        PAGER_DB=str(tmp_path / "bridge.db"),
        PAGER_TELEMETRY_DB=str(tmp_path / "bridge_tel.db"),
        PAGER_CHUNK_TOKENS=512,
        PAGER_MAX_RETURN_TOKENS=2048,
        PAGER_BRIDGE_KEY="",
    )
    bridge_models = FakeModels()

    try:
        keys = await _signup(port)
        agent_key, bridge_key = keys["agent_key"], keys["bridge_key"]
        user_id = keys["user_id"]

        await add_document(str(FIXTURE), settings=bridge_settings, models=bridge_models)

        bridge_settings.relay_ws_url = f"ws://127.0.0.1:{port}/bridge"
        bridge_settings.bridge_key = bridge_key
        asyncio.create_task(BridgeClient(bridge_settings, bridge_models).run())
        await _wait_bridge(manager, user_id)

        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {agent_key}"}) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    assert {"compress_document", "search_documents", "commit_to_long_term_memory"} <= names

                    search = await _call(session, "search_documents", {"query": "revenue targets", "top_k": 3})
                    assert search["tool"] == "search_documents"
                    assert search["results"], "expected at least one result"
                    best = search["results"][0]

                    compressed = await _call(
                        session, "compress_document", {"doc_id": best["doc_id"], "page": best["best_page"]}
                    )
                    assert compressed["tool"] == "compress_document"
                    assert compressed["doc_id"] == best["doc_id"]
                    assert compressed["next_page"] is None or compressed["next_page"] >= 1

                    committed = await _call(
                        session,
                        "commit_to_long_term_memory",
                        {"key": "q3_revenue", "insights": "Q3 revenue target is 42M."},
                    )
                    assert committed["status"] == "persisted"

                    # memory is silently recalled on a later search
                    recalled = await _call(session, "search_documents", {"query": "42M revenue target"})
                    assert any("q3_revenue" in i for i in recalled["recalled_insights"])

        usage = db.usage(user_id)
        assert usage and usage[0]["calls"] >= 4
        assert usage[0]["tokens_in"] > 0
        assert usage[0]["cost_saved_usd"] >= 0
    finally:
        server.should_exit = True
        await server_task
