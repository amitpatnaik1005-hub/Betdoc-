"""smart order router: routed orders, venue slices, venue circuit breakers (Group 71)

Revision ID: d71e5a0c2b19
Revises: c4138a07e989
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd71e5a0c2b19'
down_revision: Union[str, Sequence[str], None] = 'c4138a07e989'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_ORDER_STATUSES = "'ROUTING', 'RESERVED', 'DISPATCHING', 'FILLED', 'PARTIAL', 'UNCONFIRMED', 'REJECTED', 'ABORTED'"
_SLICE_STATUSES = "'RESERVED', 'DISPATCHED', 'FILLED', 'PARTIAL', 'REJECTED', 'UNKNOWN', 'RELEASED'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('routed_orders',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('order_id', sa.String(length=96), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('match_id', sa.String(length=128), nullable=False),
    sa.Column('market', sa.String(length=64), nullable=False),
    sa.Column('selection', sa.String(length=128), nullable=False),
    sa.Column('odds', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('min_acceptable_odds', sa.Numeric(precision=10, scale=4), nullable=False),
    sa.Column('max_slippage_pct', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('true_prob', sa.Numeric(precision=8, scale=6), nullable=True),
    sa.Column('desired_total_stake', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('target_bookmakers', _JSON, nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('filled_stake', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('blended_odds', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('reason', sa.String(length=64), nullable=True),
    sa.Column('detail', _JSON, nullable=False),
    sa.Column('hedge_state', sa.String(length=16), nullable=True),
    sa.Column('receipt_sha256', sa.String(length=64), nullable=True),
    sa.Column('nalanda_seq', sa.Integer(), nullable=True),
    sa.Column('nalanda_hash', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('desired_total_stake > 0', name=op.f('ck_routed_orders_stake_positive')),
    sa.CheckConstraint('odds > 1 AND min_acceptable_odds > 1 AND min_acceptable_odds <= odds', name=op.f('ck_routed_orders_price_floor')),
    sa.CheckConstraint('max_slippage_pct >= 0 AND max_slippage_pct <= 50', name=op.f('ck_routed_orders_slippage_bounded')),
    sa.CheckConstraint('filled_stake >= 0 AND filled_stake <= desired_total_stake', name=op.f('ck_routed_orders_filled_bounded')),
    sa.CheckConstraint(f'status IN ({_ORDER_STATUSES})', name=op.f('ck_routed_orders_status_known')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_routed_orders')),
    sa.UniqueConstraint('order_id', name=op.f('uq_routed_orders_order_id'))
    )
    op.create_index('ix_routed_orders_status_created', 'routed_orders', ['status', 'created_at'], unique=False)
    op.create_index('ix_routed_orders_user_created', 'routed_orders', ['user_id', 'created_at'], unique=False)

    op.create_table('routed_order_slices',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('routed_order_id', sa.Uuid(), nullable=False),
    sa.Column('slice_index', sa.Integer(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=192), nullable=False),
    sa.Column('client_ref', sa.Uuid(), nullable=False),
    sa.Column('venue_id', sa.String(length=64), nullable=False),
    sa.Column('account_id', sa.Uuid(), nullable=True),
    sa.Column('stake', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('quoted_odds', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('guard_odds', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('commission', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('net_ev', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('filled_stake', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('matched_odds', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('remote_bet_id', sa.String(length=128), nullable=True),
    sa.Column('ledger_id', sa.Uuid(), nullable=True),
    sa.Column('reason', sa.String(length=64), nullable=True),
    sa.Column('reserved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('stake > 0', name=op.f('ck_routed_order_slices_stake_positive')),
    sa.CheckConstraint('filled_stake >= 0 AND filled_stake <= stake', name=op.f('ck_routed_order_slices_filled_bounded')),
    sa.CheckConstraint(f'status IN ({_SLICE_STATUSES})', name=op.f('ck_routed_order_slices_status_known')),
    sa.ForeignKeyConstraint(['routed_order_id'], ['routed_orders.id'], name=op.f('fk_routed_order_slices_routed_order_id_routed_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['account_id'], ['vault_bookmaker_accounts.id'], name=op.f('fk_routed_order_slices_account_id_vault_bookmaker_accounts'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_routed_order_slices')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_routed_order_slices_idempotency_key')),
    sa.UniqueConstraint('client_ref', name=op.f('uq_routed_order_slices_client_ref')),
    sa.UniqueConstraint('routed_order_id', 'slice_index', name='uq_routed_order_slices_index')
    )
    op.create_index(op.f('ix_routed_order_slices_routed_order_id'), 'routed_order_slices', ['routed_order_id'], unique=False)
    op.create_index('ix_routed_order_slices_status_venue', 'routed_order_slices', ['status', 'venue_id'], unique=False)

    op.create_table('venue_circuit_breakers',
    sa.Column('venue_id', sa.String(length=64), nullable=False),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('streak_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paused_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trips', sa.Integer(), nullable=False),
    sa.Column('last_failure_reason', sa.Text(), nullable=True),
    sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('consecutive_failures >= 0 AND trips >= 0', name=op.f('ck_venue_circuit_breakers_counts_non_negative')),
    sa.PrimaryKeyConstraint('venue_id', name=op.f('pk_venue_circuit_breakers'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('venue_circuit_breakers')
    op.drop_index('ix_routed_order_slices_status_venue', table_name='routed_order_slices')
    op.drop_index(op.f('ix_routed_order_slices_routed_order_id'), table_name='routed_order_slices')
    op.drop_table('routed_order_slices')
    op.drop_index('ix_routed_orders_user_created', table_name='routed_orders')
    op.drop_index('ix_routed_orders_status_created', table_name='routed_orders')
    op.drop_table('routed_orders')
