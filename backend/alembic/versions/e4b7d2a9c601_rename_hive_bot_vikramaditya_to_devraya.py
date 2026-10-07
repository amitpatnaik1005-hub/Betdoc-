"""rename hive bot VIKRAMADITYA to DEVRAYA

Commander #6 is DEVRAYA everywhere in the product (frontend registry, Omni HFT lane,
roadmap). The Group 41 migration created the native enum with the stale label
VIKRAMADITYA, so a database built from migrations would reject every DEVRAYA write.

Revision ID: e4b7d2a9c601
Revises: b7d341999e2a
Create Date: 2026-10-06 10:58:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4b7d2a9c601'
down_revision: Union[str, Sequence[str], None] = 'b7d341999e2a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUM_NAME = "hive_legendary_bot"
STALE_LABEL = "VIKRAMADITYA"
CANONICAL_LABEL = "DEVRAYA"


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
    _rename(STALE_LABEL, CANONICAL_LABEL)


def downgrade() -> None:
    """Downgrade schema."""
    _rename(CANONICAL_LABEL, STALE_LABEL)
