from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    agent_key_hash TEXT NOT NULL,
    bridge_key_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    signup_ip TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_agent_hash ON users(agent_key_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_bridge_hash ON users(bridge_key_hash);

CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    user_id TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_saved_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id)
);

CREATE TABLE IF NOT EXISTS signups (
    ip TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ip, day)
);
"""


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RelayDB:
    """SQLite on the relay's EBS volume: users (keys stored hashed) + daily usage rollups.

    Content never touches the relay; the bridge does the heavy lifting (Q1/Q14).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    # ── signup ────────────────────────────────────────────────

    def signup_count(self, ip: str) -> int:
        row = self._conn.execute(
            "SELECT count FROM signups WHERE ip = ? AND day = ?", (ip, date.today().isoformat())
        ).fetchone()
        return row[0] if row else 0

    def bump_signup(self, ip: str) -> None:
        self._conn.execute(
            """INSERT INTO signups(ip, day, count) VALUES (?, ?, 1)
               ON CONFLICT(ip, day) DO UPDATE SET count = count + 1""",
            (ip, date.today().isoformat()),
        )
        self._conn.commit()

    def create_user(self, user_id: str, agent_hash: str, bridge_hash: str, ip: str) -> None:
        self._conn.execute(
            "INSERT INTO users(user_id, agent_key_hash, bridge_key_hash, created_at, signup_ip) VALUES (?, ?, ?, ?, ?)",
            (user_id, agent_hash, bridge_hash, _now_iso(), ip),
        )
        self._conn.commit()

    # ── lookups ───────────────────────────────────────────────

    def _row_to_user(self, row: tuple) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "user_id": row[0],
            "agent_key_hash": row[1],
            "bridge_key_hash": row[2],
            "created_at": row[3],
            "signup_ip": row[4],
        }

    def user_by_agent_hash(self, agent_hash: str) -> dict[str, Any] | None:
        return self._row_to_user(
            self._conn.execute(
                "SELECT user_id, agent_key_hash, bridge_key_hash, created_at, signup_ip FROM users WHERE agent_key_hash = ?",
                (agent_hash,),
            ).fetchone()
        )

    def user_by_bridge_hash(self, bridge_hash: str) -> dict[str, Any] | None:
        return self._row_to_user(
            self._conn.execute(
                "SELECT user_id, agent_key_hash, bridge_key_hash, created_at, signup_ip FROM users WHERE bridge_key_hash = ?",
                (bridge_hash,),
            ).fetchone()
        )

    # ── usage rollup ──────────────────────────────────────────

    def record_usage(self, user_id: str, envelope: dict[str, Any]) -> None:
        """Store stripped daily totals (Q14): no doc ids, no content, no per-call details."""
        meta = envelope.get("metadata", {}) or {}
        self._conn.execute(
            """INSERT INTO usage(day, user_id, calls, tokens_in, tokens_out, cost_saved_usd)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(day, user_id) DO UPDATE SET
                   calls = calls + 1,
                   tokens_in = tokens_in + excluded.tokens_in,
                   tokens_out = tokens_out + excluded.tokens_out,
                   cost_saved_usd = cost_saved_usd + excluded.cost_saved_usd""",
            (
                date.today().isoformat(),
                user_id,
                int(meta.get("original_tokens", 0)),
                int(meta.get("compressed_tokens", 0)),
                float(meta.get("cost_saved_usd", 0.0)),
            ),
        )
        self._conn.commit()

    def usage(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT day, calls, tokens_in, tokens_out, cost_saved_usd FROM usage WHERE user_id = ? ORDER BY day",
            (user_id,),
        ).fetchall()
        return [
            {"day": r[0], "calls": r[1], "tokens_in": r[2], "tokens_out": r[3], "cost_saved_usd": r[4]}
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
