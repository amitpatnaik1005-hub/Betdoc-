import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No standalone index: ix_match_bookmaker_time leads with match_id and serves
    # match_id lookups. A duplicate index would double write cost on a high-volume table.
    match_id: Mapped[str] = mapped_column(String, nullable=False)
    sport_key: Mapped[str] = mapped_column(String, nullable=False)
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    bookmaker: Mapped[str] = mapped_column(String, nullable=False)
    market_type: Mapped[str] = mapped_column(String, nullable=False)
    selection: Mapped[str] = mapped_column(String, nullable=False)
    odds: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    # Adding bookmaker_last_update for accurate steam detection as suggested by Opus
    bookmaker_last_update: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        nullable=True
    )

    __table_args__ = (
        # KARNA: per-match, per-bookmaker price history.
        Index("ix_match_bookmaker_time", "match_id", "bookmaker", "timestamp"),
        # /live latest-snapshot lookup and the poller's freshness check.
        Index("ix_sport_time", "sport_key", "timestamp"),
    )
