"""backtest, in-play stop-loss and manual parlays: the shield's stop-loss on the twin's watch, three more system bets (Group 77)

Revision ID: d71e3092f89c
Revises: c59d2013f48b
Create Date: 2026-10-11 10:00:00.000000

Group 77 extends existing tables rather than adding parallel ones: the backtests stay in ``lab_backtest_runs``
(Group 66; the new figures live in its result), the stop-loss shields are the twin's in-play monitors (Group 72),
manual parlays are twin audits and Ashoka ledger bets, and the account balances are the Vault's (Group 70).
Developer: Amit Ashok Kumar Patnaik.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd71e3092f89c'
down_revision: Union[str, Sequence[str], None] = 'c59d2013f48b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REASONS_BEFORE = "'TARGET_PROFIT_REACHED', 'PROBABILITY_COLLAPSE', 'HEDGE_LOCK', 'CASHOUT_ADVISED', 'MANUAL_USER_REQUEST'"
_REASONS = _REASONS_BEFORE + ", 'STOP_LOSS'"
_STRUCTURES_BEFORE = "'SINGLE', 'DOUBLE', 'TREBLE', 'ACCUMULATOR', 'TRIXIE', 'YANKEE', 'CANADIAN', 'HEINZ'"
_STRUCTURES = _STRUCTURES_BEFORE + ", 'PATENT', 'SUPER_HEINZ', 'GOLIATH'"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('twin_inplay_monitors', sa.Column('stop_loss_pct', sa.Float(), nullable=True))
    op.drop_constraint(op.f('ck_twin_inplay_monitors_pullout_reason_known'), 'twin_inplay_monitors', type_='check')
    op.create_check_constraint('pullout_reason_known', 'twin_inplay_monitors', f'pullout_reason IS NULL OR pullout_reason IN ({_REASONS})')
    op.drop_constraint(op.f('ck_user_placed_bets_ck_user_placed_bets_structure'), 'user_placed_bets', type_='check')
    op.create_check_constraint('ck_user_placed_bets_structure', 'user_placed_bets', f'structure IN ({_STRUCTURES})')


def downgrade() -> None:
    """Downgrade schema (fails on a bet or a monitor that uses what Group 77 added, rather than rewrite it)."""
    op.drop_constraint(op.f('ck_user_placed_bets_ck_user_placed_bets_structure'), 'user_placed_bets', type_='check')
    op.create_check_constraint('ck_user_placed_bets_structure', 'user_placed_bets', f'structure IN ({_STRUCTURES_BEFORE})')
    op.drop_constraint(op.f('ck_twin_inplay_monitors_pullout_reason_known'), 'twin_inplay_monitors', type_='check')
    op.create_check_constraint('pullout_reason_known', 'twin_inplay_monitors', f'pullout_reason IS NULL OR pullout_reason IN ({_REASONS_BEFORE})')
    op.drop_column('twin_inplay_monitors', 'stop_loss_pct')
