"""Initial schema with pgvector, RLS, and multi-tenant isolation

Revision ID: 001
Revises: 
Create Date: 2024-01-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable extensions
    op.execute('CREATE EXTENSION IF NOT EXISTS vector;')
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')

    # Users / API Keys
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('uuid_generate_v4()')),
        sa.Column('email', sa.Text(), unique=True, nullable=False),
        sa.Column('hashed_api_key', sa.Text(), nullable=False),
        sa.Column('api_key_prefix', sa.Text(), nullable=False),
        sa.Column('plan', sa.Text(), nullable=False, server_default='free'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('users_prefix_idx', 'users', ['api_key_prefix'])

    # Documents
    op.create_table(
        'documents',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('source_kind', sa.Text(), nullable=False),
        sa.Column('metadata', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('status', sa.Text(), nullable=False, server_default='processing'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('documents_tenant_idx', 'documents', ['tenant_id'])

    # Document Chunks + Dense/Sparse Embeddings
    op.create_table(
        'document_chunks',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('document_id', sa.Text(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('embedding', postgresql.VECTOR(1024), nullable=False),
        sa.Column('sparse_weights', sa.Text(), nullable=True),  # pgvector sparsevec or jsonb fallback
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('document_chunks_tenant_idx', 'document_chunks', ['tenant_id'])
    op.create_index('document_chunks_doc_idx', 'document_chunks', ['document_id'])
    op.create_unique_constraint('document_chunks_doc_chunk_uq', 'document_chunks', ['document_id', 'chunk_index'])

    # HNSW indexes for dense + sparse (created after data load)
    op.execute("""
        CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
        ON document_chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS document_chunks_sparse_idx
        ON document_chunks USING hnsw (sparse_weights sparsevec_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)

    # Entities (GraphRAG nodes)
    op.create_table(
        'entities',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('document_id', sa.Text(), nullable=False),
        sa.Column('type', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('properties', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('embedding', postgresql.VECTOR(1024), nullable=True),
        sa.Column('sparse_weights', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('entities_tenant_idx', 'entities', ['tenant_id'])
    op.create_index('entities_doc_idx', 'entities', ['document_id'])

    op.execute("""
        CREATE INDEX IF NOT EXISTS entities_embedding_idx
        ON entities USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS entities_sparse_idx
        ON entities USING hnsw (sparse_weights sparsevec_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)

    # Entity Relations (GraphRAG edges)
    op.create_table(
        'entity_relations',
        sa.Column('from_id', sa.BigInteger(), nullable=False),
        sa.Column('to_id', sa.BigInteger(), nullable=False),
        sa.Column('relation', sa.Text(), nullable=False),
        sa.Column('properties', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('from_id', 'to_id', 'relation', name='entity_relations_pkey'),
        sa.ForeignKeyConstraint(['from_id'], ['entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['to_id'], ['entities.id'], ondelete='CASCADE'),
    )
    op.create_index('entity_relations_tenant_idx', 'entity_relations', ['tenant_id'])

    # Agent Long-Term Memory
    op.create_table(
        'agent_memory',
        sa.Column('key', sa.Text(), primary_key=True),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('insights', sa.Text(), nullable=False),
        sa.Column('embedding', postgresql.VECTOR(1024), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('last_recalled', sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index('agent_memory_tenant_idx', 'agent_memory', ['tenant_id'])

    # Audit Events
    op.create_table(
        'audit_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('event_type', sa.Text(), nullable=False),
        sa.Column('tool_name', sa.Text(), nullable=True),
        sa.Column('session_id', sa.Text(), nullable=True),
        sa.Column('doc_id', sa.Text(), nullable=True),
        sa.Column('original_tokens', sa.Integer(), nullable=True),
        sa.Column('compressed_tokens', sa.Integer(), nullable=True),
        sa.Column('cost_saved_usd', sa.Numeric(10, 4), nullable=True),
        sa.Column('metadata', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('audit_events_tenant_created_idx', 'audit_events', ['tenant_id', 'created_at'])
    op.create_index('audit_events_tool_created_idx', 'audit_events', ['tool_name', 'created_at'])

    # Daily Usage Rollups
    op.create_table(
        'tenant_usage_daily',
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('tool_calls', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_compressed', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('storage_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('est_cost_usd', sa.Numeric(10, 4), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('date', 'tenant_id', name='tenant_usage_daily_pkey'),
    )
    op.create_index('tenant_usage_daily_date_idx', 'tenant_usage_daily', ['date'])

    # Row Level Security
    for table in ['documents', 'document_chunks', 'entities', 'entity_relations', 
                  'agent_memory', 'audit_events', 'tenant_usage_daily']:
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;')
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true));
        """)


def downgrade() -> None:
    for table in ['tenant_usage_daily', 'audit_events', 'agent_memory', 'entity_relations', 
                  'entities', 'document_chunks', 'documents', 'users']:
        op.execute(f'DROP TABLE IF EXISTS {table} CASCADE;')
    op.execute('DROP EXTENSION IF EXISTS vector;')