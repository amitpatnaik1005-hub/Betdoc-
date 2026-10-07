"""Add integration models

Revision ID: 0cf956003817
Revises: 5e83c9f33403
Create Date: 2026-10-01 11:40:54.669774

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0cf956003817'
down_revision: Union[str, Sequence[str], None] = '5e83c9f33403'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'api_credentials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False),
        sa.Column('encrypted_key', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_credentials_provider'), 'api_credentials', ['provider'], unique=False)

    op.create_table(
        'event_research',
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('expected_margin', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('spread_line', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('decimal_odds', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('event_id')
    )

    op.create_table(
        'predictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('model_name', sa.String(length=64), nullable=False),
        sa.Column('probability', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('implied_probability', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('edge', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('decimal_odds', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('recommended_stake', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('approved', sa.Boolean(), nullable=False),
        sa.Column('risk_reason', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_predictions_event_id'), 'predictions', ['event_id'], unique=False)

    op.create_table(
        'bets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('prediction_id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('stake', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('decimal_odds', sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('pnl', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['prediction_id'], ['predictions.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bets_event_id'), 'bets', ['event_id'], unique=False)
    op.create_index(op.f('ix_bets_prediction_id'), 'bets', ['prediction_id'], unique=False)
    op.create_index(op.f('ix_bets_status'), 'bets', ['status'], unique=False)

    op.create_table(
        'subsystem_constraints',
        sa.Column('subsystem', sa.String(length=32), nullable=False),
        sa.Column('flag', sa.String(length=64), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.String(length=256), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('subsystem', 'flag')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('subsystem_constraints')
    op.drop_index(op.f('ix_bets_status'), table_name='bets')
    op.drop_index(op.f('ix_bets_prediction_id'), table_name='bets')
    op.drop_index(op.f('ix_bets_event_id'), table_name='bets')
    op.drop_table('bets')
    op.drop_index(op.f('ix_predictions_event_id'), table_name='predictions')
    op.drop_table('predictions')
    op.drop_table('event_research')
    op.drop_index(op.f('ix_api_credentials_provider'), table_name='api_credentials')
    op.drop_table('api_credentials')

