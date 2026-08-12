from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import websockets

from context_pager.bridge.client import BridgeClient
from context_pager.config import BridgeSettings
from context_pager.core.tools import add_document
from tests.fakes import FakeModels

FIXTURE_TEXT = (
    "Acme Corp revenue grew 12% this quarter. The board approved a dividend of $2.10 per share. "
    "Our engineering team shipped the pagination feature ahead of schedule. "
) * 30


@pytest.fixture
def settings(tmp_path):
    return BridgeSettings(
        PAGER_ROOT=str(tmp_path / "docs"),
        PAGER_DB=str(tmp_path / "pager.db"),
        PAGER_TELEMETRY_DB=str(tmp_path / "telemetry.db"),
        PAGER_CHUNK_TOKENS=512,
        PAGER_MAX_RETURN_TOKENS=2048,
        PAGER_BRIDGE_KEY="test-bridge-key",
    )


@pytest.fixture
def models():
    return FakeModels()


def _write(tmp_path: Path) -> str:
    f = tmp_path / "fixture.txt"
    f.write_text(FIXTURE_TEXT, encoding="utf-8")
    return str(f)


async def _exchange(settings, models, requests: list[dict], fixture: str):
    """Run the bridge client against an in-process fake relay; return the relay's replies."""
    await add_document(fixture, settings=settings, models=models)
    seen: dict = {}

    async def relay_handler(ws):
        await ws.recv()  # auth message
        await ws.send(json.dumps({"ok": True, "user_id": "user-1"}))
        for req in requests:
            await ws.send(json.dumps(req))
            seen[req["id"]] = json.loads(await ws.recv())
        await ws.close()

    server = await websockets.serve(relay_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings.relay_ws_url = f"ws://127.0.0.1:{port}/bridge"

    client = BridgeClient(settings, models)
    task = asyncio.create_task(client.run())
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while len(seen) < len(requests):
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"only got {seen!r}")
            await asyncio.sleep(0.05)
        return seen
    finally:
        task.cancel()
        server.close()
        await server.wait_closed()


async def test_bridge_auth_and_search(settings, models, tmp_path):
    seen = await _exchange(
        settings,
        models,
        [{"id": 1, "method": "search_documents", "params": {"query": "revenue", "top_k": 5}}],
        _write(tmp_path),
    )
    resp = seen[1]
    assert resp["result"]["tool"] == "search_documents"
    assert any("fixture" in r["title"] for r in resp["result"]["results"])


async def test_bridge_unknown_method_error(settings, models, tmp_path):
    seen = await _exchange(
        settings,
        models,
        [{"id": 7, "method": "delete_everything", "params": {}}],
        _write(tmp_path),
    )
    assert seen[7]["error"]["code"] == -32601


async def test_bridge_rpc_error_envelope(settings, models, tmp_path):
    seen = await _exchange(
        settings,
        models,
        [{"id": 3, "method": "compress_document", "params": {"doc_id": "nope"}}],
        _write(tmp_path),
    )
    env = seen[3]["result"]
    assert env["tool"] == "compress_document"
    assert "document not found" in env["error"]


async def test_bridge_records_telemetry(settings, models, tmp_path):
    await _exchange(
        settings,
        models,
        [{"id": 9, "method": "search_documents", "params": {"query": "revenue"}}],
        _write(tmp_path),
    )
    conn = __import__("sqlite3").connect(settings.telemetry_db)
    rows = conn.execute("SELECT tool, doc_id FROM calls").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "search_documents"
