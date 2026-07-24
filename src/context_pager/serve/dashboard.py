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


class DashboardTenantMiddleware:
    """Copy request.state.tenant_id into contextvar for all dashboard requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from starlette.requests import Request
        request = Request(scope, receive=send)
        tid = getattr(request.state, "tenant_id", "") or ""
        token = tenant_id_var.set(tid)
        try:
            return await self.app(scope, receive, send)
        finally:
            tenant_id_var.reset(token)


app.add_middleware(DashboardTenantMiddleware)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/v1/admin/rollup")
async def trigger_rollup():
    """Manually trigger rollup (admin use)."""
    from context_pager.telemetry.rollup import rollup_audit_events
    n = await rollup_audit_events()
    return {"rolled_up": n}


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
    """Return usage stats combining rolled-up historical + live audit_events for today."""
    tid = tenant_id_var.get() or "default"
    async with acquire_conn(tid) as conn:
        # Historical: from tenant_usage_daily (excludes today)
        historical = await conn.fetchrow("""
            SELECT 
                coalesce(sum(tool_calls), 0) as tool_calls,
                coalesce(sum(tokens_compressed), 0) as tokens_compressed,
                coalesce(sum(est_cost_usd), 0) as est_cost_usd
            FROM tenant_usage_daily
            WHERE tenant_id = $1
        """, tid)

        # Live: today's audit_events (not yet rolled up)
        live = await conn.fetchrow("""
            SELECT 
                count(*) FILTER (WHERE event_type = 'tool_call') as tool_calls,
                coalesce(sum(original_tokens - compressed_tokens), 0) as tokens_compressed,
                coalesce(sum(cost_saved_usd), 0) as est_cost_usd
            FROM audit_events
            WHERE tenant_id = $1
              AND created_at >= current_date
        """, tid)

        # Storage usage
        storage = await conn.fetchrow("""
            SELECT 
                coalesce(sum(pg_column_size(content)), 0) as storage_bytes
            FROM documents
            WHERE tenant_id = $1
        """, tid)

        # Daily trend for chart (last 30 days)
        trend_rows = await conn.fetch("""
            SELECT date, tool_calls, tokens_compressed, est_cost_usd
            FROM tenant_usage_daily
            WHERE tenant_id = $1 AND date >= current_date - interval '30 days'
            ORDER BY date
        """, tid)

    return {
        "tool_calls": historical["tool_calls"] + live["tool_calls"],
        "tokens_compressed": historical["tokens_compressed"] + live["tokens_compressed"],
        "storage_bytes": storage["storage_bytes"],
        "est_cost_usd": float(historical["est_cost_usd"] + live["est_cost_usd"]),
        "trend": [
            {"date": str(r["date"]), "calls": r["tool_calls"], "tokens": r["tokens_compressed"], "cost": float(r["est_cost_usd"])}
            for r in trend_rows
        ],
    }


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


@app.delete("/v1/documents/{doc_id}")
async def delete_document(doc_id: str):
    """Delete a document and all its chunks/entities/relations (cascade)."""
    tid = tenant_id_var.get() or "default"
    async with acquire_conn(tid) as conn:
        doc = await conn.fetchrow(
            "SELECT id FROM documents WHERE id = $1", doc_id,
        )
        if not doc:
            return JSONResponse({"error": "not found"}, 404)

        await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)

    return {"deleted": doc_id}


@app.get("/v1/documents/{doc_id}/entities")
async def get_document_entities(doc_id: str, limit: int = 50):
    """List entities extracted from a document."""
    tid = tenant_id_var.get() or "default"
    async with acquire_conn(tid) as conn:
        rows = await conn.fetch(
            """SELECT id, type, name, properties, created_at
               FROM entities
               WHERE document_id = $1 AND tenant_id = $2
               ORDER BY type, name
               LIMIT $3""",
            doc_id, tid, limit,
        )
        relations = await conn.fetch(
            """SELECT er.from_id, er.to_id, er.relation, er.properties,
                      ef.name as from_name, et.name as to_name
               FROM entity_relations er
               JOIN entities ef ON er.from_id = ef.id
               JOIN entities et ON er.to_id = et.id
               WHERE ef.document_id = $1 AND er.tenant_id = $2
               LIMIT 100""",
            doc_id, tid,
        )
    return {
        "entities": [dict(r) for r in rows],
        "relations": [dict(r) for r in relations],
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
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { font-family: system-ui, -apple-system, sans-serif; background: #f5f5f5; padding: 20px; }
            .container { max-width: 1200px; margin: 0 auto; }
            h1 { color: #1f2937; margin-bottom: 24px; }
            h2 { color: #374151; font-size: 18px; margin-bottom: 12px; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin-bottom: 24px; }
            .card { background: white; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
            .card h3 { font-size: 14px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
            .metric { font-size: 2.5em; font-weight: 700; color: #2563eb; }
            .metric.green { color: #10b981; }
            .metric.purple { color: #8b5cf6; }
            .metric.orange { color: #f59e0b; }
            .full-width { grid-column: 1 / -1; }
            .chart-card { grid-column: span 2; }
            @media (max-width: 768px) { .chart-card { grid-column: span 1; } }
            table { width: 100%; border-collapse: collapse; margin-top: 8px; }
            th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #e5e7eb; font-size: 14px; }
            th { color: #6b7280; font-weight: 500; }
            tr:hover { background: #f9fafb; }
            .status { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; }
            .status-ready { background: #d1fae5; color: #065f46; }
            .status-processing { background: #fef3c7; color: #92400e; }
            .status-failed { background: #fee2e2; color: #991b1b; }
            .btn { padding: 6px 12px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 500; }
            .btn-primary { background: #2563eb; color: white; }
            .btn-danger { background: #ef4444; color: white; }
            .btn:hover { opacity: 0.9; }
            .entity-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
            .entity-tag { padding: 4px 10px; border-radius: 16px; font-size: 12px; font-weight: 500; }
            .entity-person { background: #dbeafe; color: #1e40af; }
            .entity-org { background: #ede9fe; color: #5b21b6; }
            .entity-money { background: #d1fae5; color: #065f46; }
            .entity-date { background: #fef3c7; color: #92400e; }
            .entity-gpe { background: #fce7f3; color: #9d174d; }
            .entity-email { background: #e0e7ff; color: #3730a3; }
            .entity-phone { background: #ccfbf1; color: #0f766e; }
            .entity-url { background: #f3e8ff; color: #7c3aed; }
            .empty { color: #9ca3af; font-style: italic; padding: 20px; text-align: center; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Context Pager Dashboard</h1>
            <div class="grid" id="metrics"></div>
            <div class="grid">
                <div class="card chart-card">
                    <h2>Cost Savings Trend (30d)</h2>
                    <canvas id="costChart" height="80"></canvas>
                </div>
                <div class="card full-width">
                    <h2>Documents</h2>
                    <div id="docList"><div class="empty">Loading...</div></div>
                </div>
                <div class="card full-width" id="entityPanel" style="display:none;">
                    <h2>Entities: <span id="entityDocName"></span></h2>
                    <div id="entityList"></div>
                </div>
            </div>
        </div>
        <script>
            let costChart = null;

            function formatBytes(b) {
                if (b < 1024) return b + ' B';
                if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
                return (b/1048576).toFixed(1) + ' MB';
            }
            function fmt(n) { return n.toLocaleString(); }

            async function loadStats() {
                try {
                    const r = await fetch('/v1/usage');
                    const d = await r.json();
                    document.getElementById('metrics').innerHTML = `
                        <div class="card"><h3>Cost Saved (all time)</h3><div class="metric green">$${d.est_cost_usd.toFixed(4)}</div></div>
                        <div class="card"><h3>Tokens Compressed</h3><div class="metric">${fmt(d.tokens_compressed)}</div></div>
                        <div class="card"><h3>Tool Calls</h3><div class="metric purple">${fmt(d.tool_calls)}</div></div>
                        <div class="card"><h3>Storage Used</h3><div class="metric orange">${formatBytes(d.storage_bytes)}</div></div>
                    `;
                    if (d.trend && d.trend.length > 0) renderChart(d.trend);
                } catch(e) { console.error(e); }
            }

            function renderChart(trend) {
                const ctx = document.getElementById('costChart').getContext('2d');
                if (costChart) costChart.destroy();
                costChart = new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: trend.map(t => t.date),
                        datasets: [{
                            label: 'Tool Calls',
                            data: trend.map(t => t.calls),
                            backgroundColor: '#8b5cf6',
                            yAxisID: 'y',
                        }, {
                            label: 'Tokens Saved',
                            data: trend.map(t => t.tokens),
                            backgroundColor: '#10b981',
                            yAxisID: 'y1',
                        }]
                    },
                    options: {
                        responsive: true,
                        interaction: { intersect: false, mode: 'index' },
                        scales: {
                            y: { type: 'linear', position: 'left', title: { display: true, text: 'Tool Calls' } },
                            y1: { type: 'linear', position: 'right', title: { display: true, text: 'Tokens' }, grid: { drawOnChartArea: false } },
                        }
                    }
                });
            }

            async function loadDocs() {
                try {
                    const r = await fetch('/v1/documents');
                    const d = await r.json();
                    const el = document.getElementById('docList');
                    if (!d.documents || d.documents.length === 0) {
                        el.innerHTML = '<div class="empty">No documents uploaded yet. Use POST /v1/documents to upload.</div>';
                        return;
                    }
                    let html = '<table><tr><th>ID</th><th>Title</th><th>Status</th><th>Chunks</th><th>Created</th><th>Actions</th></tr>';
                    for (const doc of d.documents) {
                        html += `<tr>
                            <td><code>${doc.id}</code></td>
                            <td>${doc.title}</td>
                            <td><span class="status status-${doc.status}">${doc.status}</span></td>
                            <td>-</td>
                            <td>${new Date(doc.created_at).toLocaleDateString()}</td>
                            <td>
                                <button class="btn btn-primary" onclick="viewEntities('${doc.id}','${doc.title}')">Entities</button>
                                <button class="btn btn-danger" onclick="deleteDoc('${doc.id}')">Delete</button>
                            </td>
                        </tr>`;
                    }
                    html += '</table>';
                    el.innerHTML = html;
                } catch(e) { console.error(e); }
            }

            async function viewEntities(docId, title) {
                const panel = document.getElementById('entityPanel');
                const list = document.getElementById('entityList');
                document.getElementById('entityDocName').textContent = title;
                panel.style.display = 'block';
                list.innerHTML = '<div class="empty">Loading...</div>';
                try {
                    const r = await fetch('/v1/documents/' + docId + '/entities');
                    const d = await r.json();
                    if (!d.entities || d.entities.length === 0) {
                        list.innerHTML = '<div class="empty">No entities found for this document.</div>';
                        return;
                    }
                    const grouped = {};
                    for (const e of d.entities) {
                        if (!grouped[e.type]) grouped[e.type] = [];
                        grouped[e.type].push(e.name);
                    }
                    let html = '';
                    for (const [type, names] of Object.entries(grouped)) {
                        html += '<div style="margin-bottom:8px;"><strong style="text-transform:uppercase;font-size:12px;color:#6b7280;">' + type + ' (' + names.length + ')</strong><div class="entity-grid">';
                        for (const name of names) {
                            html += '<span class="entity-tag entity-' + type + '">' + name + '</span>';
                        }
                        html += '</div></div>';
                    }
                    if (d.relations && d.relations.length > 0) {
                        html += '<h3 style="margin-top:16px;font-size:14px;color:#374151;">Relations (' + d.relations.length + ')</h3><table style="margin-top:8px;"><tr><th>From</th><th>Relation</th><th>To</th></tr>';
                        for (const rel of d.relations) {
                            html += '<tr><td>' + rel.from_name + '</td><td><code>' + rel.relation + '</code></td><td>' + rel.to_name + '</td></tr>';
                        }
                        html += '</table>';
                    }
                    list.innerHTML = html;
                } catch(e) { list.innerHTML = '<div class="empty">Error loading entities.</div>'; }
            }

            async function deleteDoc(docId) {
                if (!confirm('Delete document ' + docId + '?')) return;
                try {
                    await fetch('/v1/documents/' + docId, { method: 'DELETE' });
                    loadDocs();
                    document.getElementById('entityPanel').style.display = 'none';
                } catch(e) { alert('Delete failed: ' + e.message); }
            }

            loadStats();
            loadDocs();
            setInterval(loadStats, 30000);
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8501)