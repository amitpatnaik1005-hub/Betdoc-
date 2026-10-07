"""align hive bots with the canonical commander roster

The handoff document is the source of truth for the 17 commanders. Five labels created by the
Group 41 migration are retired and renamed to their successors by role, so existing Hive rows
(bot profiles, task assignments, learning logs) keep their history:

    PRITHVIRAJ     -> KARNA      steam detection / price history -> competitive intel, odds shopping
    PORUS          -> CHANAKYA   capital protection, hedging      -> risk management, Kelly
    BIRBAL         -> BHEESHMA   logic                            -> rules & compliance
    VARAHMIHIR     -> DRONA      forecasting, backtests           -> training, ML ops
    KAPILENDRADEVA -> ARJUNA     expansion                        -> sniper, high-frequency execution

Revision ID: f3a9c1d27b40
Revises: e4b7d2a9c601
Create Date: 2026-10-08 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3a9c1d27b40'
down_revision: Union[str, Sequence[str], None] = 'e4b7d2a9c601'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUM_NAME = "hive_legendary_bot"

RENAMES: tuple[tuple[str, str], ...] = (
    ("PRITHVIRAJ", "KARNA"),
    ("PORUS", "CHANAKYA"),
    ("BIRBAL", "BHEESHMA"),
    ("VARAHMIHIR", "DRONA"),
    ("KAPILENDRADEVA", "ARJUNA"),
)


def _label_exists(label: str) -> bool:
    bind = op.get_bind()
    found = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :enum_name AND e.enumlabel = :label"
        ),
        {"enum_name": ENUM_NAME, "label": label},
    ).first()
    return found is not None


def _rename(old: str, new: str) -> None:
    # Native enums only exist on PostgreSQL; other dialects store a CHECK built from the models.
    if op.get_bind().dialect.name != "postgresql":
        return
    # Idempotent: a database created straight from the current models already carries the new label.
    if _label_exists(old) and not _label_exists(new):
        # DDL cannot take bind parameters; both labels are module constants, never user input.
        op.execute(sa.text(f"ALTER TYPE {ENUM_NAME} RENAME VALUE '{old}' TO '{new}'"))


def upgrade() -> None:
    """Upgrade schema."""
    for old, new in RENAMES:
        _rename(old, new)


def downgrade() -> None:
    """Downgrade schema."""
    for old, new in reversed(RENAMES):
        _rename(new, old)
