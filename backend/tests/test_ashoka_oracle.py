"""Group 69: ASHOKA, the Oracle. Vetted slips, odds shopping, the cashout advisor, the user's P&L.

The brief's five proofs first:

* the anti-correlation gate rejects legs from the same match (and says how they correlate);
* the "1000%" confidence filter drops a multiple whose joint EV is under +7.5%, however likely it is;
* the cashout advisor says HOLD when the offer is under 85% of the slip's fair value;
* the P&L tracker moves today, this week and this month correctly as bets settle, Asian-handicap
  half wins and half losses included;
* Parimatch against 1xBet comes out in exact rupees (3.85 vs 4.15 on ₹1,000 is ₹300, +7.79%).

Then the pieces under them: the market grammar and settlement table, the scoreline models, the
10,000-path Monte Carlo against the exact answer, system bets, the Odds API normaliser's totals,
handicaps and BTTS, live slip generation from Redis end to end, settlement of parlays and systems
(voids, halves, early losses, postponements, hand-typed legs), the scores feed and its quota guards,
and the trend scan (sharp steam, public trap, hybrid; no public share unless one was measured).
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.adapters.ingestion.base import IngestionBatch, SourcePayload
from app.adapters.ingestion.odds_api_adapter import odds_api_markets
from app.api.deps import get_current_admin, get_current_user
from app.api.v1 import oracle as oracle_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.bookmakers.adapters import canonical_bookmaker, slip_line
from app.domain.oracle.cashout_advisor import Advice, OpenLeg, SettledLeg, advise, certainty_equivalent
from app.domain.oracle.markets import LegResult, MarketKind, MarketRef, parse_market, payout_factor, settle_selection
from app.domain.oracle.match_model import MarketTargets, build_models, leg_correlation, one_x_two, outcome_distribution
from app.domain.oracle.parlay_engine import (
    AntiCorrelationGate,
    GateThresholds,
    LegCandidate,
    ParlayEngine,
    Quote,
    ScenarioEngine,
    SlipKind,
    Tier,
    lines_of,
)
from app.domain.oracle.slip_formatter import LegPrice, book_view, compare, quick_copy
from app.models import User
from app.models.cfo_vault import BankrollAccount, MarketResult
from app.models.hive_bots import TradingBot
from app.models.omni_vault import OmniFleetSource
from app.models.popular_picks import ParlayReviewGateModel, PopularParlayModel
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.ashoka import PlaceBetRequest, ScoreIn
from app.services import ashoka_market
from app.services import user_pnl_tracker as tracker
from app.services.omni_normalizer import OmniNormalizer
from app.services.oracle_scores import parse_scores, poll_scores

D = Decimal
TABLES = [
    User.__table__, TradingBot.__table__, BankrollAccount.__table__, OmniFleetSource.__table__, MarketResult.__table__, UserPlacedBet.__table__, UserPlacedLeg.__table__, FixtureScore.__table__,
    PopularParlayModel.__table__, ParlayReviewGateModel.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-ashoka"
NOW = datetime(2026, 10, 9, 9, 30, tzinfo=UTC)  # 15:00 in Kolkata, a Friday
IST = ZoneInfo("Asia/Kolkata")


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(TABLES))))
        await engine.dispose()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):  # claimed by tests/conftest.py
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_MARK, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya", "ORACLE_TIMEZONE": "Asia/Kolkata", "ASHOKA_MC_PATHS": 10_000})


@pytest_asyncio.fixture
async def user(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions() as session:
        row = User(username=f"ashoka_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(row)
        await session.commit()
        return row


def market_leg(fixture: str, p: float, prices: dict[str, float], *, selection: str = "HOME", market: MarketRef | None = None, home: str | None = None, away: str | None = None,
               league: str = "EPL", kickoff: datetime | None = None, seen: datetime = NOW) -> LegCandidate:
    """A leg priced by the market alone (no scoreline model): its probability is exactly ``p``."""
    return LegCandidate(
        fixture, home or f"{fixture}-home", away or f"{fixture}-away", market or MarketRef(MarketKind.MATCH_ODDS), selection,
        {book: Quote(book, price, seen) for book, price in prices.items()}, consensus=p, consensus_books=3, models=None,
        sport_key="soccer_epl", league=league, kickoff=kickoff or NOW + timedelta(hours=6),
    )


def thresholds() -> GateThresholds:
    return GateThresholds(min_joint_ev=0.075, min_joint_probability=0.55, max_quote_age_seconds=180, min_books=2)


# ================================================================ 1. the anti-correlation gate
def test_the_anti_correlation_gate_rejects_legs_from_the_same_match() -> None:
    """Arsenal to win and Over 2.5 in Arsenal v Chelsea move together (+), Arsenal and Chelsea to win
    are opposites (-): both pairs are related contingencies a bookmaker refuses at checkout."""
    models = build_models(MarketTargets(0.47, 0.26, 0.27, {2.5: 0.52}))
    def leg(market: MarketRef, selection: str, fixture: str = "fx-ars-che", home: str = "Arsenal", away: str = "Chelsea") -> LegCandidate:
        return LegCandidate(fixture, home, away, market, selection, {"1xbet": Quote("1xbet", 2.0, NOW)}, 0.5, 3, models, "soccer_epl", "EPL", NOW + timedelta(hours=5))

    home, over, away = leg(MarketRef(MarketKind.MATCH_ODDS), "HOME"), leg(MarketRef(MarketKind.TOTALS, 2.5), "OVER"), leg(MarketRef(MarketKind.MATCH_ODDS), "AWAY")
    gate = AntiCorrelationGate()
    result = gate.check([home, over])
    assert not result.ok and [c.reason for c in result.clashes] == ["SAME_FIXTURE"] and result.clashes[0].correlation > 0.1
    opposite = gate.check([home, away])
    assert {c.reason for c in opposite.clashes} == {"SAME_FIXTURE", "NEGATIVE_CORRELATION"} and opposite.clashes[0].correlation < -0.4
    # the same team on two "fixtures" within a day and a half is one match under two ids
    alias = leg(MarketRef(MarketKind.MATCH_ODDS), "HOME", fixture="fx-other-id", home="Arsenal FC", away="Chelsea")
    alias = LegCandidate(alias.fixture_id, "Arsenal", "Chelsea", alias.market, "HOME", alias.quotes, 0.5, 3, models, "soccer_epl", "EPL", NOW + timedelta(hours=6))
    assert [c.reason for c in gate.check([home, alias]).clashes] == ["SAME_TEAM"]
    # independent fixtures pass
    assert gate.check([home, leg(MarketRef(MarketKind.MATCH_ODDS), "HOME", fixture="fx-liv-eve", home="Liverpool", away="Everton")]).ok
    # and the engine turns a same-match slip away, whatever its EV
    engine = ParlayEngine(thresholds(), paths=10_000)
    slip = engine.evaluate([home, over], SlipKind.DOUBLE, NOW)
    assert slip is not None and slip.verdict.tier is Tier.REJECTED and not slip.verdict.checks["independent"]
    assert any(reason.startswith("SAME_FIXTURE") for reason in slip.verdict.reasons)
    # the generator never even pairs two legs of one fixture
    report = engine.generate([market_leg("fx-a", 0.80, {"1xbet": 1.45}), market_leg("fx-a", 0.80, {"1xbet": 1.45}, market=MarketRef(MarketKind.BTTS), selection="YES")], NOW)
    assert all(len({leg.fixture_id for leg in s.legs}) == len(s.legs) for s in report.slips)


# ================================================================ 2. the "1000%" confidence filter
def test_the_confidence_filter_drops_a_parlay_under_seven_and_a_half_percent_ev() -> None:
    """Two 80% legs: a 64% double, comfortably over the 55% probability bar. At 1.2806 each the joint EV
    is +5%: dropped. At 1.3288 each it is +13%: "1000% vetted". The bar is the EV, not the likelihood."""
    thin = [market_leg("fx-1", 0.80, {"1xbet": 1.2806}), market_leg("fx-2", 0.80, {"1xbet": 1.2806})]
    fat = [market_leg("fx-3", 0.80, {"1xbet": 1.3288}), market_leg("fx-4", 0.80, {"1xbet": 1.3288})]
    engine = ParlayEngine(thresholds(), paths=10_000)
    under = engine.evaluate(thin, SlipKind.DOUBLE, NOW)
    assert under is not None and under.sim.analytic_ev == pytest.approx(0.05, abs=1e-3)
    assert under.verdict.tier is Tier.REJECTED and not under.verdict.checks["joint_ev"] and under.verdict.checks["joint_probability"]
    over = engine.evaluate(fat, SlipKind.DOUBLE, NOW)
    assert over is not None and over.sim.analytic_ev == pytest.approx(0.13, abs=1e-3)
    assert over.verdict.tier is Tier.VETTED and all(over.verdict.checks.values())
    assert over.sim.paths == 10_000 and abs(over.sim.joint_ev - over.sim.analytic_ev) < 4 * over.sim.joint_ev_se
    # the generator keeps the vetted double and drops the thin one (and never shows a rejected slip)
    report = engine.generate([*thin, *fat], NOW)
    kept = {frozenset(leg.fixture_id for leg in s.legs) for s in report.slips if s.kind is SlipKind.DOUBLE}
    assert frozenset({"fx-3", "fx-4"}) in kept and frozenset({"fx-1", "fx-2"}) not in kept
    assert all(s.verdict.tier is not Tier.REJECTED and s.sim.joint_ev >= 0.075 for s in report.slips)


def test_value_tier_rejection_staleness_and_dissent() -> None:
    engine = ParlayEngine(thresholds(), paths=10_000, max_stake_pct=0.02, value_max_stake_pct=0.005)
    # +EV but a 30% double: VALUE, staked at no more than 0.5% of bankroll
    long = engine.evaluate([market_leg("fx-5", 0.55, {"1xbet": 2.05}), market_leg("fx-6", 0.55, {"1xbet": 2.05})], SlipKind.DOUBLE, NOW)
    assert long is not None and long.verdict.tier is Tier.VALUE and not long.verdict.checks["joint_probability"] and 0 < long.stake_fraction <= 0.005
    # a price four minutes old is never vetted
    stale = engine.evaluate([market_leg("fx-7", 0.80, {"1xbet": 1.40}, seen=NOW - timedelta(minutes=4))], SlipKind.SINGLE, NOW)
    assert stale is not None and stale.verdict.tier is Tier.REJECTED and not stale.verdict.checks["fresh_odds"]
    # a model that prices the leg below its odds blocks VETTED
    models = build_models(MarketTargets(0.40, 0.28, 0.32))
    dissent = LegCandidate("fx-8", "H", "A", MarketRef(MarketKind.MATCH_ODDS), "HOME", {"1xbet": Quote("1xbet", 2.45, NOW)}, 0.42, 3, models, "soccer_epl", "EPL", NOW + timedelta(hours=3))
    verdict = engine.evaluate([dissent], SlipKind.SINGLE, NOW)
    assert verdict is not None and not verdict.verdict.checks["models_agree"] and verdict.verdict.tier is not Tier.VETTED


# ================================================================ 3. the cashout advisor
def test_the_cashout_advisor_holds_when_the_offer_is_under_85_percent_of_fair_value() -> None:
    """₹1,000 on a treble at 1.80 x 1.60 x 2.00, two legs won, the last a 50/50: the slip pays ₹5,760 with
    probability 0.5, fair value ₹2,880. An offer of 84% of that is a 16% cut: HOLD."""
    won = [SettledLeg("Arsenal", 1.8, LegResult.WON), SettledLeg("Inter", 1.6, LegResult.WON)]
    last = [OpenLeg("Real Madrid", 2.0, 0.5)]
    fair = 1000 * 1.8 * 1.6 * 2.0 * 0.5
    advice = advise(1000, won, last, offer=0.84 * fair, bankroll=50_000)
    assert advice.advice is Advice.HOLD and advice.fair_value == D("2880.00") and advice.potential_payout == D("5760.00")
    assert advice.offer_ratio == pytest.approx(0.84) and advice.implied_margin == pytest.approx(0.16)
    assert "penalised" in advice.reasons[-1]
    assert advise(1000, won, last, offer=0.8499 * fair).advice is Advice.HOLD
    assert advise(1000, won, last, offer=fair).advice is Advice.CASH_OUT  # at fair value the book pays what the slip is worth
    # between the hold line and fair value, the bankroll decides: a thin one is worth the certainty
    small, big = certainty_equivalent(3_000, 5760, 0.5), certainty_equivalent(10_000_000, 5760, 0.5)
    assert small < 0.95 * fair < big
    assert advise(1000, won, last, offer=0.95 * fair, bankroll=3_000).advice is Advice.CASH_OUT
    assert advise(1000, won, last, offer=0.95 * fair, bankroll=10_000_000).advice is Advice.HOLD


def test_the_hedge_locks_the_same_money_whatever_happens() -> None:
    won = [SettledLeg("Arsenal", 1.8, LegResult.WON), SettledLeg("Inter", 1.6, LegResult.WON)]
    payout = 1000 * 1.8 * 1.6 * 2.0
    back = advise(1000, won, [OpenLeg("Real Madrid", 2.0, 0.55, hedge_back=(("Draw", 3.6), ("Sevilla", 4.5)), hedge_book="pinnacle")])
    assert back.advice is Advice.HEDGE_LEG and back.hedge is not None and back.hedge.kind == "BACK_OTHERS"
    stakes = dict(back.hedge.stakes)
    for outcome, price in (("Draw", 3.6), ("Sevilla", 4.5)):
        assert float(stakes[outcome]) * price == pytest.approx(payout, abs=0.05)  # each hedge pays the slip's payout
    outlay = float(back.hedge.total_outlay)
    assert float(back.hedge.locked_profit) == pytest.approx(payout - outlay - 1000, abs=0.01)
    lay = advise(1000, won, [OpenLeg("Real Madrid", 2.0, 0.55, lay_odds=1.86, lay_commission=0.05)])
    assert lay.hedge is not None and lay.hedge.kind == "LAY"
    lay_stake = float(lay.hedge.stakes[0][1])
    win_case = payout - lay_stake * 0.86 - 1000
    lose_case = lay_stake * 0.95 - 1000
    assert win_case == pytest.approx(lose_case, abs=0.02) == pytest.approx(float(lay.hedge.locked_profit), abs=0.02)


# ================================================================ 4. the P&L scorecard
async def _bet(sessions: async_sessionmaker[AsyncSession], user: User, body: dict[str, Any]) -> uuid.UUID:
    async with sessions() as session:
        bet = await tracker.record_bet(session, user.id, PlaceBetRequest.model_validate(body), NOW)
        await session.commit()
        return bet.id


async def _score(sessions: async_sessionmaker[AsyncSession], home: str, away: str, hg: int | None, ag: int | None, kickoff: datetime, status: str = "FINAL") -> None:
    async with sessions() as session:
        await tracker.record_score(session, ScoreIn(home=home, away=away, kickoff=kickoff, home_goals=hg, away_goals=ag, status=status), source="admin", by=None, now=NOW)
        await session.commit()


def _single(home: str, away: str, market: str, selection: str, odds: str, stake: str, kickoff: datetime, **extra: Any) -> dict[str, Any]:
    return {"bookmaker": "PARIMATCH", "structure": "SINGLE", "stake_inr": stake, "placed_odds": odds,
            "legs": [{"home": home, "away": away, "market": market, "selection": selection, "odds": odds, "kickoff": kickoff.isoformat(), "league": "EPL"}], **extra}


@pytest.mark.asyncio
async def test_the_pnl_scorecard_updates_today_week_and_month_as_bets_settle(sessions: async_sessionmaker[AsyncSession], user: User) -> None:
    """Five bets settled at different times (Kolkata clock; the week starts Monday 5 October):
    today a handicap half loss (-₹500) and a half win (+₹500), on Tuesday a double (+₹1,210), on
    2 October a lost BTTS single (-₹400), in September a won single (+₹300)."""
    at = lambda day, hour: datetime(2026, day[0], day[1], hour, 0, tzinfo=IST).astimezone(UTC)  # noqa: E731
    old = await _bet(sessions, user, _single("Fulham", "Brentford", "Match Odds", "HOME", "2.00", "300", at((9, 19), 15)))
    month = await _bet(sessions, user, _single("Wolves", "Spurs", "BTTS", "YES", "1.70", "400", at((10, 1), 20)))
    week = await _bet(sessions, user, {
        "bookmaker": "1XBET", "structure": "DOUBLE", "stake_inr": "500", "placed_odds": "3.42",
        "legs": [
            {"home": "Leeds", "away": "Burnley", "market": "Match Odds", "selection": "HOME", "odds": "1.80", "kickoff": at((10, 5), 20).isoformat(), "league": "EPL"},
            {"home": "Roma", "away": "Lazio", "market": "Totals 2.5", "selection": "OVER", "odds": "1.90", "kickoff": at((10, 5), 22).isoformat(), "league": "Serie A"},
        ],
    })
    half_lost = await _bet(sessions, user, _single("Arsenal", "Chelsea", "Asian Handicap -0.25", "HOME", "1.90", "1000", at((10, 8), 21)))
    half_won = await _bet(sessions, user, _single("Everton", "Villa", "Asian Handicap +0.25", "HOME", "2.00", "1000", at((10, 8), 22)))

    async def settle(when: datetime) -> tracker.SettleReport:
        return await tracker.settle_pending(sessions, when, user_id=user.id)

    await _score(sessions, "Fulham", "Brentford", 2, 0, at((9, 19), 15))
    assert (await settle(at((9, 20), 9))).bets == 1
    await _score(sessions, "Wolves", "Spurs", 1, 0, at((10, 1), 20))
    await settle(at((10, 2), 12))
    await _score(sessions, "Leeds", "Burnley", 2, 0, at((10, 5), 20))
    assert (await settle(at((10, 5), 23))).bets == 0  # the double waits for its second leg
    await _score(sessions, "Roma", "Lazio", 3, 1, at((10, 5), 22))
    await settle(at((10, 6), 9))
    await _score(sessions, "Arsenal", "Chelsea", 1, 1, at((10, 8), 21))
    await settle(at((10, 9), 10))
    await _score(sessions, "Everton", "Villa", 2, 2, at((10, 8), 22))
    await settle(at((10, 9), 11))

    async with sessions() as session:
        bets, legs = await tracker.user_bets(session, user.id)
    by_id = {b.id: b for b in bets}
    assert (by_id[half_lost].status, by_id[half_lost].return_inr, by_id[half_lost].pnl_inr) == ("HALF_LOST", D("500.00"), D("-500.00"))
    assert (by_id[half_won].status, by_id[half_won].return_inr, by_id[half_won].pnl_inr) == ("HALF_WON", D("1500.00"), D("500.00"))
    assert (by_id[week].status, by_id[week].return_inr) == ("WON", D("1710.00"))  # ₹500 at the 3.42 the book gave
    assert (by_id[month].status, by_id[old].status) == ("LOST", "WON")

    card = tracker.scorecard(bets, at((10, 9), 15), "Asia/Kolkata", tax_rate=0.30)
    today, wk, mo, alltime = (card["periods"][p] for p in ("today", "week", "month", "all_time"))
    assert (today["pnl_inr"], today["staked_inr"], today["bets"], today["win_rate"]) == ("0.00", "2000.00", 2, 0.5)
    assert (wk["pnl_inr"], wk["bets"]) == ("1210.00", 3) and (mo["pnl_inr"], mo["bets"]) == ("810.00", 4) and (alltime["pnl_inr"], alltime["bets"]) == ("1110.00", 5)
    assert alltime["roi"] == pytest.approx(1110 / 3200, abs=1e-4) and alltime["returned_inr"] == "4310.00"
    assert (wk["tax_inr"], wk["net_after_tax_inr"]) == ("363.00", "847.00") and today["tax_inr"] == "0"
    assert card["streak"]["label"] == "W1"  # the half win came last; before it, the half loss
    twin = tracker.twin_profile(bets, legs, min_bets=1)
    assert twin["segments"]["structure"]["DOUBLE"]["pnl_inr"] == "1210.00" and twin["segments"]["league"]["Serie A"]["bets"] == 1
    assert any(item["segment"] == "DOUBLE" for item in twin["strengths"]) and any(item["segment"] == "BTTS" for item in twin["leaks"])


# ================================================================ 5. Parimatch against 1xBet, in rupees
def test_parimatch_against_1xbet_comes_out_in_exact_rupees() -> None:
    """A treble at 3.85 on Parimatch (1.10 x 1.75 x 2.00) and 4.15 on 1xBet (1.25 x 1.66 x 2.00):
    on ₹1,000 that is ₹3,850 against ₹4,150, ₹300 more on 1xBet, +7.79%."""
    legs = [
        LegPrice("f1", "Arsenal", "Chelsea", "Match Odds", "HOME", {"parimatch": 1.10, "1xbet": 1.25}),
        LegPrice("f2", "Real Madrid", "Sevilla", "Totals 2.5", "OVER", {"parimatch": 1.75, "1xbet": 1.66}),
        LegPrice("f3", "Inter", "Napoli", "BTTS", "YES", {"parimatch": 2.00, "1xbet": 2.00, "pinnacle": 2.10}),
    ]
    stake = D("1000")
    views = {v.bookmaker: v for v in (book_view(b, legs, stake) for b in ("parimatch", "1xbet", "stake", "pinnacle", "betfair"))}
    assert (views["parimatch"].odds, views["parimatch"].payout) == (D("3.850"), D("3850.00"))
    assert (views["1xbet"].odds, views["1xbet"].payout) == (D("4.150"), D("4150.00"))
    assert not views["stake"].available and views["stake"].missing and not views["betfair"].available  # unquoted; an exchange takes no multiples
    result = compare(list(views.values()), stake)
    assert result.difference_inr == D("300.00") and result.difference_pct == D("7.79") and result.recommended == "1xbet" and result.best == "1xbet"
    assert result.recommendation.startswith("⭐ Recommend placing on 1xBet for +7.79% higher payout (+₹300 on ₹1,000 stake)")
    assert compare(list(views.values()), D("250")).difference_inr == D("75.00")
    text = quick_copy("ASHOKA Treble", legs, "1xbet", views["1xbet"].odds, stake)
    assert "1) Arsenal vs Chelsea — 1X2: Arsenal @ 1.250  [W1]" in text and "Totals: Over 2.5" in text and "Both Teams to Score: YES" in text and "returns ₹4,150" in text


# ================================================================ the market grammar and settlement
def test_markets_parse_and_settle_every_line() -> None:
    assert parse_market("Over/Under 2.5") == MarketRef(MarketKind.TOTALS, 2.5) and parse_market("Asian Handicap -0.25").key == "Asian Handicap -0.25"
    assert parse_market("spreads +1") == MarketRef(MarketKind.ASIAN_HANDICAP, 1.0) and parse_market("Totals 2.3") is None and parse_market("Corners") is None
    ah = lambda line: MarketRef(MarketKind.ASIAN_HANDICAP, line)  # noqa: E731
    table = [
        (ah(-0.25), "HOME", 1, 1, LegResult.HALF_LOST), (ah(-0.25), "AWAY", 1, 1, LegResult.HALF_WON), (ah(-0.25), "HOME", 2, 1, LegResult.WON),
        (ah(-0.75), "HOME", 2, 1, LegResult.HALF_WON), (ah(-0.75), "AWAY", 2, 1, LegResult.HALF_LOST), (ah(-1.0), "HOME", 2, 1, LegResult.VOID),
        (ah(0.25), "HOME", 0, 0, LegResult.HALF_WON), (ah(1.5), "HOME", 0, 1, LegResult.WON), (ah(-0.5), "AWAY", 1, 1, LegResult.WON),
        (MarketRef(MarketKind.TOTALS, 2.25), "OVER", 1, 1, LegResult.HALF_LOST), (MarketRef(MarketKind.TOTALS, 2.75), "OVER", 2, 1, LegResult.HALF_WON),
        (MarketRef(MarketKind.TOTALS, 3.0), "UNDER", 2, 1, LegResult.VOID), (MarketRef(MarketKind.TOTALS, 2.5), "UNDER", 1, 1, LegResult.WON),
        (MarketRef(MarketKind.BTTS), "YES", 1, 0, LegResult.LOST), (MarketRef(MarketKind.DRAW_NO_BET), "AWAY", 1, 1, LegResult.VOID),
        (MarketRef(MarketKind.DOUBLE_CHANCE), "X2", 1, 1, LegResult.WON), (MarketRef(MarketKind.MATCH_ODDS), "DRAW", 0, 0, LegResult.WON),
    ]
    for ref, selection, h, a, want in table:
        assert settle_selection(ref, selection, h, a) is want, (ref.key, selection, h, a)
    assert [payout_factor(r, 1.9) for r in LegResult] == [1.9, 1.45, 1.0, 0.5, 0.0]
    assert MarketRef(MarketKind.TOTALS, 2.5).has_push is False and MarketRef(MarketKind.TOTALS, 2.25).has_push and MarketRef(MarketKind.ASIAN_HANDICAP, -1.0).has_push


def test_the_scoreline_models_fit_the_market_and_price_every_market_consistently() -> None:
    models = build_models(MarketTargets(0.47, 0.26, 0.27, {2.5: 0.52}))
    dc = models.matrices["dixon_coles"]
    assert dc.sum() == pytest.approx(1.0) and one_x_two(dc) == pytest.approx((0.47, 0.26, 0.27), abs=0.02) and models.fit_error < 1e-3
    for ref, selection in ((MarketRef(MarketKind.TOTALS, 2.25), "OVER"), (MarketRef(MarketKind.ASIAN_HANDICAP, -0.75), "HOME"), (MarketRef(MarketKind.BTTS), "NO")):
        assert sum(outcome_distribution(dc, ref, selection).values()) == pytest.approx(1.0)
    over = outcome_distribution(dc, MarketRef(MarketKind.TOTALS, 2.5), "OVER")
    under = outcome_distribution(dc, MarketRef(MarketKind.TOTALS, 2.5), "UNDER")
    assert over[LegResult.WON] + under[LegResult.WON] == pytest.approx(1.0) and over[LegResult.WON] == pytest.approx(0.52, abs=0.02)
    assert leg_correlation(dc, (MarketRef(MarketKind.TOTALS, 2.5), "OVER"), (MarketRef(MarketKind.BTTS), "YES")) > 0.4
    assert set(build_models(MarketTargets(0.5, 0.25, 0.25), home_xg=1.8, away_xg=0.9, home_elo=1700, away_elo=1550).names) == {"poisson", "dixon_coles", "xg", "elo"}


def test_the_monte_carlo_matches_the_exact_answer_and_prices_systems() -> None:
    """Independent legs: 10,000 paths land within four standard errors of the exact EV, for a treble with a
    quarter-line handicap (half results) and for a Trixie; systems stake the right number of lines."""
    assert [len(lines_of(k, n)) for k, n in ((SlipKind.TRIXIE, 3), (SlipKind.YANKEE, 4), (SlipKind.CANADIAN, 5), (SlipKind.HEINZ, 6), (SlipKind.TREBLE, 3))] == [4, 11, 26, 57, 1]
    models = build_models(MarketTargets(0.50, 0.25, 0.25))
    quarter = LegCandidate("fx-q", "H", "A", MarketRef(MarketKind.ASIAN_HANDICAP, -0.25), "HOME", {"1xbet": Quote("1xbet", 2.1, NOW)}, None, 0, models, "soccer_epl", "EPL", NOW)
    legs = [quarter, market_leg("fx-b", 0.6, {"1xbet": 1.8}), market_leg("fx-c", 0.7, {"1xbet": 1.5})]
    engine = ScenarioEngine(10_000)
    for kind in (SlipKind.TREBLE, SlipKind.TRIXIE):
        sim = engine.simulate(legs, [2.1, 1.8, 1.5], kind)
        assert sim.analytic_ev is not None and abs(sim.joint_ev - sim.analytic_ev) < 4 * sim.joint_ev_se, kind
        assert 0 <= sim.p_total_loss <= 1 and sim.var_95 <= sim.joint_ev and sim.cvar_95 <= sim.var_95
    again = engine.simulate(legs, [2.1, 1.8, 1.5], SlipKind.TREBLE)
    assert again.joint_ev == engine.simulate(legs, [2.1, 1.8, 1.5], SlipKind.TREBLE).joint_ev  # the same slip simulates the same
    assert ScenarioEngine(10_000).simulate([market_leg("fx-k", 0.6, {"1xbet": 2.0})], [2.0]).kelly_fraction == pytest.approx(0.2, abs=0.02)  # Kelly: (0.6*2-1)/1


# ================================================================ Garuda: totals, handicaps, BTTS from The Odds API
def test_the_normaliser_reads_totals_handicaps_and_btts(settings: Settings) -> None:
    event = {
        "id": "ev1", "sport_key": "soccer_epl", "commence_time": "2026-10-10T14:00:00Z", "home_team": "Arsenal", "away_team": "Chelsea",
        "bookmakers": [
            {"key": "onexbet", "title": "1xBet", "last_update": "2026-10-09T09:00:00Z", "markets": [
                {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.1}, {"name": "Chelsea", "price": 3.5}, {"name": "Draw", "price": 3.4}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9, "point": 2.5}, {"name": "Under", "price": 1.95, "point": 2.5}, {"name": "Over", "price": 2.6, "point": 3.5}]},
                {"key": "spreads", "outcomes": [{"name": "Arsenal", "price": 1.95, "point": -0.5}, {"name": "Chelsea", "price": 1.9, "point": 0.5}]},
                {"key": "btts", "outcomes": [{"name": "Yes", "price": 1.7}, {"name": "No", "price": 2.1}]},
            ]},
            {"key": "pinnacle", "title": "Pinnacle", "last_update": "2026-10-09T09:00:00Z", "markets": [
                {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.15}, {"name": "Chelsea", "price": 3.6}, {"name": "Draw", "price": 3.5}]},
                {"key": "spreads", "outcomes": [{"name": "Arsenal", "price": 1.9, "point": -0.5}, {"name": "Chelsea", "price": 1.9, "point": 0.75}]},  # sides do not mirror: dropped
            ]},
        ],
    }
    batch = IngestionBatch("odds_api", [SourcePayload("soccer_epl", [event])], NOW, 10, 1, 0)
    report = OmniNormalizer().normalize(batch)
    frames = {q.market_type: q for q in report.quotes}
    assert set(frames) == {"Match Odds", "Totals 2.5", "Asian Handicap -0.5", "BTTS"}  # Totals 3.5 had one side only
    assert {b.bookmaker_id for b in frames["Asian Handicap -0.5"].books} == {"onexbet"}
    assert {k: float(v) for k, v in frames["Totals 2.5"].books[0].prices.items()} == {"OVER": 1.9, "UNDER": 1.95}
    assert {k: float(v) for k, v in frames["BTTS"].books[0].prices.items()} == {"YES": 1.7, "NO": 2.1}
    assert len({q.match_id for q in report.quotes}) == 1  # every market of the fixture under one canonical id
    assert odds_api_markets(settings) == "h2h" and odds_api_markets(settings.model_copy(update={"ODDS_API_MARKETS": "totals,btts,spreads"})) == "h2h,spreads,totals"
    assert canonical_bookmaker("onexbet") == "1xbet" and canonical_bookmaker("betfair_ex_uk") == "betfair" and canonical_bookmaker("bet365") is None
    assert slip_line("parimatch", "Asian Handicap -0.5", "AWAY", "Arsenal", "Chelsea") == {
        "fixture": "Arsenal vs Chelsea", "market": "Asian Handicap: Chelsea (+0.5)", "search_code": "Handicap 2 (+0.5)", "bookmaker": "Parimatch",
    }


# ================================================================ live slips, end to end through Redis
async def _seed_market(redis: Redis, settings: Settings, fixture: str, home: str, away: str, prices: dict[str, dict[str, float]], *, market: str = "Match Odds",
                       kickoff: datetime | None = None, sport: str = "soccer_epl", age: float = 5.0) -> None:
    """Board tick (fixture discovery), the market index, and one book hash entry per bookmaker."""
    from app.core.live_odds import publish_board_ticks  # noqa: PLC0415
    from app.schemas.market import MarketTick  # noqa: PLC0415
    from app.services.aryabhata_pipeline import AryabhataKeys  # noqa: PLC0415

    kickoff = kickoff or datetime.now(UTC) + timedelta(hours=8)
    if market == "Match Odds":
        await publish_board_ticks(redis, [MarketTick(match_id=fixture, home_team=home, away_team=away, market_type="Match Odds", selection="HOME", odds=D("2"), true_probability=D("0.5"),
                                                     is_suspended=False, sport_key=sport, commence_time=kickoff)])
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    await redis.sadd(keys.markets(fixture), market)
    for book, book_prices in prices.items():
        await redis.hset(keys.books(f"{fixture}|{market}"), f"odds_api|{book}", json.dumps({"s": "odds_api", "b": book, "p": book_prices, "t": time.time() - age, "x": False}))


@pytest.mark.asyncio
async def test_live_slips_from_redis_with_bookmaker_views_and_a_recheck(redis: Redis, settings: Settings) -> None:
    """Two EPL favourites and a Serie A one, priced by a sharp book (Pinnacle) and the soft books Ashoka
    bets at. The slips come back staked for the bankroll, with every book's view and the comparison."""
    for fixture, home, away, sport, sharp, soft in (
        ("fx-ars-lee", "Arsenal", "Leeds", "soccer_epl", (1.22, 7.0, 15.0), (1.42, 1.38)),
        ("fx-liv-bur", "Liverpool", "Burnley", "soccer_epl", (1.25, 6.5, 13.0), (1.45, 1.40)),
        ("fx-int-lec", "Inter", "Lecce", "soccer_italy_serie_a", (1.24, 6.8, 13.5), (1.44, 1.41)),
    ):
        h, d, a = sharp
        await _seed_market(redis, settings, fixture, home, away, {
            "pinnacle": {"HOME": h, "DRAW": d, "AWAY": a}, "williamhill": {"HOME": h - 0.01, "DRAW": d, "AWAY": a},
            "onexbet": {"HOME": soft[0], "DRAW": d, "AWAY": a}, "parimatch": {"HOME": soft[1], "DRAW": d, "AWAY": a},
        }, sport=sport)
    now = datetime.now(UTC)
    legs, stats = await ashoka_market.load_candidates(redis, settings, now)
    assert stats["fixtures"] == 3 and {leg.league for leg in legs} == {"EPL", "Serie A"}
    home_legs = [leg for leg in legs if leg.selection == "HOME"]
    assert all(set(leg.quotes) == {"pinnacle", "1xbet", "parimatch"} and leg.consensus_books == 4 and leg.models is not None for leg in home_legs)  # williamhill: consensus only

    payload = await ashoka_market.vetted_slips(redis, settings, now, D("100000"), refresh=True)
    assert payload["slips"], payload["rejected"]
    double = next(s for s in payload["slips"] if s["kind"] == "DOUBLE")
    assert double["tier"] in ("VETTED", "VALUE") and double["simulation"]["paths"] == 10_000 and double["simulation"]["joint_ev"] >= 0.075
    assert int(double["stake_inr"]) <= 100000 * (0.02 if double["tier"] == "VETTED" else 0.005)
    books = {b["bookmaker"]: b for b in double["books"]}
    assert books["1xbet"]["available"] and books["parimatch"]["available"] and not books["betfair"]["available"]
    assert double["comparison"]["recommended"] == "1xbet" and "Recommend placing on 1xBet" in double["comparison"]["recommendation"]
    assert double["odds_age_seconds"] < 60 and "Arsenal vs Leeds" in double["quick_copy"] or "Liverpool vs Burnley" in double["quick_copy"]
    cross = [s for s in payload["slips"] if s["cross_league"]]
    assert all(s["title"].startswith("ASHOKA Multi-League Cross") for s in cross if s["kind"] in ("DOUBLE", "TREBLE"))
    rechecked = await ashoka_market.recheck(redis, settings, now, [leg["leg_id"] for leg in double["legs"]], "DOUBLE", D("100000"))
    assert rechecked is not None and rechecked["slip_id"] == double["slip_id"]
    cached = await ashoka_market.vetted_slips(redis, settings, now + timedelta(seconds=3), D("50000"))
    assert cached["generated_at"] == payload["generated_at"] and cached["age_seconds"] == pytest.approx(3.0, abs=0.5)  # cached work, re-staked


