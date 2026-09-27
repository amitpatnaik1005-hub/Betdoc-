import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utc_now


class MarketTickModel(Base):
    __tablename__ = "market_ticks"
    __table_args__ = (
        Index(
            "ix_market_ticks_market_lookup",
            "match_id", "market_type", "selection_id", "odds_type", "timestamp",
        ),
        Index("ix_market_ticks_timestamp", "timestamp"),
        CheckConstraint("odds_type IN ('BACK', 'LAY')", name="odds_type_valid"),
        CheckConstraint("decimal_odds >= 1.0", name="decimal_odds_min"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bookmaker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    match_id: Mapped[str] = mapped_column(String(128), nullable=False)
    selection_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market_type: Mapped[str] = mapped_column(String(64), nullable=False)
    odds_type: Mapped[str] = mapped_column(String(8), nullable=False)
    decimal_odds: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    line: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_sharp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
