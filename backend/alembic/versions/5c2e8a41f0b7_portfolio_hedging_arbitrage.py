"""portfolio hedging arbitrage

Group 64: the bet ledger records partial fills (the stake asked for, next to the stake matched),
which arbitrage or hedge a leg belongs to, and a foreign-currency venue's own stake. Execution
venues gain their commission rate and account currency.

Revision ID: 5c2e8a41f0b7
Revises: 180f198651da
Create Date: 2026-10-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "5c2e8a41f0b7"
down_revision: Union[str, Sequence[str], None] = "180f198651da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cfo_phantom_ledger", sa.Column("requested_stake_inr", sa.Numeric(18, 2), nullable=True))
    op.add_column("cfo_phantom_ledger", sa.Column("strategy", sa.String(length=16), nullable=True))
    op.add_column("cfo_phantom_ledger", sa.Column("group_id", sa.Uuid(), nullable=True))
    op.add_column("cfo_phantom_ledger", sa.Column("currency", sa.String(length=3), nullable=True))
    op.add_column("cfo_phantom_ledger", sa.Column("stake_ccy", sa.Numeric(18, 2), nullable=True))
    op.create_check_constraint(
        "fill_within_request", "cfo_phantom_ledger", "requested_stake_inr IS NULL OR requested_stake_inr >= stake_inr"
    )
    op.create_index("ix_cfo_phantom_ledger_user_group", "cfo_phantom_ledger", ["user_id", "group_id"], unique=False)

    op.add_column("sniper_execution_venues", sa.Column("commission_rate", sa.Numeric(6, 4), nullable=True))
    op.add_column("sniper_execution_venues", sa.Column("currency", sa.String(length=3), nullable=True))
    op.create_check_constraint(
        "commission_range", "sniper_execution_venues", "commission_rate IS NULL OR (commission_rate >= 0 AND commission_rate < 0.5)"
    )
    op.create_check_constraint("currency_code", "sniper_execution_venues", "currency IS NULL OR length(currency) = 3")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_sniper_execution_venues_currency_code"), "sniper_execution_venues", type_="check")
    op.drop_constraint(op.f("ck_sniper_execution_venues_commission_range"), "sniper_execution_venues", type_="check")
    op.drop_column("sniper_execution_venues", "currency")
    op.drop_column("sniper_execution_venues", "commission_rate")

    op.drop_index("ix_cfo_phantom_ledger_user_group", table_name="cfo_phantom_ledger")
    op.drop_constraint(op.f("ck_cfo_phantom_ledger_fill_within_request"), "cfo_phantom_ledger", type_="check")
    op.drop_column("cfo_phantom_ledger", "stake_ccy")
    op.drop_column("cfo_phantom_ledger", "currency")
    op.drop_column("cfo_phantom_ledger", "group_id")
    op.drop_column("cfo_phantom_ledger", "strategy")
    op.drop_column("cfo_phantom_ledger", "requested_stake_inr")
