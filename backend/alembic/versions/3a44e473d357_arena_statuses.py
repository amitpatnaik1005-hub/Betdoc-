"""arena statuses and strategy_name

Revision ID: 3a44e473d357
Revises: 3a44e473d356
Create Date: 2026-09-27 22:36:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3a44e473d357"
down_revision: Union[str, None] = "3a44e473d356"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATUSES = ("PENDING", "PENDING_NETWORK", "ACCEPTED", "REJECTED", "UNKNOWN", "WON", "LOST", "VOID")
NEW_STATUSES = OLD_STATUSES + ("HALF_WON", "HALF_LOST", "CASH_OUT")
OLD_RESOLVED = ("WON", "LOST", "VOID")
NEW_RESOLVED = OLD_RESOLVED + ("HALF_WON", "HALF_LOST", "CASH_OUT")

def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)

def upgrade() -> None:
    op.add_column("bet_ledger", sa.Column("strategy_name", sa.String(64), nullable=True))
    op.create_index(op.f("ix_bet_ledger_strategy_name"), "bet_ledger", ["strategy_name"])

    op.drop_constraint(op.f("ck_bet_ledger_status_valid"), "bet_ledger", type_="check")
    op.create_check_constraint("status_valid", "bet_ledger", f"status IN ({_in(NEW_STATUSES)})")

    op.drop_constraint(op.f("ck_bet_ledger_resolution_consistent"), "bet_ledger", type_="check")
    op.create_check_constraint(
        "resolution_consistent",
        "bet_ledger",
        f"status NOT IN ({_in(NEW_RESOLVED)}) OR resolved_at IS NOT NULL",
    )

def downgrade() -> None:
    op.drop_constraint(op.f("ck_bet_ledger_resolution_consistent"), "bet_ledger", type_="check")
    op.create_check_constraint(
        "resolution_consistent",
        "bet_ledger",
        f"(status IN ('PENDING', 'PENDING_NETWORK', 'UNKNOWN', 'ACCEPTED') AND resolved_at IS NULL AND payout IS NULL) OR (status IN ('WON', 'LOST', 'VOID', 'REJECTED') AND resolved_at IS NOT NULL)",
    )
    op.drop_constraint(op.f("ck_bet_ledger_status_valid"), "bet_ledger", type_="check")
    op.create_check_constraint("status_valid", "bet_ledger", f"status IN ({_in(OLD_STATUSES)})")

    op.drop_index(op.f("ix_bet_ledger_strategy_name"), table_name="bet_ledger")
    op.drop_column("bet_ledger", "strategy_name")

