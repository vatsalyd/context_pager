from __future__ import annotations

"""Context Pager MCP Server - Zero-copy context paging for LLM agents."""

__version__ = "0.1.0"

from context_pager.config import settings
from context_pager.deps import Dependencies, lifespan

__all__ = [
    "settings",
    "Dependencies",
    "lifespan",
]