# ================================================================ settlement in depth
@pytest.mark.asyncio
async def test_parlays_and_systems_settle_exactly(sessions: async_sessionmaker[AsyncSession], user: User) -> None:
    k = NOW - timedelta(hours=4)
    leg = lambda home, away, market, sel, odds: {"home": home, "away": away, "market": market, "selection": sel, "odds": odds, "kickoff": k.isoformat()}  # noqa: E731
    # a treble with a void (draw no bet on a draw) and a half-won quarter handicap: 1000 x 1.0 x (1.9+1)/2 x 1.5, scaled to the 4.20 the book gave
    treble = await _bet(sessions, user, {"bookmaker": "1XBET", "structure": "TREBLE", "stake_inr": "1000", "placed_odds": "4.20", "legs": [
        leg("A", "B", "Draw No Bet", "HOME", "1.40"), leg("C", "D", "Asian Handicap +0.25", "HOME", "1.90"), leg("E", "F", "Match Odds", "AWAY", "1.50")]})
    early = await _bet(sessions, user, {"bookmaker": "PARIMATCH", "structure": "DOUBLE", "stake_inr": "200", "legs": [leg("G", "H", "BTTS", "YES", "1.80"), leg("I", "J", "Match Odds", "HOME", "2.00")]})
    trixie = await _bet(sessions, user, {"bookmaker": "OTHER", "bookmaker_name": "Stake", "structure": "TRIXIE", "stake_inr": "400", "legs": [
        leg("K", "L", "Match Odds", "HOME", "2.00"), leg("M", "N", "Match Odds", "HOME", "3.00"), leg("O", "P", "Totals 2.5", "OVER", "1.50")]})
    for home, away, hg, ag in (("A", "B", 1, 1), ("C", "D", 0, 0), ("E", "F", 0, 2), ("G", "H", 2, 0), ("K", "L", 1, 0), ("M", "N", 0, 0), ("O", "P", 2, 2)):
        await _score(sessions, home, away, hg, ag, k)
    report = await tracker.settle_pending(sessions, NOW, user_id=user.id)
    async with sessions() as session:
        rows = {b.id: b for b in (await session.execute(select(UserPlacedBet))).scalars()}
    nominal = 1.40 * 1.90 * 1.50
    assert rows[treble].status == "WON" and rows[treble].return_inr == D(str(round(1000 * 1.0 * 1.45 * 1.5 * 4.20 / nominal, 2)))
    assert (rows[early].status, rows[early].return_inr) == ("LOST", D("0.00"))  # one leg lost: settled before I v J is even played
    # Trixie: unit 100 on 4 lines; K wins, M draws (lost), O over: lines KxM 0, KxO 2*1.5, MxO 0, KxMxO 0 -> 300
    assert (rows[trixie].unit_stake_inr, rows[trixie].status, rows[trixie].return_inr) == (D("100.00"), "HALF_LOST", D("300.00"))
    assert report.bets == 3


