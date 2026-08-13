from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import websockets

from context_pager.config import RelaySettings
from context_pager.relay.connections import ConnectionManager
from context_pager.relay.db import RelayDB, sha256
from context_pager.relay.ratelimit import TokenBucket
from context_pager.relay.server import build_app
from context_pager.relay.tools import authorize, route_call


@pytest.fixture
def settings(tmp_path):
    return RelaySettings(
        PAGER_SQLITE_DB=str(tmp_path / "relay.db"),
        PAGER_RATE_LIMIT_CALLS_PER_HOUR=1000,
        PAGER_RELAY_HOST="127.0.0.1",
        PAGER_RELAY_PORT=0,
    )


@pytest.fixture
def db(settings):
    return RelayDB(settings.sqlite_db)


@pytest.fixture
def manager():
    return ConnectionManager(max_per_user=2)


def _make_user(db: RelayDB) -> dict:
    user_id = uuid.uuid4().hex[:16]
    agent_key = f"pgr_agent_{secrets_hex()}"
    bridge_key = f"pgr_bridge_{secrets_hex()}"
    db.create_user(user_id, sha256(agent_key), sha256(bridge_key), "127.0.0.1")
    return {"user_id": user_id, "agent_key": agent_key, "bridge_key": bridge_key}


def secrets_hex() -> str:
    import secrets

    return secrets.token_hex(16)


class FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


class FakeCtx:
    def __init__(self, headers: dict):
        self.request_context = type("RC", (), {"request": FakeRequest(headers)})()


async def test_signup_issues_keys_once(settings, db, manager, tmp_path):
    app = build_app(settings, db, manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
        r = await client.post("/v1/signup")
    data = r.json()
    assert data["agent_key"].startswith("pgr_agent_")
    assert data["bridge_key"].startswith("pgr_bridge_")
    assert db.user_by_agent_hash(sha256(data["agent_key"])) is not None
    assert db.user_by_bridge_hash(sha256(data["bridge_key"])) is not None
    # keys are stored hashed, never in plaintext
    raw = open(settings.sqlite_db, "rb").read()
    assert data["agent_key"].encode() not in raw


async def test_authorize_accepts_valid_key(settings, db, manager):
    keys = _make_user(db)
    bucket = TokenBucket(1000, 1)
    ctx = FakeCtx({"authorization": f"Bearer {keys['agent_key']}"})
    user = await authorize(ctx, "search_documents", db, bucket)
    assert isinstance(user, dict) and user["user_id"] == keys["user_id"]


async def test_authorize_rejects_bad_token(settings, db, manager):
    bucket = TokenBucket(1000, 1)
    ctx = FakeCtx({"authorization": "Bearer pgr_agent_wrong"})
    out = await authorize(ctx, "search_documents", db, bucket)
    assert isinstance(out, str)
    assert json.loads(out)["error"] == "unauthorized: invalid agent key"


async def test_authorize_requires_header(settings, db, manager):
    bucket = TokenBucket(1000, 1)
    ctx = FakeCtx({})
    out = await authorize(ctx, "search_documents", db, bucket)
    assert "missing bearer token" in json.loads(out)["error"]


async def test_rate_limit(settings, db, manager):
    keys = _make_user(db)
    bucket = TokenBucket(2, 0.0)
    for _ in range(2):
        assert await authorize(FakeCtx({"authorization": f"Bearer {keys['agent_key']}"}), "t", db, bucket) is not None
    out = await authorize(FakeCtx({"authorization": f"Bearer {keys['agent_key']}"}), "t", db, bucket)
    assert "rate limit" in json.loads(out)["error"]
    assert json.loads(out)["metadata"]["retryable"] is True


async def test_route_call_offline_envelope(settings, db, manager):
    user = db.user_by_agent_hash(sha256(_make_user(db)["agent_key"]))
    out = json.loads(await route_call(user, "compress_document", {"doc_id": "x"}, manager, db))
    assert out["tool"] == "compress_document"
    assert "bridge_offline" in out["error"]
    assert out["metadata"]["retryable"] is True


async def test_full_roundtrip_fake_bridge(settings, db, manager):
    """Q38: real relay app over a real WSS channel with a scripted fake bridge."""
    keys = _make_user(db)
    app = build_app(settings, db, manager)

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]

        async def fake_bridge():
            async with websockets.connect(f"ws://127.0.0.1:{port}/bridge") as ws:
                await ws.send(json.dumps({"method": "auth", "params": {"bridge_key": keys["bridge_key"]}}))
                auth = json.loads(await ws.recv())
                assert auth["ok"] is True
                req = json.loads(await ws.recv())
                assert req["method"] == "search_documents"
                envelope = {
                    "tool": "search_documents",
                    "query": req["params"]["query"],
                    "results": [{"doc_id": "d1", "title": "acme", "snippet": "Revenue grew.", "best_page": 1, "score": 0.9}],
                    "recalled_insights": [],
                    "metadata": {"original_tokens": 4000, "compressed_tokens": 500, "cost_saved_usd": 0.01, "elapsed_ms": 300},
                }
                await ws.send(json.dumps({"id": req["id"], "result": envelope}))
                await ws.recv()  # keep channel open until cancelled

        bridge_task = asyncio.create_task(fake_bridge())
        # wait until the bridge is registered
        for _ in range(100):
            if manager.pick(keys["user_id"]) is not None:
                break
            await asyncio.sleep(0.02)

        user = db.user_by_agent_hash(sha256(keys["agent_key"]))
        out = json.loads(await route_call(user, "search_documents", {"query": "revenue", "top_k": 5}, manager, db))
        assert out["tool"] == "search_documents"
        assert out["results"][0]["doc_id"] == "d1"

        usage = db.usage(keys["user_id"])
        assert usage and usage[0]["calls"] == 1
        assert usage[0]["tokens_in"] == 4000
        bridge_task.cancel()
    finally:
        server.should_exit = True
        await task
