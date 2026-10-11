"""add real money execution tables (Group 95)

The Live Real-Money Desk and TODAR MAL's cash ledger. Developed for Amit Ashok Kumar Patnaik.

* real_bankrolls: one real cash vault per user in INR, liquid cash, locked exposure, realized P&L,
  daily drawdown latch (15%) and cool-down timer.
* real_bet_tickets: assisted execution tickets built for Parimatch (or 1xBet), target odds, minimum floor,
  Kelly stake, and settlement with Section 115BBJ 30% TDS.
* real_cash_transactions: immutable double-entry ledger for every liquid cash movement.

Revision ID: b94f1308e05a
Revises: a83e1207d94f
Create Date: 2026-10-11 14:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b94f1308e05a'
down_revision: Union[str, Sequence[str], None] = 'a83e1207d94f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. real_bankrolls
    op.create_table(
        'real_bankrolls',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('cash_balance', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('locked_exposure', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('total_deposited', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('total_withdrawn', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('realized_pnl', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('gross_winnings', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('total_tds_deducted', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('high_water_mark', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('daily_starting_balance', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('daily_realized_loss', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('consecutive_losses', sa.Integer(), nullable=False),
        sa.Column('stop_loss_triggered', sa.Boolean(), nullable=False),
        sa.Column('stop_loss_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cooldown_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('trading_day', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('cash_balance >= 0', name='ck_real_bankrolls_cash_not_negative'),
        sa.CheckConstraint('locked_exposure >= 0', name='ck_real_bankrolls_exposure_not_negative'),
        sa.CheckConstraint("currency = 'INR'", name='ck_real_bankrolls_canonical_inr'),
        sa.CheckConstraint('consecutive_losses >= 0', name='ck_real_bankrolls_streak_not_negative'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_real_bankrolls_user_id_users', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_real_bankrolls'),
        sa.UniqueConstraint('user_id', name='uq_real_bankrolls_user_id'),
    )
    op.create_index('ix_real_bankrolls_user_id', 'real_bankrolls', ['user_id'], unique=True)

    # 2. real_bet_tickets
    op.create_table(
        'real_bet_tickets',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('ticket_reference', sa.String(length=32), nullable=False),
        sa.Column('bookmaker', sa.String(length=16), nullable=False),
        sa.Column('sport', sa.String(length=64), nullable=False),
        sa.Column('league', sa.String(length=128), nullable=False),
        sa.Column('match_id', sa.String(length=128), nullable=False),
        sa.Column('event_name', sa.String(length=255), nullable=False),
        sa.Column('market_type', sa.String(length=100), nullable=False),
        sa.Column('selection', sa.String(length=128), nullable=False),
        sa.Column('recommended_stake', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('placed_stake', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('target_odds', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('placed_odds', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('minimum_profitable_odds', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('true_probability', sa.Float(), nullable=False),
        sa.Column('model_edge_pct', sa.Float(), nullable=False),
        sa.Column('kelly_fraction', sa.Float(), nullable=False),
        sa.Column('deep_link_url', sa.Text(), nullable=False),
        sa.Column('clipboard_payload', sa.Text(), nullable=False),
        sa.Column('bookmaker_slip_id', sa.String(length=64), nullable=True),
        sa.Column('quote_market', sa.String(length=64), nullable=True),
        sa.Column('quote_selection', sa.String(length=32), nullable=True),
        sa.Column('settleable_from_score', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('gross_return', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('settlement_payout', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('net_profit', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('tds_deducted', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('vetting_pillar_score', sa.Float(), nullable=False),
        sa.Column('odds_decay_flag', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('placed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('placed_stake >= 0', name='ck_real_bet_tickets_ticket_stake_positive'),
        sa.CheckConstraint('target_odds > 1', name='ck_real_bet_tickets_target_odds_valid'),
        sa.CheckConstraint('placed_odds IS NULL OR placed_odds > 1', name='ck_real_bet_tickets_placed_odds_valid'),
        sa.CheckConstraint("bookmaker IN ('parimatch', 'onexbet')", name='ck_real_bet_tickets_bookmaker_valid'),
        sa.CheckConstraint("status IN ('PENDING_PLACEMENT', 'PLACED', 'WON', 'HALF_WON', 'LOST', 'HALF_LOST', 'PUSH', 'VOID', 'CANCELLED')", name='ck_real_bet_tickets_status_valid'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_real_bet_tickets_user_id_users', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_real_bet_tickets'),
        sa.UniqueConstraint('ticket_reference', name='uq_real_bet_tickets_ticket_reference'),
    )
    op.create_index('ix_real_bet_tickets_match', 'real_bet_tickets', ['match_id'])
    op.create_index('ix_real_bet_tickets_ticket_reference', 'real_bet_tickets', ['ticket_reference'], unique=True)
    op.create_index('ix_real_bet_tickets_user_id', 'real_bet_tickets', ['user_id'])
    op.create_index('ix_real_bet_tickets_user_status', 'real_bet_tickets', ['user_id', 'status'])

    # 3. real_cash_transactions
    op.create_table(
        'real_cash_transactions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('ticket_id', sa.Uuid(), nullable=True),
        sa.Column('transaction_type', sa.String(length=32), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False),
        sa.Column('balance_before', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('balance_after', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('reference_id', sa.String(length=64), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('amount >= 0', name='ck_real_cash_transactions_amount_positive'),
        sa.CheckConstraint("transaction_type IN ('DEPOSIT', 'WITHDRAWAL', 'BET_STAKE_LOCKED', 'BET_PAYOUT_CREDIT', 'TDS_WITHHOLDING_DEBIT', 'BET_REFUND', 'BET_STAKE_FORFEITED')", name='ck_real_cash_transactions_transaction_type_valid'),
        sa.ForeignKeyConstraint(['ticket_id'], ['real_bet_tickets.id'], name='fk_real_cash_transactions_ticket_id_real_bet_tickets', ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_real_cash_transactions_user_id_users', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_real_cash_transactions'),
    )
    op.create_index('ix_real_cash_transactions_user_created', 'real_cash_transactions', ['user_id', 'created_at'])
    op.create_index('ix_real_cash_transactions_user_id', 'real_cash_transactions', ['user_id'])


def downgrade() -> None:
    op.drop_table('real_cash_transactions')
    op.drop_table('real_bet_tickets')
    op.drop_table('real_bankrolls')