@pytest.mark.asyncio
async def test_postponed_abandoned_and_market_results_settle_legs(sessions: async_sessionmaker[AsyncSession], user: User) -> None:
    k = NOW - timedelta(hours=60)
    postponed = await _bet(sessions, user, _single("Q", "R", "Match Odds", "HOME", "1.90", "100", k))
    abandoned = await _bet(sessions, user, _single("S", "T", "Totals 2.5", "OVER", "1.90", "100", k))
    await _score(sessions, "Q", "R", None, None, k, status="POSTPONED")
    await _score(sessions, "S", "T", None, None, k, status="ABANDONED")
    await tracker.settle_pending(sessions, k + timedelta(hours=24), user_id=user.id)
    async with sessions() as session:
        assert (await session.get(UserPlacedBet, postponed)).status == "PENDING"  # inside the 48h the books wait
        assert (await session.get(UserPlacedBet, abandoned)).status == "VOID"
    await tracker.settle_pending(sessions, k + timedelta(hours=49), user_id=user.id)
    async with sessions() as session:
        bet = await session.get(UserPlacedBet, postponed)
        assert (bet.status, bet.return_inr, bet.pnl_inr) == ("VOID", D("100.00"), D("0.00"))
    # a 1X2 leg with Ashoka's fixture id settles from the CFO's market result, no score needed
    ashoka_leg = {**_single("U", "V", "Match Odds", "AWAY", "3.10", "100", k), "legs": [{"fixture_id": "fx-u-v", "home": "U", "away": "V", "market": "Match Odds", "selection": "AWAY", "odds": "3.10"}]}
    by_result = await _bet(sessions, user, ashoka_leg)
    async with sessions() as session:
        session.add(MarketResult(fixture_id="fx-u-v", market="Match Odds", winning_selection="AWAY", source="test"))
        await session.commit()
    await tracker.settle_pending(sessions, NOW, user_id=user.id)
    async with sessions() as session:
        assert (await session.get(UserPlacedBet, by_result)).return_inr == D("310.00")


