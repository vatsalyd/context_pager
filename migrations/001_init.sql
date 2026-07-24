-- Context Pager Initial Schema (extracted from migrations/versions/001_initial_schema.py)
-- Run against the pager database inside the postgres container:
--   docker-compose exec postgres psql -U pager -d pager -f /docker-entrypoint-initdb.d/001_init.sql

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users / API Keys
CREATE TABLE IF NOT EXISTS users (
    id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           text UNIQUE NOT NULL,
    hashed_api_key  text NOT NULL,
    api_key_prefix  text NOT NULL,
    plan            text NOT NULL DEFAULT 'free',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_prefix_idx ON users (api_key_prefix);

-- Documents
CREATE TABLE IF NOT EXISTS documents (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL,
    title           text NOT NULL,
    content         text NOT NULL,
    source_kind     text NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'processing',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS documents_tenant_idx ON documents (tenant_id);

-- Document Chunks (with dense + sparse embeddings)
CREATE TABLE IF NOT EXISTS document_chunks (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    document_id     text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     int NOT NULL,
    text            text NOT NULL,
    embedding       vector(1024) NOT NULL,
    sparse_weights  sparsevec,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS document_chunks_tenant_idx ON document_chunks (tenant_id);
CREATE INDEX IF NOT EXISTS document_chunks_doc_idx ON document_chunks (document_id);

-- Entities (GraphRAG nodes)
CREATE TABLE IF NOT EXISTS entities (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    document_id     text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    type            text NOT NULL,
    name            text NOT NULL,
    properties      jsonb NOT NULL DEFAULT '{}',
    embedding       vector(1024),
    sparse_weights  sparsevec,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entities_tenant_idx ON entities (tenant_id);
CREATE INDEX IF NOT EXISTS entities_doc_idx ON entities (document_id);

-- Entity Relations (GraphRAG edges)
CREATE TABLE IF NOT EXISTS entity_relations (
    from_id         bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id           bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation        text NOT NULL,
    properties      jsonb NOT NULL DEFAULT '{}',
    tenant_id       text NOT NULL,
    PRIMARY KEY (from_id, to_id, relation)
);
CREATE INDEX IF NOT EXISTS entity_relations_tenant_idx ON entity_relations (tenant_id);

-- Agent Long-Term Memory
CREATE TABLE IF NOT EXISTS agent_memory (
    key             text PRIMARY KEY,
    tenant_id       text NOT NULL,
    insights        text NOT NULL,
    embedding       vector(1024) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_recalled   timestamptz
);
CREATE INDEX IF NOT EXISTS agent_memory_tenant_idx ON agent_memory (tenant_id);

-- Audit Events
CREATE TABLE IF NOT EXISTS audit_events (
    id              bigserial PRIMARY KEY,
    tenant_id       text NOT NULL,
    event_type      text NOT NULL,
    tool_name       text,
    session_id      text,
    doc_id          text,
    original_tokens int,
    compressed_tokens int,
    cost_saved_usd  numeric(10, 4),
    metadata        jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_events_tenant_created_idx ON audit_events (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS audit_events_tool_created_idx ON audit_events (tool_name, created_at);

-- Daily Usage Rollups
CREATE TABLE IF NOT EXISTS tenant_usage_daily (
    date            date NOT NULL,
    tenant_id       text NOT NULL,
    tool_calls      int NOT NULL DEFAULT 0,
    tokens_compressed bigint NOT NULL DEFAULT 0,
    storage_bytes   bigint NOT NULL DEFAULT 0,
    est_cost_usd    numeric(10, 4) NOT NULL DEFAULT 0,
    PRIMARY KEY (date, tenant_id)
);
CREATE INDEX IF NOT EXISTS tenant_usage_daily_date_idx ON tenant_usage_daily (date);

-- HNSW indexes (created after initial schema; build time scales with data)
CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS entities_embedding_idx
    ON entities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Row-Level Security (multi-tenant isolation per Q22)
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_usage_daily ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t text;
BEGIN
    FOR t IN SELECT unnest(ARRAY['documents','document_chunks','entities','entity_relations','agent_memory','audit_events','tenant_usage_daily'])
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true))', t);
    END LOOP;
END$$;

-- Mark alembic version so the migration framework knows Phase 001 has been applied
CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version (version_num) VALUES ('001') ON CONFLICT DO NOTHING;
