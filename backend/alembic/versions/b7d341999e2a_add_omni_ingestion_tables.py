"""Add Omni-Ingestion tables

Revision ID: b7d341999e2a
Revises: 0cf956003817
Create Date: 2026-10-01 23:58:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7d341999e2a'
down_revision: Union[str, None] = '0cf956003817'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. OmniProviderConfig
    op.create_table(
        'omni_provider_configs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('provider_name', sa.String(length=128), nullable=False),
        sa.Column('category_code', sa.String(length=1), nullable=False),
        sa.Column('base_url', sa.Text(), nullable=False),
        sa.Column('ws_url', sa.Text(), nullable=True),
        sa.Column('auth_strategy', sa.String(length=16), nullable=False),
        sa.Column('auth_param_name', sa.String(length=128), nullable=True),
        sa.Column('encrypted_api_key', sa.Text(), nullable=True),
        sa.Column('api_key_hint', sa.String(length=64), nullable=True),
        sa.Column('ws_auth_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('ws_subscribe_payloads', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('default_headers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('requests_per_minute', sa.Integer(), nullable=False),
        sa.Column('timeout_seconds', sa.Float(), nullable=True),
        sa.Column('queue_name', sa.String(length=64), nullable=True),
        sa.Column('adapter_key', sa.String(length=64), nullable=True),
        sa.Column('normalization_spec', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('health_status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("category_code IN ('A','B','C','D','E','F','G','H')", name='ck_omni_provider_category'),
        sa.CheckConstraint('requests_per_minute > 0', name='ck_omni_provider_rpm_positive'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_omni_provider_configs_category_code'), 'omni_provider_configs', ['category_code'], unique=False)
    op.create_index(op.f('ix_omni_provider_configs_is_active'), 'omni_provider_configs', ['is_active'], unique=False)
    op.create_index(op.f('ix_omni_provider_configs_provider_name'), 'omni_provider_configs', ['provider_name'], unique=True)

    # 2. OmniProviderEndpoint
    op.create_table(
        'omni_provider_endpoints',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('provider_id', sa.Uuid(), nullable=False),
        sa.Column('path', sa.Text(), nullable=False),
        sa.Column('http_method', sa.String(length=8), nullable=False),
        sa.Column('query_params', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('request_body', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('topic', sa.String(length=128), nullable=True),
        sa.Column('min_interval_seconds', sa.Float(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("http_method IN ('GET','POST')", name='ck_omni_endpoint_method'),
        sa.ForeignKeyConstraint(['provider_id'], ['omni_provider_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_id', 'http_method', 'path', name='uq_omni_endpoint')
    )
    op.create_index(op.f('ix_omni_provider_endpoints_provider_id'), 'omni_provider_endpoints', ['provider_id'], unique=False)
    op.create_index(op.f('ix_omni_provider_endpoints_topic'), 'omni_provider_endpoints', ['topic'], unique=False)

    # 3. OmniRawPayload
    op.create_table(
        'omni_raw_payloads',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('provider_id', sa.Uuid(), nullable=False),
        sa.Column('endpoint_id', sa.Uuid(), nullable=True),
        sa.Column('endpoint_path', sa.Text(), nullable=False),
        sa.Column('transport', sa.String(length=8), nullable=False),
        sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('is_json', sa.Boolean(), nullable=False),
        sa.Column('content_type', sa.String(length=255), nullable=True),
        sa.Column('payload_hash', sa.String(length=64), nullable=False),
        sa.Column('source_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_timestamp_origin', sa.String(length=16), nullable=False),
        sa.Column('ingestion_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['endpoint_id'], ['omni_provider_endpoints.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['provider_id'], ['omni_provider_configs.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_omni_raw_payloads_ingestion_timestamp'), 'omni_raw_payloads', ['ingestion_timestamp'], unique=False)
    op.create_index(op.f('ix_omni_raw_payloads_payload_hash'), 'omni_raw_payloads', ['payload_hash'], unique=False)
    op.create_index('ix_omni_raw_provider_ingested', 'omni_raw_payloads', ['provider_id', 'ingestion_timestamp'], unique=False)

    # 4. OmniQuarantineLog
    op.create_table(
        'omni_quarantine_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('topic', sa.String(length=256), nullable=False),
        sa.Column('reason', sa.String(length=64), nullable=False),
        sa.Column('variance', sa.Float(), nullable=True),
        sa.Column('threshold', sa.Float(), nullable=True),
        sa.Column('events', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved', sa.Boolean(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_note', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_omni_quarantine_logs_created_at'), 'omni_quarantine_logs', ['created_at'], unique=False)
    op.create_index(op.f('ix_omni_quarantine_logs_resolved'), 'omni_quarantine_logs', ['resolved'], unique=False)
    op.create_index(op.f('ix_omni_quarantine_logs_topic'), 'omni_quarantine_logs', ['topic'], unique=False)

    # Immutable trigger function (PostgreSQL only)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION omni_raw_payload_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'omni_raw_payloads is append-only (% blocked)', TG_OP;
        END;
        $$;
        """
    )
    
    op.execute(
        """
        CREATE TRIGGER trg_omni_raw_payload_immutable BEFORE UPDATE OR DELETE ON omni_raw_payloads
        FOR EACH ROW EXECUTE FUNCTION omni_raw_payload_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_omni_raw_payload_immutable ON omni_raw_payloads;")
    op.execute("DROP FUNCTION IF EXISTS omni_raw_payload_immutable();")
    
    op.drop_table('omni_quarantine_logs')
    op.drop_table('omni_raw_payloads')
    op.drop_table('omni_provider_endpoints')
    op.drop_table('omni_provider_configs')
