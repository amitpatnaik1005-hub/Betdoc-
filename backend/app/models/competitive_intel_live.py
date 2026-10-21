"""Group 85: live competitive market intelligence records (Commanders KARNA & PANINI).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Analytics Fleet.

* ``competitor_public_consensus_snapshots``: per live fixture and Match Odds selection, Parimatch's and 1xBet's
  prices and margins, the sharp zero-vig fair price, BetDoc's probability and EV, and, where measured, the public
  ticket share, the money share and the opening price (with the divergence, RLM and trap they imply). Rebuilt by
  every sync from the live board.
* ``bookmaker_margin_benchmarks``: Parimatch and 1xBet per sport: mean measured overround, payout efficiency and
  how often each has the better price; the sharp zero-vig benchmark beside them.
* ``competitor_signal_feed_items``: public picks graded by BetDoc: DERIVED from the measured splits (a public trap,
  a sharp-verified side, a pick at a high margin) or RECORDED by an administrator from a named outside source.
  A sync rebuilds the derived rows and re-grades the recorded ones; it never deletes a recorded pick.

Stakes are INR (``Numeric(16, 2)``), sized with KUMBHA's policy at the reference bankroll; the USD figure uses
TODAR MAL's fresh rate and is empty when no fresh rate exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Numeric, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utc_now

VERDICTS = ("SHARP_CONTRARIAN", "PUBLIC_TRAP", "SHARP_CONSENSUS", "NEUTRAL")
SIGNAL_VERDICTS = ("PUBLIC_TRAP", "SHARP_VERIFIED", "CAUTION_HIGH_VIG", "NEUTRAL")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CompetitorPublicConsensusSnapshot(Base):
    """One selection on the live board as KARNA reads it. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "competitor_public_consensus_snapshots"
    __table_args__ = (
        CheckConstraint("public_ticket_pct IS NULL OR (public_ticket_pct >= 0 AND public_ticket_pct <= 100)", name="ticket_pct_bounded"),
        CheckConstraint("sharp_money_pct IS NULL OR (sharp_money_pct >= 0 AND sharp_money_pct <= 100)", name="money_pct_bounded"),
        CheckConstraint("parimatch_odds IS NULL OR parimatch_odds > 1", name="parimatch_odds_valid"),
        CheckConstraint("one_xbet_odds IS NULL OR one_xbet_odds > 1", name="onexbet_odds_valid"),
        CheckConstraint("parimatch_odds IS NOT NULL OR one_xbet_odds IS NOT NULL", name="a_book_quotes"),
        CheckConstraint(_in("verdict", VERDICTS), name="verdict_known"),
        Index("ix_comp_consensus_sport_trap", "sport", "public_trap_flag"),
        Index("ix_comp_consensus_match_selection", "match_id", "market_type", "selection", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id: Mapped[str] = mapped_column(String(128), index=True)
    sport: Mapped[str] = mapped_column(String(64), index=True)  # the sport label (Cricket, Football ...)
    sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    league: Mapped[str | None] = mapped_column(String(128), nullable=True)
    home_team: Mapped[str] = mapped_column(String(128))
    away_team: Mapped[str] = mapped_column(String(128))
    kickoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_type: Mapped[str] = mapped_column(String(64))
    selection: Mapped[str] = mapped_column(String(16))  # HOME / DRAW / AWAY
    selection_label: Mapped[str] = mapped_column(String(160))
    public_ticket_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    sharp_money_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)  # the measured money (handle) share
    public_source: Mapped[str | None] = mapped_column(String(96), nullable=True)  # who measured the shares
    divergence_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    rlm_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    opening_odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    parimatch_odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    one_xbet_odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    sharp_fair_odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)  # 1 / the de-vigged sharp consensus
    parimatch_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    one_xbet_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    best_bookmaker: Mapped[str] = mapped_column(String(32))
    best_odds: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    betdoc_true_prob: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    betdoc_ev_pct: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    public_trap_flag: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    contrarian_edge_score: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    verdict: Mapped[str] = mapped_column(String(32), default="NEUTRAL")
    plain_english_summary: Mapped[str] = mapped_column(String(600))
    recommended_stake_inr: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"))
    recommended_stake_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    source_provider: Mapped[str] = mapped_column(String(64), default="KARNA_CONSENSUS_ENGINE")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BookmakerMarginBenchmark(Base):
    """A book's measured margin and price share in one sport. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "bookmaker_margin_benchmarks"
    __table_args__ = (
        UniqueConstraint("bookmaker_key", "sport", "market_type", name="uq_bookmaker_margin_sport_market"),
        Index("ix_bookmaker_margin_key_sport", "bookmaker_key", "sport"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bookmaker_key: Mapped[str] = mapped_column(String(32), index=True)
    bookmaker_display: Mapped[str] = mapped_column(String(64))
    sport: Mapped[str] = mapped_column(String(64), index=True)
    market_type: Mapped[str] = mapped_column(String(64))
    measured_overround_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    payout_efficiency_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    best_price_share_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)  # none for the zero-vig benchmark
    sample_size: Mapped[int] = mapped_column(Integer)  # markets measured
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CompetitorSignalFeedItem(Base):
    """A public pick and BetDoc's grade of it. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "competitor_signal_feed_items"
    __table_args__ = (
        Index("ix_comp_signal_sport_verdict", "sport", "betdoc_verdict"),
        UniqueConstraint("origin", "source_site", "match_id", name="uq_comp_signal_origin_source_match"),
        CheckConstraint("origin IN ('DERIVED', 'RECORDED')", name="origin_known"),
        CheckConstraint(_in("betdoc_verdict", SIGNAL_VERDICTS), name="verdict_known"),
        CheckConstraint("public_backing_pct >= 0 AND public_backing_pct <= 100", name="backing_bounded"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    origin: Mapped[str] = mapped_column(String(16))
    source_site: Mapped[str] = mapped_column(String(96))
    sport: Mapped[str] = mapped_column(String(64), index=True)
    match_id: Mapped[str] = mapped_column(String(128), index=True)
    match_title: Mapped[str] = mapped_column(String(260))
    public_selection: Mapped[str] = mapped_column(String(16))  # HOME / DRAW / AWAY
    public_pick: Mapped[str] = mapped_column(String(160))
    public_backing_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    betdoc_verdict: Mapped[str] = mapped_column(String(32))
    betdoc_recommended_pick: Mapped[str] = mapped_column(String(160))
    best_bookmaker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    best_odds: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    betdoc_ev_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)  # of the recommended pick
    plain_english_rationale: Mapped[str] = mapped_column(String(600))
    stake_inr: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0.00"))
    stake_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    graded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
