from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'unstructured',
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DimensionMismatchError(RuntimeError):
    pass


class Library:
    """Sqlite-vec backed library: document registry + chunk index + agent memory.

    Lives on the laptop. All methods are synchronous (sqlite3 is fast and the
    bridge dispatches calls via asyncio executor where needed).
    """

    def __init__(self, db_path: str | Path, embedding_dim: int, chunks_per_page: int = 4):
        self.db_path = Path(db_path)
        self.embedding_dim = embedding_dim
        self.chunks_per_page = chunks_per_page
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        stored = self._meta_get("embedding_dim")
        if stored is None:
            self._meta_set("embedding_dim", str(self.embedding_dim))
        elif int(stored) != self.embedding_dim:
            raise DimensionMismatchError(
                f"index was built with embedding_dim={stored}, current model is {self.embedding_dim}; "
                "run `pager docs reindex` for every document, or delete the db and re-add"
            )
        stored_cpp = self._meta_get("chunks_per_page")
        if stored_cpp is None:
            self._meta_set("chunks_per_page", str(self.chunks_per_page))
        elif int(stored_cpp) != self.chunks_per_page:
            raise DimensionMismatchError(
                f"index was built with chunks_per_page={stored_cpp}, current is {self.chunks_per_page}; reindex"
            )
        dim = self.embedding_dim
        self._conn.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                doc_id TEXT,
                chunk_idx INTEGER,
                text TEXT,
                embedding FLOAT[{dim}] distance_metric=cosine
            )"""
        )
        self._conn.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory USING vec0(
                memory_id INTEGER PRIMARY KEY,
                key TEXT,
                insights TEXT,
                embedding FLOAT[{dim}] distance_metric=cosine
            )"""
        )
        self._conn.commit()

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    # ── registry ──────────────────────────────────────────────

    def add_document(
        self,
        title: str,
        path: str,
        kind: str,
        chunk_texts: list[str],
        embeddings: list[list[float]],
        doc_id: str | None = None,
    ) -> str:
        doc_id = doc_id or uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO docs(doc_id, title, path, kind, indexed_at) VALUES (?, ?, ?, ?, ?)",
            (doc_id, title, path, kind, _now_iso()),
        )
        self._insert_chunks(doc_id, chunk_texts, embeddings)
        self._conn.commit()
        return doc_id

    def replace_chunks(self, doc_id: str, chunk_texts: list[str], embeddings: list[list[float]]) -> None:
        self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        self._insert_chunks(doc_id, chunk_texts, embeddings)
        self._conn.execute(
            "UPDATE docs SET indexed_at = ? WHERE doc_id = ?", (_now_iso(), doc_id)
        )
        self._conn.commit()

    def _insert_chunks(self, doc_id: str, chunk_texts: list[str], embeddings: list[list[float]]) -> None:
        for idx, (text, emb) in enumerate(zip(chunk_texts, embeddings)):
            self._conn.execute(
                "INSERT INTO chunks(doc_id, chunk_idx, text, embedding) VALUES (?, ?, ?, ?)",
                (doc_id, idx, text, json.dumps(emb)),
            )

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT doc_id, title, path, kind, indexed_at FROM docs WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if not row:
            return None
        count = self._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0]
        return {
            "doc_id": row[0],
            "title": row[1],
            "path": row[2],
            "kind": row[3],
            "indexed_at": row[4],
            "chunks": count,
        }

    def list_documents(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT doc_id, title, path, kind, indexed_at FROM docs ORDER BY indexed_at"
        ).fetchall()
        result = []
        for r in rows:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (r[0],)
            ).fetchone()[0]
            result.append(
                {"doc_id": r[0], "title": r[1], "path": r[2], "kind": r[3], "indexed_at": r[4], "chunks": count}
            )
        return result

    def remove_document(self, doc_id: str) -> bool:
        self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        doc_deleted = self._conn.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,)).rowcount
        self._conn.commit()
        return doc_deleted > 0

    # ── search ────────────────────────────────────────────────

    def search_chunks(self, query_embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        """KNN over chunks. Returns best chunk per document (deduped by doc_id)."""
        rows = self._conn.execute(
            """SELECT chunk_id, doc_id, chunk_idx, text, distance
               FROM chunks WHERE embedding MATCH ?
               ORDER BY distance LIMIT ?""",
            (json.dumps(query_embedding), top_k * 8),
        ).fetchall()
        seen: set[str] = set()
        result = []
        for chunk_id, doc_id, chunk_idx, text, distance in rows:
            if doc_id in seen:
                continue
            seen.add(doc_id)
            result.append(
                {
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "chunk_idx": chunk_idx,
                    "text": text,
                    "score": round(1.0 - distance, 4),
                }
            )
            if len(result) >= top_k:
                break
        return result

    def count_chunks(self, doc_id: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()[0]

    def page_chunks(self, doc_id: str, page: int) -> list[str]:
        """Chunks for canonical page N (1-indexed). Page boundaries are stable
        regardless of per-call max_return_tokens, so search's best_page stays valid."""
        start = (page - 1) * self.chunks_per_page
        end = start + self.chunks_per_page
        rows = self._conn.execute(
            "SELECT text FROM chunks WHERE doc_id = ? AND chunk_idx >= ? AND chunk_idx < ? ORDER BY chunk_idx",
            (doc_id, start, end),
        ).fetchall()
        return [r[0] for r in rows]

    def pages_total(self, doc_id: str) -> int:
        chunks = self.count_chunks(doc_id)
        return max(1, (chunks + self.chunks_per_page - 1) // self.chunks_per_page)

    # ── agent memory ──────────────────────────────────────────

    def upsert_memory(self, key: str, insights: str, embedding: list[float]) -> None:
        row = self._conn.execute(
            "SELECT memory_id FROM agent_memory WHERE key = ?", (key,)
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE agent_memory SET insights = ?, embedding = ? WHERE memory_id = ?",
                (insights, json.dumps(embedding), row[0]),
            )
        else:
            self._conn.execute(
                "INSERT INTO agent_memory(key, insights, embedding) VALUES (?, ?, ?)",
                (key, insights, json.dumps(embedding)),
            )
        self._conn.commit()

    def recall_memory(self, query_embedding: list[float], threshold: float = 0.78, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT key, insights, distance
               FROM agent_memory WHERE embedding MATCH ?
               ORDER BY distance LIMIT ?""",
            (json.dumps(query_embedding), limit),
        ).fetchall()
        result = []
        for key, insights, distance in rows:
            score = round(1.0 - distance, 4)
            if score >= threshold:
                result.append({"key": key, "insights": insights, "similarity": score})
        return result

    def close(self) -> None:
        self._conn.close()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


@contextmanager
def open_library(settings=None) -> Iterator[Library]:
    """Open the library with the bridge settings' db path + embedding dim."""
    from context_pager.config import get_bridge_settings
    from context_pager.core.embedder import dim_for

    settings = settings or get_bridge_settings()
    chunks_per_page = max(1, settings.max_return_tokens // settings.chunk_tokens)
    lib = Library(expand(settings.db_path), dim_for(settings), chunks_per_page)
    try:
        yield lib
    finally:
        lib.close()
