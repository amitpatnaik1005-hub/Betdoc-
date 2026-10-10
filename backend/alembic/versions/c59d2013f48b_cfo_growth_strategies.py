"""cfo growth strategies: forecasts, KUMBHA's regime advisories, venue rebalancing plans (Group 76)

Revision ID: c59d2013f48b
Revises: b48c1f92e35a
Create Date: 2026-10-10 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c59d2013f48b'
down_revision: Union[str, Sequence[str], None] = 'b48c1f92e35a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_SEVERITIES = "'INFO', 'RECOMMENDATION', 'WARNING', 'CRITICAL'"
_INSIGHTS = "'OPTIMAL_GROWTH_TRAJECTORY', 'VARIANCE_THROTTLE', 'CAPITAL_PRESERVATION_HALT', 'VENUE_REBALANCE'"
_REBALANCE = "'PENDING', 'APPROVED', 'EXECUTED', 'DISMISSED', 'SUPERSEDED'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('cfo_growth_simulations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('strategy', sa.String(length=32), nullable=False),
    sa.Column('horizon_days', sa.Integer(), nullable=False),
    sa.Column('simulated_paths', sa.Integer(), nullable=False),
    sa.Column('trades', sa.Integer(), nullable=False),
    sa.Column('seed', sa.BigInteger(), nullable=False),
    sa.Column('starting_bankroll_inr', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('median_ending_bankroll_inr', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('mean_ending_bankroll_inr', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('expected_cagr_pct', sa.Float(), nullable=False),
    sa.Column('sharpe_ratio', sa.Float(), nullable=True),
    sa.Column('sortino_ratio', sa.Float(), nullable=True),
    sa.Column('prob_circuit_breaker', sa.Float(), nullable=False),
    sa.Column('prob_ruin', sa.Float(), nullable=False),
    sa.Column('median_max_drawdown', sa.Float(), nullable=False),
    sa.Column('p95_max_drawdown', sa.Float(), nullable=False),
    sa.Column('percentile_curves', _JSON, nullable=False),
    sa.Column('parameters', _JSON, nullable=False),
    sa.Column('developer_credit', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('horizon_days > 0 AND simulated_paths > 0 AND trades > 0', name=op.f('ck_cfo_growth_simulations_sizes_positive')),
    sa.CheckConstraint('prob_circuit_breaker >= 0 AND prob_circuit_breaker <= 1 AND prob_ruin >= 0 AND prob_ruin <= 1', name=op.f('ck_cfo_growth_simulations_probabilities_bounded')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_cfo_growth_simulations_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cfo_growth_simulations'))
    )
    op.create_index('ix_cfo_growth_simulations_user_created', 'cfo_growth_simulations', ['user_id', 'created_at'], unique=False)

    op.create_table('cfo_advisory_logs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('insight_code', sa.String(length=32), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('regime', sa.String(length=40), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('action_directive', sa.String(length=255), nullable=True),
    sa.Column('metrics_snapshot', _JSON, nullable=False),
    sa.Column('is_acknowledged', sa.Boolean(), nullable=False),
    sa.Column('acknowledged_by', sa.Uuid(), nullable=True),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('acknowledgement_note', sa.Text(), nullable=True),
    sa.Column('developer_credit', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(f'insight_code IN ({_INSIGHTS})', name=op.f('ck_cfo_advisory_logs_insight_known')),
    sa.CheckConstraint(f'severity IN ({_SEVERITIES})', name=op.f('ck_cfo_advisory_logs_severity_known')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_cfo_advisory_logs_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cfo_advisory_logs'))
    )
    op.create_index('ix_cfo_advisory_logs_user_code_ack', 'cfo_advisory_logs', ['user_id', 'insight_code', 'is_acknowledged'], unique=False)
    op.create_index('ix_cfo_advisory_logs_user_created', 'cfo_advisory_logs', ['user_id', 'created_at'], unique=False)

    op.create_table('cfo_rebalance_recommendations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('plan_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('source_venue', sa.String(length=32), nullable=False),
    sa.Column('destination_venue', sa.String(length=32), nullable=False),
    sa.Column('amount_inr', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('source_balance_before', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('dest_balance_before', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('source_target', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('dest_target', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('status_changed_by', sa.Uuid(), nullable=True),
    sa.Column('status_note', sa.Text(), nullable=True),
    sa.Column('status_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('developer_credit', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('amount_inr > 0', name=op.f('ck_cfo_rebalance_recommendations_amount_positive')),
    sa.CheckConstraint('source_venue <> destination_venue', name=op.f('ck_cfo_rebalance_recommendations_distinct_venues')),
    sa.CheckConstraint(f'status IN ({_REBALANCE})', name=op.f('ck_cfo_rebalance_recommendations_status_known')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_cfo_rebalance_recommendations_created_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cfo_rebalance_recommendations'))
    )
    op.create_index('ix_cfo_rebalance_recommendations_plan', 'cfo_rebalance_recommendations', ['plan_id'], unique=False)
    op.create_index('ix_cfo_rebalance_recommendations_status_created', 'cfo_rebalance_recommendations', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_cfo_rebalance_recommendations_status_created', table_name='cfo_rebalance_recommendations')
    op.drop_index('ix_cfo_rebalance_recommendations_plan', table_name='cfo_rebalance_recommendations')
    op.drop_table('cfo_rebalance_recommendations')
    op.drop_index('ix_cfo_advisory_logs_user_created', table_name='cfo_advisory_logs')
    op.drop_index('ix_cfo_advisory_logs_user_code_ack', table_name='cfo_advisory_logs')
    op.drop_table('cfo_advisory_logs')
    op.drop_index('ix_cfo_growth_simulations_user_created', table_name='cfo_growth_simulations')
    op.drop_table('cfo_growth_simulations')
