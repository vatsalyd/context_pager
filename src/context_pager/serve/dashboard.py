from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.authentication import AuthenticationMiddleware

from context_pager.auth.middleware import AuthBackend
from context_pager.config import settings
from context_pager.contextvar import tenant_id_var
from context_pager.db import acquire_conn
from context_pager.deps import Dependencies, lifespan

logger = logging.getLogger("context_pager.dashboard")


@asynccontextmanager
async def dashboard_lifespan(app: FastAPI):
    async with lifespan(app):
        yield


app = FastAPI(
    title="Context Pager Dashboard",
    lifespan=dashboard_lifespan,
)

app.add_middleware(AuthenticationMiddleware, backend=AuthBackend())


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/v1/signup")
async def signup(request: Request):
    """Create a user + issue an API key. No auth required."""
    import secrets

    body = await request.json()
    email = body.get("email", "").strip()
    if not email or "@" not in email:
        return JSONResponse({"error": "valid email required"}, 400)

    pool = await Dependencies.pg_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
        if existing:
            return JSONResponse({"error": "email already registered"}, 409)

        api_key = settings.api_key_prefix + secrets.token_urlsafe(24)
        hashed = hashlib.sha256(api_key.encode()).hexdigest()
        prefix = api_key[:8]
        tenant_id = "tenant_" + uuid.uuid4().hex[:16]
        await conn.execute(
            """INSERT INTO users (id, email, hashed_api_key, api_key_prefix, plan, tenant_id)
               VALUES (uuid_generate_v4(), $1, $2, $3, 'free', $4)""",
            email, hashed, prefix, tenant_id,
        )

    return JSONResponse({"tenant_id": tenant_id, "api_key": api_key, "email": email}, 201)


@app.get("/v1/usage")
async def get_usage():
    # TODO: implement usage stats
    return {"tool_calls": 0, "tokens_compressed": 0, "storage_bytes": 0, "est_cost_usd": 0.0}


@app.post("/v1/documents")
async def upload_document(
    file: UploadFile = File(...),
    doc_id: str | None = Form(default=None),
    source_kind: str = Form(default="unstructured"),
    tenant_id: str = Form(default=""),
):
    """Upload a document for ingestion. Async worker chunks + embeds + extracts."""
    content = await file.read()
    if not content:
        return JSONResponse({"error": "empty file"}, 400)

    raw_text = content.decode("utf-8", errors="replace")

    # Use provided tenant_id or default (signup flow sets this via middleware)
    tid = tenant_id or tenant_id_var.get() or "default"
    did = doc_id or hashlib.sha256(raw_text.encode()).hexdigest()[:16]

    pool = await Dependencies.pg_pool()

    # Check for duplicate
    async with acquire_conn(tid) as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM documents WHERE id = $1", did,
        )
        if existing:
            return JSONResponse({"error": "doc_id already exists", "status": existing["status"]}, 409)

        await conn.execute(
            """INSERT INTO documents (id, tenant_id, title, content, source_kind, status)
               VALUES ($1, $2, $3, $4, $5, 'processing')""",
            did, tid, file.filename or "untitled", raw_text, source_kind,
        )

    # Spawn async ingestion worker (fire-and-forget)
    task = asyncio.create_task(_ingest_worker(did, tid, raw_text))
    task.add_done_callback(lambda t: t.exception() and logger.error("Ingest task failed: %s", t.exception()))

    return JSONResponse({"doc_id": did, "status": "processing", "title": file.filename}, 202)