# ================================================================ the scores feed and its guards
@pytest.mark.asyncio
async def test_scores_are_polled_only_when_a_bet_waits_and_the_quota_allows(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    from app.core.omni_keys import OmniRedisKeys  # noqa: PLC0415

    calls: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, headers={"x-requests-remaining": "480", "x-requests-used": "20"}, json=[
            {"id": "e1", "sport_key": "soccer_epl", "commence_time": (NOW - timedelta(hours=3)).isoformat(), "completed": True, "home_team": "Arsenal", "away_team": "Chelsea",
             "scores": [{"name": "Arsenal", "score": "2"}, {"name": "Chelsea", "score": "0"}]},
        ])

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    cfg = settings.model_copy(update={"ODDS_API_KEY": __import__("pydantic").SecretStr("test-key-not-real")})
    # nothing pending: no call at all
    assert (await poll_scores(sessions, redis, cfg, None, http=http, now=NOW))["polled"] == [] and calls == []
    bet = await _bet(sessions, user, {**_single("Arsenal", "Chelsea", "Match Odds", "HOME", "1.90", "500", NOW - timedelta(hours=3)), "legs": [
        {"home": "Arsenal", "away": "Chelsea", "market": "Match Odds", "selection": "HOME", "odds": "1.90", "kickoff": (NOW - timedelta(hours=3)).isoformat(), "sport_key": "soccer_epl"}]})
    # the fleet's quota is under the floor: no call
    metrics = OmniRedisKeys(cfg.omni_redis_prefix).fleet_metrics("odds_api")
    await redis.hset(metrics, mapping={"quota_remaining": "3", "quota_fraction": "0.006"})
    result = await poll_scores(sessions, redis, cfg, None, http=http, now=NOW)
    assert result["skipped"] == {"soccer_epl": "quota floor"} and calls == []
    await redis.hset(metrics, mapping={"quota_remaining": "500", "quota_fraction": "0.9"})
    result = await poll_scores(sessions, redis, cfg, None, http=http, now=NOW)
    assert result["polled"] == ["soccer_epl"] and result["scores"] == 1 and result["settled_bets"] == 1 and len(calls) == 1
    assert calls[0].url.params["daysFrom"] == "3" and await redis.hget(metrics, "quota_remaining") == "480"  # the spend reaches Fleet Command
    async with sessions() as session:
        assert (await session.get(UserPlacedBet, bet)).status == "WON"
    # within the poll interval, never twice
    await _bet(sessions, user, {**_single("Arsenal", "Chelsea", "Match Odds", "AWAY", "4.0", "100", NOW - timedelta(hours=3)), "legs": [
        {"home": "Spurs", "away": "Leeds", "market": "Match Odds", "selection": "AWAY", "odds": "4.0", "kickoff": (NOW - timedelta(hours=3)).isoformat(), "sport_key": "soccer_epl"}]})
    assert (await poll_scores(sessions, redis, cfg, None, http=http, now=NOW))["skipped"] == {"soccer_epl": "polled recently"} and len(calls) == 1
    assert parse_scores([{"completed": True, "home_team": "X", "away_team": "Y", "scores": None, "commence_time": "2026-10-09T10:00:00Z"}], "soccer_epl") == []
    await http.aclose()


