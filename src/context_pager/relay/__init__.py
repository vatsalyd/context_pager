"""Thin AWS relay. See CONTEXT.md for the routing contract."""

from context_pager.relay.server import build_app, run_relay

__all__ = ["build_app", "run_relay"]