async def _ingest_worker(doc_id: str, tenant_id: str, raw_text: str) -> None:
    """Background worker: chunk → embed → extract → store."""
    import time
    start = time.monotonic()
    try:
        # 1. Chunk text
        logger.info("Ingest %s: starting chunking", doc_id)
        from context_pager.ingestion.chunker import chunk_text
        chunks = chunk_text(raw_text, target_tokens=512, overlap_tokens=50)
        logger.info("Ingest %s: %d chunks from %d chars", doc_id, len(chunks), len(raw_text))

        # 2. Embed all chunks (dense + sparse)
        logger.info("Ingest %s: starting embedding", doc_id)
        from context_pager.knowledge.embedder import get_embedder
        embedder = get_embedder()
        dense_vecs, sparse_weights = await embedder.embed_multi(chunks)
        logger.info("Ingest %s: embedding done", doc_id)

        # 3. Store chunks + embeddings
        async with acquire_conn(tenant_id) as conn:
            for i, (text, dense, sparse) in enumerate(zip(chunks, dense_vecs, sparse_weights)):
                # Convert sparse dict to pgvector sparsevec text literal
                from context_pager.knowledge.retriever import _dict_to_sparsevec
                sparse_literal = _dict_to_sparsevec(sparse) if sparse else None
                await conn.execute(
                    """INSERT INTO document_chunks
                       (tenant_id, document_id, chunk_index, text, embedding, sparse_weights)
                       VALUES ($1, $2, $3, $4, $5::vector, $6)""",
                    tenant_id, doc_id, i, text,
                    dense, sparse_literal,
                )

            # 4. Extract entities (spaCy NER + regex)
            from context_pager.ingestion.extractor import extract_entities
            extraction = extract_entities(raw_text, tenant_id=tenant_id)

            # 5. Store entities
            entity_name_to_id: dict[str, int] = {}
            for ent in extraction.entities:
                row = await conn.fetchrow(
                    """INSERT INTO entities (tenant_id, document_id, type, name, properties)
                       VALUES ($1, $2, $3, $4, $5::jsonb)
                       RETURNING id""",
                    tenant_id, doc_id, ent.entity_type, ent.name,
                    '{}',
                )
                entity_name_to_id[ent.name] = row["id"]

            # 6. Store relations (MENTIONED_WITH)
            for from_name, to_name, rel_type in extraction.relations:
                from_id = entity_name_to_id.get(from_name)
                to_id = entity_name_to_id.get(to_name)
                if from_id and to_id and from_id != to_id:
                    try:
                        await conn.execute(
                            """INSERT INTO entity_relations (from_id, to_id, relation, properties, tenant_id)
                               VALUES ($1, $2, $3, '{}'::jsonb, $4)
                               ON CONFLICT DO NOTHING""",
                            from_id, to_id, rel_type, tenant_id,
                        )
                    except Exception:
                        pass

            # 7. Update document status
            elapsed = time.monotonic() - start
            await conn.execute(
                """UPDATE documents SET status = 'ready', updated_at = now()
                   WHERE id = $1""",
                doc_id,
            )
            logger.info(
                "Ingest %s complete: %d chunks, %d entities in %.1fs",
                doc_id, len(chunks), len(extraction.entities), elapsed,
            )

    except Exception as e:
        logger.exception("Ingest worker failed for %s: %s", doc_id, e)
        async with acquire_conn(tenant_id) as conn:
            await conn.execute(
                """UPDATE documents SET status = 'failed', updated_at = now()
                   WHERE id = $1""",
                doc_id,
            )


@app.get("/v1/documents")
async def list_documents():
    """List documents for the current tenant."""
    tid = tenant_id_var.get() or "default"
    async with acquire_conn(tid) as conn:
        rows = await conn.fetch(
            """SELECT id, title, source_kind, status, created_at, updated_at
               FROM documents WHERE tenant_id = $1 ORDER BY created_at DESC""",
            tid,
        )
    return {"documents": [dict(r) for r in rows]}


@app.get("/v1/documents/{doc_id}")
async def get_document(doc_id: str):
    """Get document status and metadata."""
    tid = tenant_id_var.get() or "default"
    async with acquire_conn(tid) as conn:
        doc = await conn.fetchrow(
            "SELECT * FROM documents WHERE id = $1", doc_id,
        )
        if not doc:
            return JSONResponse({"error": "not found"}, 404)

        chunk_count = await conn.fetchval(
            "SELECT count(*) FROM document_chunks WHERE document_id = $1", doc_id,
        )
        entity_count = await conn.fetchval(
            "SELECT count(*) FROM entities WHERE document_id = $1", doc_id,
        )

    return {
        "id": doc["id"],
        "title": doc["title"],
        "status": doc["status"],
        "source_kind": doc["source_kind"],
        "chunks": chunk_count,
        "entities": entity_count,
        "created_at": str(doc["created_at"]),
        "updated_at": str(doc["updated_at"]),
    }


@app.get("/dashboard/", response_class=HTMLResponse)
async def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Context Pager Dashboard</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { font-family: system-ui; margin: 40px; }
            .card { border: 1px solid #ddd; border-radius: 8px; padding: 20px; margin: 20px 0; }
            .metric { font-size: 2em; font-weight: bold; color: #2563eb; }
        </style>
    </head>
    <body>
        <h1>Context Pager Dashboard</h1>
        <div class="card">
            <h2>Cost Savings</h2>
            <div class="metric" id="savings">$0.00</div>
        </div>
        <div class="card">
            <h2>Total Tokens Compressed</h2>
            <div class="metric" id="tokens">0</div>
        </div>
        <div class="card">
            <canvas id="costChart"></canvas>
        </div>
        <script>
            async function loadStats() {
                try {
                    const resp = await fetch('/v1/usage');
                    const data = await resp.json();
                    document.getElementById('savings').textContent = '$' + data.est_cost_usd.toFixed(2);
                    document.getElementById('tokens').textContent = data.tokens_compressed.toLocaleString();
                } catch (e) {
                    console.error(e);
                }
            }
            loadStats();
            setInterval(loadStats, 30000);
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8501)