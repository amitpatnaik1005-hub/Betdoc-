"""retire the GARUDA commander into PANINI (Group 96)

Group 96 retired the Phantom section and its commander GARUDA; The Lab (PANINI) absorbed the quant
toolkit. ``LegendaryBot`` no longer has GARUDA, so a row still holding it could not be loaded. This is a
data-only revision, following f3a9c1d27b40's rule that a retired commander's Hive history goes to its
successor by role:

    hive_tasks.assignee_name            GARUDA -> PANINI
    hive_self_learning_logs.bot_name    GARUDA -> PANINI
    hive_bot_profiles                   GARUDA's counters folded into PANINI's profile, then its row removed
                                        (renamed to PANINI when PANINI has never reported)

No schema changes: the label stays dormant in the Postgres type ``hive_legendary_bot`` (Postgres cannot
drop an enum value in place, and nothing can write it any more). ``phantom_arbitrage_opportunities`` is
left as it is (unmapped; ``alembic/env.py`` keeps autogenerate from proposing to drop it).

Downgrade is a no-op: once merged, which PANINI rows were GARUDA's cannot be told apart.

Revision ID: f96a2b7c4d10
Revises: e87c4521b38d
Create Date: 2026-10-21 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f96a2b7c4d10'
down_revision: Union[str, Sequence[str], None] = 'e87c4521b38d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RETIRED = "GARUDA"
SUCCESSOR = "PANINI"


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
