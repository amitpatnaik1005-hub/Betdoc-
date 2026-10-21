"""The pre-match board, one row per fixture (Group 101, point b.13a). Developed for Amit Ashok Kumar Patnaik.

Written by the ingestion fleet each time it buys a sport's Odds API board (``app.services.pre_match_odds_service``);
read by the desk without a network call. ``odds_snapshots`` stays the append-only price history; this table is the
latest board only.

* ``markets_json``: ``{market: {"polled_at", "books": {book: {"last_update", "outcomes": [{"name", "price", "point"?}]}}}}``.
  A fetch replaces the markets it carried and keeps the others (h2h-only quiet hours do not wipe totals), each market
  with its own ``polled_at``.
* ``bookmaker_odds``: the h2h prices per book as quoted, keyed home / draw / away.
* ``consensus_fair_odds``: the h2h board de-vigged (the Group 79 consensus: every complete book proportionally, then
  averaged) and the sharp benchmark (Shin, the first sharp book with a complete market); ``{}`` when no book is complete.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(UTC)


class PreMatchOddsSnapshotModel(Base):
    __tablename__ = "pre_match_odds_snapshots"
    __table_args__ = (
        Index("ix_pre_match_sport_commence", "sport_key", "commence_time"),  # the desk's sport + window read, the fleet's nearest kickoff
        Index("ix_pre_match_commence", "commence_time"),  # the all-sports window read and the retention prune
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)  # The Odds API event id
    sport_key: Mapped[str] = mapped_column(String(64), nullable=False)  # The Odds API sport key (soccer_epl, cricket_ipl, ...)
    home_team: Mapped[str] = mapped_column(String(128), nullable=False)
    away_team: Mapped[str] = mapped_column(String(128), nullable=False)
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    markets_json: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    consensus_fair_odds: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    bookmaker_odds: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False, default=dict)
    last_polled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # when the provider served this board
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now(), onupdate=utc_now)
