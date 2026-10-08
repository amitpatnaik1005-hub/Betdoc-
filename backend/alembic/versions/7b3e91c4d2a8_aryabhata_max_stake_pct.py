"""aryabhata max stake pct

The Control Panel's bankroll cap for Aryabhata stake recommendations: no recommended stake may
exceed this percentage of the user's live bankroll. Adjustable 1-10% from the risk slider.

Revision ID: 7b3e91c4d2a8
Revises: 2ca48d2a6d3e
Create Date: 2026-10-08 18:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7b3e91c4d2a8'
down_revision: Union[str, Sequence[str], None] = '2ca48d2a6d3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'system_settings',
        sa.Column('max_stake_pct', sa.Numeric(precision=5, scale=2), server_default='5.00', nullable=False),
    )
    op.create_check_constraint('ck_max_stake_pct', 'system_settings', 'max_stake_pct >= 1 AND max_stake_pct <= 10')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f('ck_system_settings_ck_max_stake_pct'), 'system_settings', type_='check')
    op.drop_column('system_settings', 'max_stake_pct')
