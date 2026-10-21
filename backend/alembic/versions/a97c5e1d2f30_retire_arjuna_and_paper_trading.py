"""retire ARJUNA into KUMBHA and unmap the paper trading tables (Group 97)

Group 97 retired the Paper Trading Sandbox and the commander ARJUNA.

ARJUNA (data), following f3a9c1d27b40 / f96a2b7c4d10's rule that a retired commander's Hive history goes to its
successor by role. KUMBHA (the Vault) now watches the orders awaiting reconciliation that ARJUNA watched:

    hive_tasks.assignee_name            ARJUNA -> KUMBHA
    hive_self_learning_logs.bot_name    ARJUNA -> KUMBHA
    hive_bot_profiles                   ARJUNA's counters folded into KUMBHA's profile, then its row removed
                                        (renamed to KUMBHA when KUMBHA has never reported)

The label stays dormant in the Postgres type ``hive_legendary_bot``.

Paper trading (schema): ``paper_accounts``, ``paper_bets`` and ``paper_ledger_entries`` are kept, unmapped, with
their history (``alembic/env.py`` keeps autogenerate from proposing to drop them). ``tax_ledger_entries.paper_bet_id``
pointed at ``paper_bets``; that foreign key is dropped so the tax ledger no longer depends on an unmapped table. The
column and its unique constraint stay: the sandbox's PAPER rows keep their historical id.

Downgrade restores the foreign key; the merged Hive history cannot be split back out.

Revision ID: a97c5e1d2f30
Revises: f96a2b7c4d10
Create Date: 2026-10-21 23:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a97c5e1d2f30'
down_revision: Union[str, Sequence[str], None] = 'f96a2b7c4d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RETIRED = "ARJUNA"
SUCCESSOR = "KUMBHA"
TAX_PAPER_FK = "fk_tax_ledger_entries_paper_bet_id_paper_bets"


def _retire_commander() -> None:
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


def upgrade() -> None:
    """Upgrade schema."""
    _retire_commander()
    with op.batch_alter_table('tax_ledger_entries', schema=None) as batch_op:
        batch_op.drop_constraint(TAX_PAPER_FK, type_='foreignkey')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tax_ledger_entries', schema=None) as batch_op:
        batch_op.create_foreign_key(TAX_PAPER_FK, 'paper_bets', ['paper_bet_id'], ['id'], ondelete='SET NULL')
