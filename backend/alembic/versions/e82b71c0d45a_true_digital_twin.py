"""true digital betting twin: 14-pillar vetting audits, in-play monitors, booking codes on placed bets (Group 72)

Revision ID: e82b71c0d45a
Revises: d71e5a0c2b19
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e82b71c0d45a'
down_revision: Union[str, Sequence[str], None] = 'd71e5a0c2b19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_PULLOUT_REASONS = "'TARGET_PROFIT_REACHED', 'PROBABILITY_COLLAPSE', 'HEDGE_LOCK', 'CASHOUT_ADVISED', 'MANUAL_USER_REQUEST'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('twin_vetting_audits',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('slip_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('leg_ids', _JSON, nullable=False),
    sa.Column('bookmaker', sa.String(length=32), nullable=True),
    sa.Column('total_odds', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('stake_inr', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('bankroll_inr', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('kelly_fraction', sa.Float(), nullable=False),
    sa.Column('joint_ev', sa.Float(), nullable=True),
    sa.Column('joint_probability', sa.Float(), nullable=True),
    sa.Column('consensus_ev', sa.Float(), nullable=True),
    sa.Column('sharp_edge', sa.Float(), nullable=True),
    sa.Column('pillars_passed', sa.Integer(), nullable=False),
    sa.Column('conviction_score', sa.Float(), nullable=False),
    sa.Column('is_vetted', sa.Boolean(), nullable=False),
    sa.Column('pillars', _JSON, nullable=False),
    sa.Column('rejection_reasons', _JSON, nullable=False),
    sa.Column('slip', _JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('pillars_passed >= 0 AND pillars_passed <= 14', name=op.f('ck_twin_vetting_audits_pillars_bounded')),
    sa.CheckConstraint('conviction_score >= 0 AND conviction_score <= 100', name=op.f('ck_twin_vetting_audits_conviction_bounded')),
    sa.CheckConstraint('stake_inr >= 0', name=op.f('ck_twin_vetting_audits_stake_not_negative')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_twin_vetting_audits_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_twin_vetting_audits'))
    )
    op.create_index('ix_twin_vetting_audits_user_created', 'twin_vetting_audits', ['user_id', 'created_at'], unique=False)
    op.create_index('ix_twin_vetting_audits_vetted_created', 'twin_vetting_audits', ['is_vetted', 'created_at'], unique=False)
    op.create_index('ix_twin_vetting_audits_slip', 'twin_vetting_audits', ['slip_id'], unique=False)

    op.create_table('twin_inplay_monitors',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('bet_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('vetting_audit_id', sa.Uuid(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('target_profit_pct', sa.Float(), nullable=False),
    sa.Column('initial_win_prob', sa.Float(), nullable=False),
    sa.Column('current_win_prob', sa.Float(), nullable=False),
    sa.Column('fair_value_inr', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('peak_fair_value_inr', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('cashout_offer_inr', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('last_advice', sa.String(length=16), nullable=True),
    sa.Column('last_tick_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ticks', sa.Integer(), nullable=False),
    sa.Column('pullout_triggered', sa.Boolean(), nullable=False),
    sa.Column('pullout_reason', sa.String(length=32), nullable=True),
    sa.Column('pullout_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('detail', _JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('initial_win_prob >= 0 AND initial_win_prob <= 1', name=op.f('ck_twin_inplay_monitors_initial_prob_bounded')),
    sa.CheckConstraint('current_win_prob >= 0 AND current_win_prob <= 1', name=op.f('ck_twin_inplay_monitors_current_prob_bounded')),
    sa.CheckConstraint(f'pullout_reason IS NULL OR pullout_reason IN ({_PULLOUT_REASONS})', name=op.f('ck_twin_inplay_monitors_pullout_reason_known')),
    sa.ForeignKeyConstraint(['bet_id'], ['user_placed_bets.id'], name=op.f('fk_twin_inplay_monitors_bet_id_user_placed_bets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_twin_inplay_monitors_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vetting_audit_id'], ['twin_vetting_audits.id'], name=op.f('fk_twin_inplay_monitors_vetting_audit_id_twin_vetting_audits'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_twin_inplay_monitors')),
    sa.UniqueConstraint('bet_id', name=op.f('uq_twin_inplay_monitors_bet_id'))
    )
    op.create_index('ix_twin_inplay_monitors_active', 'twin_inplay_monitors', ['is_active', 'last_tick_at'], unique=False)
    op.create_index('ix_twin_inplay_monitors_user', 'twin_inplay_monitors', ['user_id', 'created_at'], unique=False)

    op.add_column('user_placed_bets', sa.Column('vetting_audit_id', sa.Uuid(), nullable=True))
    op.add_column('user_placed_bets', sa.Column('booking_code', sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_placed_bets', 'booking_code')
    op.drop_column('user_placed_bets', 'vetting_audit_id')
    op.drop_index('ix_twin_inplay_monitors_user', table_name='twin_inplay_monitors')
    op.drop_index('ix_twin_inplay_monitors_active', table_name='twin_inplay_monitors')
    op.drop_table('twin_inplay_monitors')
    op.drop_index('ix_twin_vetting_audits_slip', table_name='twin_vetting_audits')
    op.drop_index('ix_twin_vetting_audits_vetted_created', table_name='twin_vetting_audits')
    op.drop_index('ix_twin_vetting_audits_user_created', table_name='twin_vetting_audits')
    op.drop_table('twin_vetting_audits')
