"""The daily market health snapshot: TODAR MAL's "when to bet" (Group 115, points b.10 and b.12). Developed for Amit Ashok
Kumar Patnaik.

One row per local trading day (SENTINEL_TIMEZONE), refreshed whenever the day is evaluated: the 0-100 score, its tier
(REST_DAY, SELECTIVE, PRIME_HARVEST), the advice, and the measured inputs behind it (the desk books' Match Odds
overround, the slate's depth, rotation and absence risk, the anchors priced at or above fair). An input that could not
be measured is NULL and named in ``unmeasured``, never filled with a default. The daily intimation's alert id and its
time are kept so the phone hears the day once; what each channel delivered is read from the Sentinel's delivery log.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, Float, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

_JSON = JSON().with_variant(JSONB(), "postgresql")


class MarketAdvisoryTier(StrEnum):
    REST_DAY = "REST_DAY"  # under MARKET_HEALTH_REST_BELOW: capital preservation, a warning to the phone
    SELECTIVE = "SELECTIVE"  # light stakes, anchors only; nothing sent
    PRIME_HARVEST = "PRIME_HARVEST"  # from MARKET_HEALTH_PRIME_FROM: the anchors go to the phone


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


class DailyMarketHealthSnapshot(Base):
    __tablename__ = "daily_market_health_snapshots"
    __table_args__ = (
        CheckConstraint("health_score >= 0 AND health_score <= 100", name="score_range"),
        CheckConstraint(_in("advisory_tier", MarketAdvisoryTier), name="tier_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_date: Mapped[date] = mapped_column(Date, unique=True)  # the local trading day
    health_score: Mapped[int] = mapped_column(Integer)
    advisory_tier: Mapped[str] = mapped_column(String(32))
    advisory_message: Mapped[str] = mapped_column(Text)
    deductions: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, default=list)  # [{code, points, reason}]
    unmeasured: Mapped[list[str]] = mapped_column(_JSON, default=list)  # inputs the day could not measure
    active_fixtures_count: Mapped[int] = mapped_column(Integer, default=0)  # priced at Parimatch or 1xBet, kicking off today
    top_tier_fixtures_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None: no league tier known
    average_bookmaker_margin: Mapped[float | None] = mapped_column(Float, nullable=True)  # None: no complete market at a desk book
    rotation_risks_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None: the Wire has read nothing recent
    anchors_count: Mapped[int] = mapped_column(Integer, default=0)  # legs a synthesized slip could use today
    top_anchors: Mapped[list[dict[str, Any]]] = mapped_column(_JSON, default=list)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # the day's intimation, once sent
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
