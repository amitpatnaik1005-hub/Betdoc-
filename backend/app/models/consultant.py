"""The BetDoc Consultant's signed verdicts (Group 105, ASHVATTHAMA, point b.6). Developed for Amit Ashok Kumar Patnaik.

One row per consultation: the slip the Doctor, the Lawyer and the Consultant examined, their verdict, and the whole
report exactly as it was signed. ``seal`` is an HMAC-SHA256 over the report's canonical JSON under a key derived from
the server's secret (``app.domain.consultant.diagnostics.seal``): the client cannot forge one, and any later change to
the stored report no longer matches its seal (``GET /consultant/verdicts/{id}`` re-checks it). The application only
ever inserts rows; no route updates or deletes one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class VerdictStatus(StrEnum):
    APPROVED_GOLD = "APPROVED_GOLD"
    APPROVED_WITH_CURE = "APPROVED_WITH_CURE"
    REJECTED = "REJECTED"


class ConsultantVerdictModel(Base):
    __tablename__ = "consultant_verdicts"
    __table_args__ = (
        CheckConstraint("verdict IN ('APPROVED_GOLD', 'APPROVED_WITH_CURE', 'REJECTED')", name="verdict_known"),
        CheckConstraint("slip_score >= 0 AND slip_score <= 100", name="slip_score_bounded"),
        Index("ix_consultant_verdicts_user_signed", "user_id", "signed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    slip_id: Mapped[str] = mapped_column(String(32))  # Ashoka's slip id for these legs and structure
    leg_ids: Mapped[list[str]] = mapped_column(_JSON, default=list)
    bookmaker: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the retail book the slip was examined at
    verdict: Mapped[str] = mapped_column(String(24))
    slip_score: Mapped[float] = mapped_column(Float)
    tier: Mapped[str] = mapped_column(String(16))
    report: Mapped[dict[str, Any]] = mapped_column(_JSON, default=dict)  # the signed body, byte for byte what the seal covers
    seal: Mapped[str] = mapped_column(String(64))  # HMAC-SHA256, hex
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
