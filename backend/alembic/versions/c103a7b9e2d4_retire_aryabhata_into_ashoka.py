"""retire the ARYABHATA commander into ASHOKA (Group 103)

Group 103 unified the Sports Engine page (/sports) into The Oracle, and its commander ARYABHATA into ASHOKA. Only
the commander retires: the pricing engine that carries the name (``app.services.aryabhata_engine``, the ARYABHATA_*
settings and Redis streams) is unchanged. ``LegendaryBot`` no longer has ARYABHATA, so a row still holding it could
not be loaded. This is a data-only revision, following f3a9c1d27b40's rule (and f96a2b7c4d10 / a97c5e1d2f30's
precedent) that a retired commander's Hive history goes to its successor by role:

    hive_tasks.assignee_name            ARYABHATA -> ASHOKA
    hive_self_learning_logs.bot_name    ARYABHATA -> ASHOKA
    hive_bot_profiles                   ARYABHATA's counters folded into ASHOKA's profile, then its row removed
                                        (renamed to ASHOKA when ASHOKA has never reported)

No schema changes: the label stays dormant in the Postgres type ``hive_legendary_bot`` (Postgres cannot drop an
enum value in place, and nothing can write it any more). ASHOKA's health probe now also runs ARYABHATA's
probability self-test.

Downgrade is a no-op: once merged, which ASHOKA rows were ARYABHATA's cannot be told apart.

Revision ID: c103a7b9e2d4
Revises: b1013c5d7e9f
Create Date: 2026-10-22 03:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c103a7b9e2d4'
down_revision: Union[str, Sequence[str], None] = 'b1013c5d7e9f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RETIRED = "ARYABHATA"
SUCCESSOR = "ASHOKA"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    params = {"retired": RETIRED, "successor": SUCCESSOR}
    bind.execute(sa.text("UPDATE hive_tasks SET assignee_name = :successor WHERE assignee_name = :retired"), params)
    bind.execute(sa.text("UPDATE hive_self_learning_logs SET bot_name = :successor WHERE bot_name = :retired"), params)

    successor_reported = bind.execute(sa.text("SELECT 1 FROM hive_bot_profiles WHERE bot_name = :successor"), params).first() is not None
    if successor_reported:
        bind.execute(sa.text(
            "UPDATE hive_bot_profiles AS s SET "
            "tasks_completed = s.tasks_completed + r.tasks_completed, error_count = s.error_count + r.error_count "
            "FROM hive_bot_profiles AS r WHERE s.bot_name = :successor AND r.bot_name = :retired"
        ), params)
        bind.execute(sa.text("DELETE FROM hive_bot_profiles WHERE bot_name = :retired"), params)
    else:
        bind.execute(sa.text("UPDATE hive_bot_profiles SET bot_name = :successor WHERE bot_name = :retired"), params)


def downgrade() -> None:
    """Downgrade schema."""
    # Nothing to undo: the merged history cannot be split back out, and the schema never changed.
