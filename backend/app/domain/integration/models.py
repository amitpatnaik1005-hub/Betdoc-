"""SQLAlchemy 2.0 ORM models for the integration layer."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base

Money = Numeric(18, 6, asdecimal=False)

def utcnow() -> datetime:
    return datetime.now(UTC)


class BetStatus(StrEnum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"


class ApiCredential(Base):
    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_key: Mapped[str] = mapped_column(Text)  # Fernet token; never queried directly
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventResearch(Base):
    __tablename__ = "event_research"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    expected_margin: Mapped[float] = mapped_column(Money)
    spread_line: Mapped[float] = mapped_column(Money)
    decimal_odds: Mapped[float] = mapped_column(Money)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PredictionRecord(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    model_name: Mapped[str] = mapped_column(String(64))
    probability: Mapped[float] = mapped_column(Money)
    implied_probability: Mapped[float] = mapped_column(Money)
    edge: Mapped[float] = mapped_column(Money)
    decimal_odds: Mapped[float] = mapped_column(Money)
    recommended_stake: Mapped[float] = mapped_column(Money)
    approved: Mapped[bool] = mapped_column(Boolean)
    risk_reason: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BetRecord(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(primary_key=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    stake: Mapped[float] = mapped_column(Money)
    decimal_odds: Mapped[float] = mapped_column(Money)
    status: Mapped[BetStatus] = mapped_column(Enum(BetStatus, native_enum=False, length=8), default=BetStatus.OPEN, index=True)
    pnl: Mapped[float | None] = mapped_column(Money, nullable=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SubsystemConstraint(Base):
    __tablename__ = "subsystem_constraints"

    subsystem: Mapped[str] = mapped_column(String(32), primary_key=True)
    flag: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
