"""post-execution feedback loop: model prediction feedback, root-cause audits, CLV and settlement attribution (Group 73)

Revision ID: f93c8b1a0d2e
Revises: e82b71c0d45a
Create Date: 2026-10-10 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f93c8b1a0d2e'
down_revision: Union[str, Sequence[str], None] = 'e82b71c0d45a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_TAGS = ("'NONE', 'INPLAY_SHOCK_RED_CARD', 'STEAM_ADVERSE_SELECTION', 'WEATHER_ANOMALY', 'MODEL_UNDERESTIMATION', "
         "'REFEREE_STRICTNESS_BIAS', 'VARIANCE_BAD_LUCK'")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('user_placed_bets', sa.Column('closing_odds', sa.Numeric(precision=12, scale=4), nullable=True))
    op.add_column('user_placed_bets', sa.Column('clv_pct', sa.Float(), nullable=True))
    op.add_column('user_placed_bets', sa.Column('clv_sharp_pct', sa.Float(), nullable=True))
    op.add_column('user_placed_bets', sa.Column('settlement_source', sa.String(length=16), nullable=True))
    op.add_column('user_placed_bets', sa.Column('root_cause_tag', sa.String(length=32), nullable=True))
    op.add_column('user_placed_bets', sa.Column('feedback_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_user_placed_bets_feedback_due', 'user_placed_bets', ['status', 'feedback_at'], unique=False)
    # bets settled before this group came from settle_pending (or a cashout): say so
    op.execute("UPDATE user_placed_bets SET settlement_source = CASE WHEN status = 'CASHED_OUT' THEN 'CASHOUT' ELSE 'AUTOMATED' END WHERE status <> 'PENDING'")

    op.add_column('user_placed_legs', sa.Column('closing_odds', sa.Numeric(precision=12, scale=4), nullable=True))
    op.add_column('user_placed_legs', sa.Column('closing_fair_probability', sa.Float(), nullable=True))
    op.add_column('user_placed_legs', sa.Column('closing_book', sa.String(length=16), nullable=True))

    op.create_table('model_prediction_feedback',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('bet_id', sa.Uuid(), nullable=False),
    sa.Column('leg_id', sa.Uuid(), nullable=False),
    sa.Column('fixture_id', sa.String(length=128), nullable=False),
    sa.Column('sport_key', sa.String(length=64), nullable=True),
    sa.Column('market', sa.String(length=64), nullable=False),
    sa.Column('selection', sa.String(length=16), nullable=False),
    sa.Column('model_name', sa.String(length=32), nullable=False),
    sa.Column('predicted_prob', sa.Float(), nullable=False),
    sa.Column('actual_outcome', sa.Float(), nullable=False),
    sa.Column('brier_score', sa.Float(), nullable=False),
    sa.Column('log_loss', sa.Float(), nullable=False),
    sa.Column('rps', sa.Float(), nullable=True),
    sa.Column('closing_odds', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('clv_pct', sa.Float(), nullable=True),
    sa.Column('details', _JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('predicted_prob >= 0 AND predicted_prob <= 1', name=op.f('ck_model_prediction_feedback_prob_bounded')),
    sa.CheckConstraint('actual_outcome >= 0 AND actual_outcome <= 1', name=op.f('ck_model_prediction_feedback_outcome_bounded')),
    sa.CheckConstraint('brier_score >= 0 AND brier_score <= 1', name=op.f('ck_model_prediction_feedback_brier_bounded')),
    sa.CheckConstraint('log_loss >= 0', name=op.f('ck_model_prediction_feedback_log_loss_not_negative')),
    sa.CheckConstraint('rps IS NULL OR (rps >= 0 AND rps <= 1)', name=op.f('ck_model_prediction_feedback_rps_bounded')),
    sa.ForeignKeyConstraint(['bet_id'], ['user_placed_bets.id'], name=op.f('fk_model_prediction_feedback_bet_id_user_placed_bets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['leg_id'], ['user_placed_legs.id'], name=op.f('fk_model_prediction_feedback_leg_id_user_placed_legs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_prediction_feedback')),
    sa.UniqueConstraint('leg_id', 'model_name', name=op.f('uq_model_prediction_feedback_leg_id'))
    )
    op.create_index('ix_model_prediction_feedback_model_created', 'model_prediction_feedback', ['model_name', 'created_at'], unique=False)
    op.create_index('ix_model_prediction_feedback_fixture_market', 'model_prediction_feedback', ['fixture_id', 'market'], unique=False)
    op.create_index('ix_model_prediction_feedback_bet', 'model_prediction_feedback', ['bet_id'], unique=False)

    op.create_table('settlement_root_cause_audits',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('bet_id', sa.Uuid(), nullable=False),
    sa.Column('root_cause_tag', sa.String(length=32), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=False),
    sa.Column('model_error_delta', sa.Float(), nullable=True),
    sa.Column('evidence', _JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(f'root_cause_tag IN ({_TAGS})', name=op.f('ck_settlement_root_cause_audits_tag_known')),
    sa.ForeignKeyConstraint(['bet_id'], ['user_placed_bets.id'], name=op.f('fk_settlement_root_cause_audits_bet_id_user_placed_bets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_settlement_root_cause_audits'))
    )
    op.create_index('ix_settlement_root_cause_audits_bet', 'settlement_root_cause_audits', ['bet_id', 'created_at'], unique=False)
    op.create_index('ix_settlement_root_cause_audits_tag', 'settlement_root_cause_audits', ['root_cause_tag', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_settlement_root_cause_audits_tag', table_name='settlement_root_cause_audits')
    op.drop_index('ix_settlement_root_cause_audits_bet', table_name='settlement_root_cause_audits')
    op.drop_table('settlement_root_cause_audits')
    op.drop_index('ix_model_prediction_feedback_bet', table_name='model_prediction_feedback')
    op.drop_index('ix_model_prediction_feedback_fixture_market', table_name='model_prediction_feedback')
    op.drop_index('ix_model_prediction_feedback_model_created', table_name='model_prediction_feedback')
    op.drop_table('model_prediction_feedback')
    op.drop_column('user_placed_legs', 'closing_book')
    op.drop_column('user_placed_legs', 'closing_fair_probability')
    op.drop_column('user_placed_legs', 'closing_odds')
    op.drop_index('ix_user_placed_bets_feedback_due', table_name='user_placed_bets')
    op.drop_column('user_placed_bets', 'feedback_at')
    op.drop_column('user_placed_bets', 'root_cause_tag')
    op.drop_column('user_placed_bets', 'settlement_source')
    op.drop_column('user_placed_bets', 'clv_sharp_pct')
    op.drop_column('user_placed_bets', 'clv_pct')
    op.drop_column('user_placed_bets', 'closing_odds')