# ================================================================ trends: sharp steam, the public trap, the hybrid
@pytest.mark.asyncio
async def test_the_trend_scan_flags_the_public_trap_and_never_invents_a_public_share(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    from app.domain.popular_picks import trends  # noqa: PLC0415

    # three heavy favourites, every book shading them (no value): the crowd's treble loses to the vig
    for fixture, home, away in (("fx-1", "City", "Luton"), ("fx-2", "Madrid", "Getafe"), ("fx-3", "Bayern", "Bochum")):
        await _seed_market(redis, settings, fixture, home, away, {
            "pinnacle": {"HOME": 1.20, "DRAW": 7.0, "AWAY": 14.0}, "onexbet": {"HOME": 1.17, "DRAW": 7.0, "AWAY": 14.0}, "parimatch": {"HOME": 1.16, "DRAW": 7.0, "AWAY": 14.0},
        })
    # and one underpriced home side elsewhere (the soft books are slow to move): the hybrid's value leg
    await _seed_market(redis, settings, "fx-4", "Wolves", "Leeds", {
        "pinnacle": {"HOME": 2.40, "DRAW": 3.30, "AWAY": 3.10}, "williamhill": {"HOME": 2.38, "DRAW": 3.30, "AWAY": 3.10},
        "onexbet": {"HOME": 3.40, "DRAW": 3.30, "AWAY": 3.10}, "parimatch": {"HOME": 3.30, "DRAW": 3.30, "AWAY": 3.10},
    })
    now = datetime.now(UTC)
    async with sessions() as session:
        rows = await trends.scan(session, redis, settings, now)
    by_cat = {r.category: r for r in rows}
    trap = by_cat["PUBLIC_TRAP"]
    assert trap.true_ev_pct < 0 and trap.warning.startswith("⚠️ Public Trap:") and f"{trap.true_ev_pct / 100:+.1%}" in trap.warning
    assert trap.public_share_pct is None and trap.public_share_source is None  # nothing was measured: no number
    assert {leg["match_id"] for leg in trap.legs} == {"fx-1", "fx-2", "fx-3"}
    hybrid = by_cat["AI_HYBRID"]
    assert hybrid.true_ev_pct > 0 and {leg["match_id"] for leg in hybrid.legs} >= {"fx-4"} and len(hybrid.legs) == 2
    assert "SHARP_STEAM" not in by_cat  # no steam flags were raised
    # a second scan retires the first one's rows
    async with sessions() as session:
        await trends.scan(session, redis, settings, now)
        active = (await session.execute(select(PopularParlayModel).where(PopularParlayModel.is_active.is_(True)))).scalars().all()
    assert len(active) == len(rows)


# ================================================================ the API, as the user uses it
def oracle_app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(oracle_api.router, prefix="/api/v1/oracle")
    app.state.redis, app.state.vault = redis, None

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    from app.api.deps import get_db  # noqa: PLC0415

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


@pytest.mark.asyncio
async def test_i_placed_this_bet_then_a_score_settles_it_and_the_scorecard_moves(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    kickoff = datetime.now(UTC) - timedelta(hours=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oracle_app(sessions, redis, settings, user)), base_url="http://test") as client:
        bad = await client.post("/api/v1/oracle/bets", json={"bookmaker": "PARIMATCH", "structure": "DOUBLE", "stake_inr": "100", "legs": [
            {"home": "A", "away": "B", "market": "Match Odds", "selection": "HOME", "odds": "2"}]})
        assert bad.status_code == 422  # a double has two legs
        placed = (await client.post("/api/v1/oracle/bets", json={"bookmaker": "1XBET", "structure": "SINGLE", "stake_inr": "1000", "placed_odds": "1.95", "legs": [
            {"home": "Arsenal", "away": "Chelsea", "market": "Asian Handicap -0.25", "selection": "home", "odds": "1.95", "kickoff": kickoff.isoformat(), "league": "EPL"}]})).json()
        assert placed["status"] == "PENDING" and placed["legs"][0]["match_status"] == "LIVE" and placed["legs"][0]["selection"] == "HOME"
        before = (await client.get("/api/v1/oracle/pnl")).json()
        assert before["pending"] == {"bets": 1, "staked_inr": "1000.00"} and before["periods"]["today"]["bets"] == 0
        settled = (await client.post("/api/v1/oracle/scores", json={"home": "Arsenal", "away": "Chelsea", "kickoff": kickoff.isoformat(), "home_goals": 1, "away_goals": 1})).json()
        assert settled["settled_bets"] == 1
        after = (await client.get("/api/v1/oracle/pnl", params={"tax": True})).json()
        assert after["cached"] is False and after["periods"]["today"]["pnl_inr"] == "-500.00" and after["periods"]["today"]["tax_inr"] == "0"
        bets = (await client.get("/api/v1/oracle/bets", params={"which": "settled"})).json()
        assert bets[0]["status"] == "HALF_LOST" and bets[0]["legs"][0]["score"] == "1-1" and bets[0]["legs"][0]["result"] == "HALF_LOST"
        assert (await client.delete(f"/api/v1/oracle/bets/{bets[0]['id']}")).status_code == 409  # settled bets stay
        check = (await client.post("/api/v1/oracle/odds-check", json={"stake_inr": "1000", "legs": [
            {"home": "Arsenal", "away": "Chelsea", "market": "Match Odds", "selection": "HOME", "prices": {"Parimatch": 1.10, "1xBet": 1.25}},
            {"home": "Madrid", "away": "Sevilla", "market": "Totals 2.5", "selection": "OVER", "prices": {"Parimatch": 1.75, "onexbet": 1.66}},
            {"home": "Inter", "away": "Napoli", "market": "BTTS", "selection": "YES", "prices": {"parimatch": 2.0, "1xbet": 2.0}},
        ]})).json()
        assert check["difference_inr"] == "300.00" and check["recommended"] == "1xbet"
        twin = (await client.get("/api/v1/oracle/twin")).json()
        assert twin["settled_bets"] == 1


@pytest.mark.asyncio
async def test_cashout_advice_for_a_running_double_through_the_api(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    played = datetime.now(UTC) - timedelta(hours=4)
    later = datetime.now(UTC) + timedelta(hours=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oracle_app(sessions, redis, settings, user)), base_url="http://test") as client:
        bet = (await client.post("/api/v1/oracle/bets", json={"bookmaker": "PARIMATCH", "structure": "DOUBLE", "stake_inr": "1000", "placed_odds": "3.60", "legs": [
            {"home": "Arsenal", "away": "Chelsea", "market": "Match Odds", "selection": "HOME", "odds": "1.80", "kickoff": played.isoformat()},
            {"home": "Real Madrid", "away": "Sevilla", "market": "Match Odds", "selection": "HOME", "odds": "2.00", "kickoff": later.isoformat()}]})).json()
        await client.post("/api/v1/oracle/scores", json={"home": "Arsenal", "away": "Chelsea", "kickoff": played.isoformat(), "home_goals": 2, "away_goals": 0})
        # no feed prices the open leg: the user must say how likely it is
        missing = await client.post(f"/api/v1/oracle/bets/{bet['id']}/cashout-advice", json={"offer_inr": "1500"})
        assert missing.status_code == 422 and missing.json()["detail"]["reason"] == "NO_LIVE_PRICE"
        advice = (await client.post(f"/api/v1/oracle/bets/{bet['id']}/cashout-advice", json={"offer_inr": "1500", "probabilities": {"1": 0.5}})).json()
        assert advice["advice"] == "HOLD" and advice["fair_value_inr"] == "1800.00" and advice["offer_ratio"] == pytest.approx(1500 / 1800, abs=1e-4)
        took = (await client.post(f"/api/v1/oracle/bets/{bet['id']}/cashout", json={"cashout_inr": "1750"})).json()
        assert (took["status"], took["pnl_inr"]) == ("CASHED_OUT", "750.00")
