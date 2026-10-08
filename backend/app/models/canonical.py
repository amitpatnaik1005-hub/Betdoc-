"""Canonical BetDoc entities: the UUIDs every external team/player name is mapped to.

Rows mirror the predefined alias dictionary (``app/data/canonical_aliases.json``); the fleet
upserts them, so ids are identical in every environment and other tables can reference them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, String, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class CanonicalEntity(Base):
    __tablename__ = "canonical_entities"
    __table_args__ = (
        UniqueConstraint("sport_key", "kind", "canonical_name", name="uq_canonical_entities_identity"),
        CheckConstraint("kind IN ('team', 'player')", name="kind"),
    )

    # No default: ids come from the dictionary, never from the database
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    sport_key: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    canonical_name: Mapped[str] = mapped_column(String(128))
    aliases: Mapped[list[Any]] = mapped_column(JsonColumn, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
