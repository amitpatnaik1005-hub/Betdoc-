"""real_bet_tickets: CASHED_OUT, and each placed slip's signed verdict and Oracle bet (Group 114)

Revision ID: f114a8b7c2d1
Revises: e106b4d2c9a7
Create Date: 2026-10-23 12:30:00.000000

* The status CHECK gains CASHED_OUT. The live constraint was created as ``ck_real_bet_tickets_ck_real_bet_tickets_status_valid``
  (the naming convention applied to an already-prefixed name), so every spelling is dropped before the new one is added;
  dropping only ``ck_real_bet_tickets_status_valid`` would leave the old CHECK in force and refuse every cashout.
* ``verdict_id`` (unique: a signed slip is placed once) and ``user_placed_bet_id`` link a ticket placed from the Bet-Slip
  to its Consultant verdict and to the Oracle bet whose legs settle it. Both are nullable: tickets built on the
  real-money desk itself have neither.

Downgrade refuses while any ticket is CASHED_OUT (the old CHECK cannot hold it).
Developed for Amit Ashok Kumar Patnaik.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f114a8b7c2d1'
down_revision: Union[str, Sequence[str], None] = 'e106b4d2c9a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUS_CHECK = "ck_real_bet_tickets_status_valid"
OLD_SPELLINGS = ("ck_real_bet_tickets_ck_real_bet_tickets_status_valid", "ck_real_bet_tickets_status_valid", "status_valid")
NEW_STATUSES = "'PENDING_PLACEMENT', 'PLACED', 'WON', 'HALF_WON', 'LOST', 'HALF_LOST', 'PUSH', 'VOID', 'CANCELLED', 'CASHED_OUT'"
OLD_STATUSES = "'PENDING_PLACEMENT', 'PLACED', 'WON', 'HALF_WON', 'LOST', 'HALF_LOST', 'PUSH', 'VOID', 'CANCELLED'"


def _replace_status_check(statuses: str) -> None:
    for name in OLD_SPELLINGS:
        op.execute(f"ALTER TABLE real_bet_tickets DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE real_bet_tickets ADD CONSTRAINT {STATUS_CHECK} CHECK (status IN ({statuses}))")


def upgrade() -> None:
    op.add_column("real_bet_tickets", sa.Column("verdict_id", sa.Uuid(), nullable=True))
    op.add_column("real_bet_tickets", sa.Column("user_placed_bet_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_real_bet_tickets_verdict_id_consultant_verdicts", "real_bet_tickets", "consultant_verdicts", ["verdict_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_real_bet_tickets_user_placed_bet_id_user_placed_bets", "real_bet_tickets", "user_placed_bets", ["user_placed_bet_id"], ["id"], ondelete="SET NULL")
    op.create_unique_constraint("uq_real_bet_tickets_verdict_id", "real_bet_tickets", ["verdict_id"])
    op.create_index("ix_real_bet_tickets_user_placed_bet_id", "real_bet_tickets", ["user_placed_bet_id"])
    if op.get_bind().dialect.name == "postgresql":
        _replace_status_check(NEW_STATUSES)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        cashed = bind.execute(sa.text("SELECT count(*) FROM real_bet_tickets WHERE status = 'CASHED_OUT'")).scalar()
        if cashed:
            raise RuntimeError(f"{cashed} ticket(s) are CASHED_OUT: the pre-Group 114 status CHECK cannot hold them")
        _replace_status_check(OLD_STATUSES)
    op.drop_index("ix_real_bet_tickets_user_placed_bet_id", table_name="real_bet_tickets")
    op.drop_constraint("uq_real_bet_tickets_verdict_id", "real_bet_tickets", type_="unique")
    op.drop_constraint("fk_real_bet_tickets_user_placed_bet_id_user_placed_bets", "real_bet_tickets", type_="foreignkey")
    op.drop_constraint("fk_real_bet_tickets_verdict_id_consultant_verdicts", "real_bet_tickets", type_="foreignkey")
    op.drop_column("real_bet_tickets", "user_placed_bet_id")
    op.drop_column("real_bet_tickets", "verdict_id")
