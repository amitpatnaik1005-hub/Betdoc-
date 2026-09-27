"""market ticks

Revision ID: 9b10c512d78a
Revises: 3a44e473d357
Create Date: 2026-09-27 23:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b10c512d78a'
down_revision: Union[str, None] = '3a44e473d357'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'market_ticks',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('bookmaker_id', sa.String(length=64), nullable=False),
        sa.Column('match_id', sa.String(length=128), nullable=False),
        sa.Column('selection_id', sa.String(length=128), nullable=False),
        sa.Column('market_type', sa.String(length=64), nullable=False),
        sa.Column('odds_type', sa.String(length=8), nullable=False),
        sa.Column('decimal_odds', sa.Numeric(precision=16, scale=4), nullable=False),
        sa.Column('line', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('is_sharp', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_market_ticks'))
    )
    op.create_index('ix_market_ticks_market_lookup', 'market_ticks', ['match_id', 'market_type', 'selection_id', 'odds_type', 'timestamp'], unique=False)
    op.create_index('ix_market_ticks_timestamp', 'market_ticks', ['timestamp'], unique=False)
    op.create_check_constraint('odds_type_valid', 'market_ticks', "odds_type IN ('BACK', 'LAY')")
    op.create_check_constraint('decimal_odds_min', 'market_ticks', "decimal_odds >= 1.0")


def downgrade() -> None:
    op.drop_index('ix_market_ticks_timestamp', table_name='market_ticks')
    op.drop_index('ix_market_ticks_market_lookup', table_name='market_ticks')
    op.drop_table('market_ticks')
