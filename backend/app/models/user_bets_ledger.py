"""The user's own bets, placed by hand at a bookmaker, and the scores that settle them (Group 69).

* ``user_placed_bets``: one row per slip the user says they placed ("I placed this bet"): where,
  the stake in rupees, the odds they actually got, when. Systems (Trixie .. Heinz) keep their unit
  stake; ``stake_inr`` is always the total. Settled by ``app.services.user_pnl_tracker`` into WON,
  LOST, VOID, HALF_WON or HALF_LOST with the exact return; a cashout taken at the book is CASHED_OUT.
* ``user_placed_legs``: its legs, each with the canonical market (``Totals 2.5``, ``Asian Handicap
  -0.25``) and its own result, so a parlay with a void leg or a half-won handicap pays exactly.
* ``fixture_scores``: final scores (and abandonments), from The Odds API's scores feed or entered by
  an administrator. One row per fixture; the source and who recorded it are kept.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models import Base, utc_now

MONEY = Numeric(18, 2)
ODDS = Numeric(12, 4)


class PlacedBookmaker(StrEnum):
    PARIMATCH = "PARIMATCH"
    ONEXBET = "1XBET"
    STAKE = "STAKE"
    PINNACLE = "PINNACLE"
    BETFAIR = "BETFAIR"
    OTHER = "OTHER"


class PlacedStructure(StrEnum):
    SINGLE = "SINGLE"
    DOUBLE = "DOUBLE"
    TREBLE = "TREBLE"
    ACCUMULATOR = "ACCUMULATOR"
    TRIXIE = "TRIXIE"
    YANKEE = "YANKEE"
    CANADIAN = "CANADIAN"
    HEINZ = "HEINZ"
    PATENT = "PATENT"  # Group 77
    SUPER_HEINZ = "SUPER_HEINZ"
    GOLIATH = "GOLIATH"


class PlacedStatus(StrEnum):
    PENDING = "PENDING"
    WON = "WON"
    HALF_WON = "HALF_WON"
    VOID = "VOID"
    HALF_LOST = "HALF_LOST"
    LOST = "LOST"
    CASHED_OUT = "CASHED_OUT"


class SettlementSource(StrEnum):
    """Group 73: how a bet reached its final status."""

    AUTOMATED = "AUTOMATED"  # scores or market results, through settle_pending
    CASHOUT = "CASHOUT"  # the user took the bookmaker's cashout
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"  # an administrator set the result


class ScoreStatus(StrEnum):
    FINAL = "FINAL"
    ABANDONED = "ABANDONED"  # every leg on it is void
    POSTPONED = "POSTPONED"  # void once the books' wait (48h) has passed


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(m.value) for m in enum)})"


class UserPlacedBet(Base):
    __tablename__ = "user_placed_bets"
    __table_args__ = (
        CheckConstraint("stake_inr > 0", name="ck_user_placed_bets_stake_positive"),
        CheckConstraint("placed_odds IS NULL OR placed_odds >= 1", name="ck_user_placed_bets_odds"),
        CheckConstraint(_in("status", PlacedStatus), name="ck_user_placed_bets_status"),
        CheckConstraint(_in("structure", PlacedStructure), name="ck_user_placed_bets_structure"),
        CheckConstraint(_in("bookmaker", PlacedBookmaker), name="ck_user_placed_bets_bookmaker"),
        Index("ix_user_placed_bets_user_placed", "user_id", "placed_at"),
        Index("ix_user_placed_bets_user_status", "user_id", "status"),
        Index("ix_user_placed_bets_user_settled", "user_id", "settled_at"),
        Index("ix_user_placed_bets_feedback_due", "status", "feedback_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    slip_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the Ashoka slip it came from
    vetting_audit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)  # Group 72: the 14-pillar audit it was placed from
    booking_code: Mapped[str | None] = mapped_column(String(32), nullable=True)  # Group 72: the code the bookmaker itself issued for the slip
    # Group 73: closing-line value against the sharp books, how it settled, and why it lost
    closing_odds: Mapped[Decimal | None] = mapped_column(ODDS, nullable=True)  # the sharp books' closing price of the whole slip
    clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # placed / closing - 1, in percent
    clv_sharp_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # placed x de-vigged closing probability - 1, in percent
    settlement_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # SettlementSource
    root_cause_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)  # RootCauseTag (NONE for a bet that did not lose)
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # when attribution ran; NULL: still to do
    source: Mapped[str] = mapped_column(String(16), default="ASHOKA")  # ASHOKA | MANUAL
    bookmaker: Mapped[str] = mapped_column(String(16))  # PlacedBookmaker
    bookmaker_name: Mapped[str | None] = mapped_column(String(64), nullable=True)  # OTHER: which one
    structure: Mapped[str] = mapped_column(String(16))  # PlacedStructure
    stake_inr: Mapped[Decimal] = mapped_column(MONEY)  # the total staked
    unit_stake_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)  # systems: per line
    placed_odds: Mapped[Decimal | None] = mapped_column(ODDS, nullable=True)  # straight slips: what the book gave
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default=PlacedStatus.PENDING.value)
    return_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    pnl_inr: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, server_default=func.now())


class UserPlacedLeg(Base):
    __tablename__ = "user_placed_legs"
    __table_args__ = (
        CheckConstraint("odds >= 1", name="ck_user_placed_legs_odds"),
        Index("ix_user_placed_legs_bet", "bet_id"),
        Index("ix_user_placed_legs_fixture_result", "fixture_id", "result"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_placed_bets.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    fixture_id: Mapped[str] = mapped_column(String(128))
    home: Mapped[str] = mapped_column(String(128))
    away: Mapped[str] = mapped_column(String(128))
    sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    league: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market: Mapped[str] = mapped_column(String(64))  # canonical market type
    selection: Mapped[str] = mapped_column(String(16))
    odds: Mapped[Decimal] = mapped_column(ODDS)
    fair_probability: Mapped[float | None] = mapped_column(Float, nullable=True)  # Ashoka's, when it suggested the leg
    closing_odds: Mapped[Decimal | None] = mapped_column(ODDS, nullable=True)  # Group 73: the sharp book's last price before kickoff
    closing_fair_probability: Mapped[float | None] = mapped_column(Float, nullable=True)  # that market, Shin de-vigged
    closing_book: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result: Mapped[str] = mapped_column(String(12), default=PlacedStatus.PENDING.value)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    home_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FixtureScore(Base):
    __tablename__ = "fixture_scores"
    __table_args__ = (
        CheckConstraint(_in("status", ScoreStatus), name="ck_fixture_scores_status"),
        CheckConstraint("(home_goals IS NULL AND away_goals IS NULL) OR (home_goals >= 0 AND away_goals >= 0)", name="ck_fixture_scores_goals"),
        Index("ix_fixture_scores_kickoff", "kickoff"),
    )

    fixture_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    home: Mapped[str] = mapped_column(String(128))
    away: Mapped[str] = mapped_column(String(128))
    sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    home_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default=ScoreStatus.FINAL.value)
    source: Mapped[str] = mapped_column(String(32))  # odds_api_scores | admin
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
