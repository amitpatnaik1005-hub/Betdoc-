"""model recalibration engine: recalibration runs and per-model weight audits (Group 74)

Revision ID: a17e92c4b50d
Revises: f93c8b1a0d2e
Create Date: 2026-10-10 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a17e92c4b50d'
down_revision: Union[str, Sequence[str], None] = 'f93c8b1a0d2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_TRIGGERS = "'SCHEDULED', 'ON_DEMAND_ADMIN', 'LOSS_THRESHOLD_TRIGGER', 'MANUAL_OVERRIDE', 'EMERGENCY_RESET'"
_STATUSES = "'ALPHA_BOOSTED', 'ACTIVE', 'PROBATION', 'BENCHED'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('model_recalibration_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('trigger_type', sa.String(length=32), nullable=False),
    sa.Column('triggered_by', sa.Uuid(), nullable=True),
    sa.Column('models_evaluated', sa.Integer(), nullable=False),
    sa.Column('models_promoted', sa.Integer(), nullable=False),
    sa.Column('models_demoted', sa.Integer(), nullable=False),
    sa.Column('benchmark_model', sa.String(length=32), nullable=False),
    sa.Column('benchmark_brier', sa.Float(), nullable=True),
    sa.Column('published', sa.Boolean(), nullable=False),
    sa.Column('published_weights', _JSON, nullable=False),
    sa.Column('parameters', _JSON, nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('developer_credit', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(f'trigger_type IN ({_TRIGGERS})', name=op.f('ck_model_recalibration_runs_trigger_known')),
    sa.CheckConstraint('models_evaluated >= 0 AND models_promoted >= 0 AND models_demoted >= 0', name=op.f('ck_model_recalibration_runs_counts_not_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_recalibration_runs'))
    )
    op.create_index('ix_model_recalibration_runs_created', 'model_recalibration_runs', ['created_at'], unique=False)

    op.create_table('model_weight_audits',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('model_name', sa.String(length=32), nullable=False),
    sa.Column('sample_count', sa.Integer(), nullable=False),
    sa.Column('sample_count_30d', sa.Integer(), nullable=False),
    sa.Column('paired_count', sa.Integer(), nullable=False),
    sa.Column('brier_score_30d', sa.Float(), nullable=True),
    sa.Column('brier_score_90d', sa.Float(), nullable=True),
    sa.Column('brier_decayed', sa.Float(), nullable=True),
    sa.Column('brier_skill_score', sa.Float(), nullable=True),
    sa.Column('avg_clv_pct', sa.Float(), nullable=True),
    sa.Column('reliability', sa.Float(), nullable=True),
    sa.Column('resolution', sa.Float(), nullable=True),
    sa.Column('uncertainty', sa.Float(), nullable=True),
    sa.Column('previous_weight', sa.Float(), nullable=True),
    sa.Column('new_weight', sa.Float(), nullable=False),
    sa.Column('previous_status', sa.String(length=16), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('status_reason', sa.Text(), nullable=False),
    sa.Column('metrics_snapshot', _JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('new_weight >= 0', name=op.f('ck_model_weight_audits_new_weight_not_negative')),
    sa.CheckConstraint('previous_weight IS NULL OR previous_weight >= 0', name=op.f('ck_model_weight_audits_previous_weight_not_negative')),
    sa.CheckConstraint(f'status IN ({_STATUSES})', name=op.f('ck_model_weight_audits_status_known')),
    sa.ForeignKeyConstraint(['run_id'], ['model_recalibration_runs.id'], name=op.f('fk_model_weight_audits_run_id_model_recalibration_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_weight_audits'))
    )
    op.create_index('ix_model_weight_audits_model_created', 'model_weight_audits', ['model_name', 'created_at'], unique=False)
    op.create_index('ix_model_weight_audits_run', 'model_weight_audits', ['run_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_model_weight_audits_run', table_name='model_weight_audits')
    op.drop_index('ix_model_weight_audits_model_created', table_name='model_weight_audits')
    op.drop_table('model_weight_audits')
    op.drop_index('ix_model_recalibration_runs_created', table_name='model_recalibration_runs')
    op.drop_table('model_recalibration_runs')
