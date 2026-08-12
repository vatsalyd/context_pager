from __future__ import annotations

import asyncio
import contextlib
import logging

from context_pager.bridge.client import BridgeClient
from context_pager.bridge.server import serve_local
from context_pager.config import BridgeSettings, get_bridge_settings
from context_pager.core.models import get_models


async def run_bridge_async(settings: BridgeSettings | None = None) -> None:
    """Preload models (Q18), serve localhost MCP, and keep the WSS channel up."""
    settings = settings or get_bridge_settings()
    models = get_models(settings)
    await models.preload()

    mcp_task = asyncio.create_task(serve_local(settings, models))
    try:
        await BridgeClient(settings, models).run()
    finally:
        mcp_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mcp_task


def run_bridge() -> None:
    logging.basicConfig(
        level=get_bridge_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_bridge_async())
    except KeyboardInterrupt:
        pass
