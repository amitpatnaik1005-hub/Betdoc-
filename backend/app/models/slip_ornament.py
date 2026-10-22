"""The Bet-Slip ornament's ticket record (Group 106, ASHVATTHAMA, points b.7 and b.12). Developed for Amit Ashok Kumar
Patnaik.

A ticket is the printable face of a signed Consultant verdict (``consultant_verdicts``): everything on it comes from
the sealed report, which never changes. What the user adds afterwards lives here, outside the seal: the bookmaker's
booking code once the slip is placed, and the deliveries (Telegram, SMS, email) made of it. One row per verdict,
created the first time either is recorded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class BetSlipTicketModel(Base):
    __tablename__ = "bet_slip_tickets"
    __table_args__ = (Index("ix_bet_slip_tickets_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    verdict_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consultant_verdicts.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    booking_code: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the bookmaker's own slip reference, typed by the user
    booking_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deliveries: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, default=list)  # [{channel, ok, deliveries, error, at}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
