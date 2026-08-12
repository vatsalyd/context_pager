from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from context_pager.config import BridgeSettings, get_bridge_settings
from context_pager.core.storage import expand

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tool TEXT NOT NULL,
    doc_id TEXT,
    original_tokens INTEGER NOT NULL DEFAULT 0,
    compressed_tokens INTEGER NOT NULL DEFAULT 0,
    cost_saved_usd REAL NOT NULL DEFAULT 0,
    elapsed_ms INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(settings: BridgeSettings | None = None) -> sqlite3.Connection:
    settings = settings or get_bridge_settings()
    path = expand(settings.telemetry_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def record_call(tool: str, envelope: dict[str, Any], settings: BridgeSettings | None = None) -> None:
    """Log one tool call locally. Bridge computes cost; relay only sees rollups (Q14)."""
    meta = envelope.get("metadata", {}) or {}
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(settings)
    try:
        conn.execute(
            """INSERT INTO calls(ts, tool, doc_id, original_tokens, compressed_tokens, cost_saved_usd, elapsed_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                tool,
                envelope.get("doc_id"),
                int(meta.get("original_tokens", 0)),
                int(meta.get("compressed_tokens", 0)),
                float(meta.get("cost_saved_usd", 0.0)),
                int(meta.get("elapsed_ms", 0)),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(query: str, params: tuple = (), settings: BridgeSettings | None = None) -> list[tuple]:
    conn = _connect(settings)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def print_stats(settings: BridgeSettings | None = None) -> None:
    """`pager stats`: local usage + cost savings, per-doc and recent-call breakdown."""
    total_calls = _rows("SELECT COUNT(*) FROM calls", settings=settings)[0][0]
    total_orig = _rows("SELECT COALESCE(SUM(original_tokens), 0) FROM calls", settings=settings)[0][0]
    total_compressed = _rows("SELECT COALESCE(SUM(compressed_tokens), 0) FROM calls", settings=settings)[0][0]
    total_cost = _rows("SELECT COALESCE(SUM(cost_saved_usd), 0) FROM calls", settings=settings)[0][0]

    print("=== context-pager bridge telemetry ===")
    print(f"calls:            {total_calls}")
    print(f"tokens in:        {total_orig}")
    print(f"tokens out:       {total_compressed}")
    saved = (total_orig - total_compressed) if total_orig else 0
    print(f"tokens saved:     {saved}")
    print(f"cost saved (usd): {total_cost:.4f}")

    per_doc = _rows(
        """SELECT COALESCE(doc_id, '<memory>'), COUNT(*), SUM(original_tokens), SUM(compressed_tokens)
           FROM calls GROUP BY doc_id ORDER BY 2 DESC""",
        settings=settings,
    )
    if per_doc:
        print("\nper-doc:")
        for doc_id, calls, orig, comp in per_doc:
            print(
                f"  {doc_id}\t{calls} calls\t"
                f"saved {int(orig or 0) - int(comp or 0)} tokens"
            )

    recent = _rows("SELECT ts, tool, doc_id, cost_saved_usd, elapsed_ms FROM calls ORDER BY id DESC LIMIT 20", settings=settings)
    if recent:
        print("\nrecent calls:")
        for ts, tool, doc_id, cost, ms in recent:
            print(f"  {ts}\t{tool}\t{doc_id or ''}\t${cost:.4f}\t{ms}ms")
