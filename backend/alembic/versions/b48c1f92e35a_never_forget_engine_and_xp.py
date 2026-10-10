"""never-forget shield and experience: mistake memories, rules, prevention audits, XP profiles and logs; pillar 15 (Group 75)

Revision ID: b48c1f92e35a
Revises: a17e92c4b50d
Create Date: 2026-10-10 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b48c1f92e35a'
down_revision: Union[str, Sequence[str], None] = 'a17e92c4b50d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
_CAUSES = "'NONE', 'INPLAY_SHOCK_RED_CARD', 'STEAM_ADVERSE_SELECTION', 'WEATHER_ANOMALY', 'MODEL_UNDERESTIMATION', 'REFEREE_STRICTNESS_BIAS', 'VARIANCE_BAD_LUCK'"
_RULE_STATUSES = "'ACTIVE', 'EXPERIMENTAL', 'ARCHIVED'"
_RESULTS = "'WON', 'HALF_WON', 'VOID', 'HALF_LOST', 'LOST'"
_XP_ACTIONS = "'SLIP_VETTED', 'BET_WON', 'LOSS_PREVENTED', 'MISTAKE_MEMORIZED', 'STREAK_BONUS'"


def upgrade() -> None:
    """Upgrade schema."""
    # pillar 15: an audit can now pass 15 pillars
    op.drop_constraint(op.f('ck_twin_vetting_audits_pillars_bounded'), 'twin_vetting_audits', type_='check')
    op.create_check_constraint('pillars_bounded', 'twin_vetting_audits', 'pillars_passed >= 0 AND pillars_passed <= 15')

    op.create_table('ashoka_mistake_memories',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('bet_id', sa.Uuid(), nullable=False),
    sa.Column('leg_id', sa.Uuid(), nullable=False),
    sa.Column('vetting_audit_id', sa.Uuid(), nullable=True),
    sa.Column('fixture_id', sa.String(length=128), nullable=False),
    sa.Column('home', sa.String(length=128), nullable=False),
    sa.Column('away', sa.String(length=128), nullable=False),
    sa.Column('sport_key', sa.String(length=64), nullable=True),
    sa.Column('league', sa.String(length=64), nullable=True),
    sa.Column('market', sa.String(length=64), nullable=False),
    sa.Column('selection', sa.String(length=16), nullable=False),
    sa.Column('placed_odds', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('leg_result', sa.String(length=12), nullable=False),
    sa.Column('shape', sa.String(length=48), nullable=False),
    sa.Column('loss_root_cause', sa.String(length=32), nullable=False),
    sa.Column('root_cause_explanation', sa.Text(), nullable=False),
    sa.Column('situational_fingerprint', _JSON, nullable=False),
    sa.Column('situation_raw', _JSON, nullable=False),
    sa.Column('extracted_lesson', sa.Text(), nullable=False),
    sa.Column('developer_credit', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('placed_odds >= 1', name=op.f('ck_ashoka_mistake_memories_odds_valid')),
    sa.CheckConstraint(f'loss_root_cause IN ({_CAUSES})', name=op.f('ck_ashoka_mistake_memories_cause_known')),
    sa.ForeignKeyConstraint(['bet_id'], ['user_placed_bets.id'], name=op.f('fk_ashoka_mistake_memories_bet_id_user_placed_bets'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['leg_id'], ['user_placed_legs.id'], name=op.f('fk_ashoka_mistake_memories_leg_id_user_placed_legs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vetting_audit_id'], ['twin_vetting_audits.id'], name=op.f('fk_ashoka_mistake_memories_vetting_audit_id_twin_vetting_audits'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ashoka_mistake_memories')),
    sa.UniqueConstraint('leg_id', name=op.f('uq_ashoka_mistake_memories_leg_id'))
    )
    op.create_index('ix_ashoka_mistake_memories_bet', 'ashoka_mistake_memories', ['bet_id'], unique=False)
    op.create_index('ix_ashoka_mistake_memories_created', 'ashoka_mistake_memories', ['created_at'], unique=False)
    op.create_index('ix_ashoka_mistake_memories_fixture', 'ashoka_mistake_memories', ['fixture_id'], unique=False)

    op.create_table('never_forget_rules',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('mistake_id', sa.Uuid(), nullable=False),
    sa.Column('rule_code', sa.String(length=32), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('shape', sa.String(length=48), nullable=False),
    sa.Column('rule_conditions', _JSON, nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('status_reason', sa.Text(), nullable=False),
    sa.Column('status_changed_by', sa.Uuid(), nullable=True),
    sa.Column('specificity', _JSON, nullable=False),
    sa.Column('times_triggered', sa.Integer(), nullable=False),
    sa.Column('last_triggered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(f'status IN ({_RULE_STATUSES})', name=op.f('ck_never_forget_rules_status_known')),
    sa.CheckConstraint('times_triggered >= 0', name=op.f('ck_never_forget_rules_triggers_not_negative')),
    sa.ForeignKeyConstraint(['mistake_id'], ['ashoka_mistake_memories.id'], name=op.f('fk_never_forget_rules_mistake_id_ashoka_mistake_memories'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_never_forget_rules')),
    sa.UniqueConstraint('mistake_id', name=op.f('uq_never_forget_rules_mistake_id')),
    sa.UniqueConstraint('rule_code', name=op.f('uq_never_forget_rules_rule_code'))
    )
    op.create_index('ix_never_forget_rules_status', 'never_forget_rules', ['status', 'created_at'], unique=False)

    op.create_table('never_forget_prevention_audits',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('rule_id', sa.Uuid(), nullable=False),
    sa.Column('mistake_id', sa.Uuid(), nullable=False),
    sa.Column('vetting_audit_id', sa.Uuid(), nullable=True),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('dedupe_key', sa.String(length=240), nullable=False),
    sa.Column('leg_ref', sa.String(length=220), nullable=False),
    sa.Column('fixture_id', sa.String(length=128), nullable=False),
    sa.Column('home', sa.String(length=128), nullable=False),
    sa.Column('away', sa.String(length=128), nullable=False),
    sa.Column('sport_key', sa.String(length=64), nullable=True),
    sa.Column('kickoff', sa.DateTime(timezone=True), nullable=True),
    sa.Column('market', sa.String(length=64), nullable=False),
    sa.Column('selection', sa.String(length=16), nullable=False),
    sa.Column('odds', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('similarity_score', sa.Float(), nullable=False),
    sa.Column('stake_withheld_inr', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('veto_reason', sa.Text(), nullable=False),
    sa.Column('outcome', sa.String(length=12), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(f'outcome IS NULL OR outcome IN ({_RESULTS})', name=op.f('ck_never_forget_prevention_audits_outcome_known')),
    sa.CheckConstraint('similarity_score >= 0 AND similarity_score <= 1', name=op.f('ck_never_forget_prevention_audits_similarity_bounded')),
    sa.CheckConstraint('stake_withheld_inr IS NULL OR stake_withheld_inr >= 0', name=op.f('ck_never_forget_prevention_audits_stake_not_negative')),
    sa.ForeignKeyConstraint(['mistake_id'], ['ashoka_mistake_memories.id'], name=op.f('fk_never_forget_prevention_audits_mistake_id_ashoka_mistake_memories'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['never_forget_rules.id'], name=op.f('fk_never_forget_prevention_audits_rule_id_never_forget_rules'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_never_forget_prevention_audits_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vetting_audit_id'], ['twin_vetting_audits.id'], name=op.f('fk_never_forget_prevention_audits_vetting_audit_id_twin_vetting_audits'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_never_forget_prevention_audits')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_never_forget_prevention_audits_dedupe_key'))
    )
    op.create_index('ix_never_forget_prevention_audits_rule_created', 'never_forget_prevention_audits', ['rule_id', 'created_at'], unique=False)
    op.create_index('ix_never_forget_prevention_audits_unresolved', 'never_forget_prevention_audits', ['outcome', 'fixture_id'], unique=False)
    op.create_index('ix_never_forget_prevention_audits_user_created', 'never_forget_prevention_audits', ['user_id', 'created_at'], unique=False)

    op.create_table('user_xp_profiles',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('total_xp', sa.Integer(), nullable=False),
    sa.Column('level', sa.Integer(), nullable=False),
    sa.Column('rank_title', sa.String(length=32), nullable=False),
    sa.Column('slips_vetted_count', sa.Integer(), nullable=False),
    sa.Column('bets_won_count', sa.Integer(), nullable=False),
    sa.Column('losses_prevented_count', sa.Integer(), nullable=False),
    sa.Column('mistakes_learned_count', sa.Integer(), nullable=False),
    sa.Column('streak_bonuses_count', sa.Integer(), nullable=False),
    sa.Column('last_action_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('level >= 1', name=op.f('ck_user_xp_profiles_level_positive')),
    sa.CheckConstraint('total_xp >= 0', name=op.f('ck_user_xp_profiles_xp_not_negative')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_xp_profiles_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_xp_profiles')),
    sa.UniqueConstraint('user_id', name=op.f('uq_user_xp_profiles_user_id'))
    )

    op.create_table('user_xp_audit_logs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('profile_id', sa.Uuid(), nullable=False),
    sa.Column('action_type', sa.String(length=32), nullable=False),
    sa.Column('xp_amount', sa.Integer(), nullable=False),
    sa.Column('source_ref', sa.String(length=160), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=False),
    sa.Column('metadata_snapshot', _JSON, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(f'action_type IN ({_XP_ACTIONS})', name=op.f('ck_user_xp_audit_logs_action_known')),
    sa.CheckConstraint('xp_amount > 0', name=op.f('ck_user_xp_audit_logs_amount_positive')),
    sa.ForeignKeyConstraint(['profile_id'], ['user_xp_profiles.id'], name=op.f('fk_user_xp_audit_logs_profile_id_user_xp_profiles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_xp_audit_logs')),
    sa.UniqueConstraint('profile_id', 'action_type', 'source_ref', name=op.f('uq_user_xp_audit_logs_profile_id'))
    )
    op.create_index('ix_user_xp_audit_logs_profile_created', 'user_xp_audit_logs', ['profile_id', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_user_xp_audit_logs_profile_created', table_name='user_xp_audit_logs')
    op.drop_table('user_xp_audit_logs')
    op.drop_table('user_xp_profiles')
    op.drop_index('ix_never_forget_prevention_audits_user_created', table_name='never_forget_prevention_audits')
    op.drop_index('ix_never_forget_prevention_audits_unresolved', table_name='never_forget_prevention_audits')
    op.drop_index('ix_never_forget_prevention_audits_rule_created', table_name='never_forget_prevention_audits')
    op.drop_table('never_forget_prevention_audits')
    op.drop_index('ix_never_forget_rules_status', table_name='never_forget_rules')
    op.drop_table('never_forget_rules')
    op.drop_index('ix_ashoka_mistake_memories_fixture', table_name='ashoka_mistake_memories')
    op.drop_index('ix_ashoka_mistake_memories_created', table_name='ashoka_mistake_memories')
    op.drop_index('ix_ashoka_mistake_memories_bet', table_name='ashoka_mistake_memories')
    op.drop_table('ashoka_mistake_memories')
    # an audit that passed 15 pillars cannot survive the 14-pillar bound: the downgrade fails on it rather than rewrite history
    op.drop_constraint(op.f('ck_twin_vetting_audits_pillars_bounded'), 'twin_vetting_audits', type_='check')
    op.create_check_constraint('pillars_bounded', 'twin_vetting_audits', 'pillars_passed >= 0 AND pillars_passed <= 14')
