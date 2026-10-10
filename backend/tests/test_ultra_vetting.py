"""Group 72: the True Digital Betting Twin. The 14-pillar fortress, the booking-code ledger, the in-play watch.

The brief's proofs first, on the real code paths:

* Shin's de-vig sums to one, splits an even market evenly, and gives the favourite more than the
  proportional method does (the favourite-longshot bias it exists to remove);
* reverse line movement (the public on a selection whose price lengthens) and steam against a leg veto it;
* quarter Kelly respects the 5% ceiling, halves past a 10% rolling drawdown and halts past 20%;
* an Asian handicap +0.25 on a draw half-wins (₹1,000 at 2.00 returns ₹1,500) and -0.25 half-loses (₹500).

Then every pillar's PASS, FAIL and UNVERIFIED path (missing evidence is never a pass), the advisory switch,
and the twin end to end through Redis and the API: a slip vetted 14/14 at a retail book, the Sentinel
paging the phone, the pre-placement re-check, the ledger entry with the bookmaker's booking code, and the
in-play watch calling the pullout when the win probability collapses.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_admin, get_current_user
from app.api.v1 import digital_twin as twin_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.oracle import fortress
from app.domain.oracle.fortress import FortressInputs, FortressPolicy, LegEvidence, Status, backed_side, rolling_drawdown, shin_devig, size_stake
from app.domain.oracle.markets import LegResult, MarketKind, MarketRef, payout_factor, settle_selection
from app.domain.oracle.match_model import MarketTargets, build_models
from app.domain.oracle.parlay_engine import GateThresholds, LegCandidate, ParlayEngine, Quote, SlipCandidate
from app.models import User
from app.models.cfo_vault import BankrollAccount, MarketResult
from app.models.control_panel import SystemSettingsModel
from app.models.digital_twin import TwinInPlayMonitor, TwinVettingAudit
from app.models.cfo_growth import CFOAdvisoryLog
from app.models.hive_bots import TradingBot
from app.models.model_calibration import ModelRecalibrationRun, ModelWeightAudit
from app.models.never_forget import AshokaMistakeMemory, NeverForgetPreventionAudit, NeverForgetRule, UserXPProfile, XPAuditLog
from app.models.omni_vault import OmniFleetSource
from app.models.popular_picks import ParlayReviewGateModel, PopularParlayModel
from app.models.sentinel import Severity
from app.models.user_bets_ledger import FixtureScore, UserPlacedBet, UserPlacedLeg
from app.schemas.twin import FixtureIntel
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, decode
from app.services.sentinel_routing import HYPE, row_of
from app.services.twin import inplay, vetting
from app.services.twin.intel import read_intel, weights_key, write_intel

D = Decimal
TABLES = [
    User.__table__, TradingBot.__table__, BankrollAccount.__table__, OmniFleetSource.__table__, MarketResult.__table__, SystemSettingsModel.__table__,
    UserPlacedBet.__table__, UserPlacedLeg.__table__, FixtureScore.__table__, PopularParlayModel.__table__, ParlayReviewGateModel.__table__,
    TwinVettingAudit.__table__, TwinInPlayMonitor.__table__,
    AshokaMistakeMemory.__table__, NeverForgetRule.__table__, NeverForgetPreventionAudit.__table__, UserXPProfile.__table__, XPAuditLog.__table__,
    ModelRecalibrationRun.__table__, ModelWeightAudit.__table__, CFOAdvisoryLog.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-twin"
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
PINNACLE = {"HOME": 2.02, "DRAW": 3.60, "AWAY": 4.20}
# a wider sharp margin on the outsiders: backing HOME at 2.45 and dutching these is no arbitrage, so nothing locks a hedge at kick-off
PINNACLE_NO_ARB = {"HOME": 2.10, "DRAW": 3.20, "AWAY": 3.40}


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
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya", "TWIN_PREFIX": "test_twin", "ASHOKA_MC_PATHS": 4_000, "ORACLE_TIMEZONE": "Asia/Kolkata"})


@pytest_asyncio.fixture
async def user(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions() as session:
        row = User(username=f"twin_{uuid.uuid4().hex[:6]}", hashed_password="x", role="ADMIN")
        session.add(row)
        await session.commit()
        return row


def policy(settings: Settings, **override: Any) -> FortressPolicy:
    return dataclasses.replace(FortressPolicy.from_settings(settings), **override)


# ================================================================ evidence builders
def intel_ok(now: datetime, **replace: Any) -> FixtureIntel:
    """Every section present, fresh and clear."""
    seen = {"source": "test-feed", "observed_at": (now - timedelta(minutes=5)).isoformat()}
    body: dict[str, Any] = {
        "weather": {**seen, "wind_kmh": 12, "precipitation_mmh": 0.0, "altitude_m": 40},
        "travel": {**seen, "home": {"rest_hours": 144, "flight_delay_hours": 0, "timezones_crossed": 0}, "away": {"rest_hours": 120, "flight_delay_hours": 0.5, "timezones_crossed": 0}},
        "injuries": {**seen, "absences": [{"side": "AWAY", "player": "Squad rotation", "impact": 0.2, "status": "OUT"}]},
        "lineups": {**seen, "home_confirmed": True, "away_confirmed": True, "published_at": (now - timedelta(minutes=50)).isoformat()},
        "referee": {**seen, "name": "A. Referee", "cards_per_game": 3.9, "penalties_per_90": 0.21, "matches": 120},
        "motivation": {**seen, "home": 0.8, "away": 0.6, "derby": False},
        "liquidity": {**seen, "max_stake_inr": {"1xbet": "50000", "parimatch": "40000"}},
    }
    body.update(replace)
    return FixtureIntel.model_validate({k: v for k, v in body.items() if v is not None})


def leg(fixture: str = "fx-ars-che", home: str = "Arsenal", away: str = "Chelsea", *, p: float = 0.46, retail: float = 2.45, seen: datetime = NOW,
        selection: str = "HOME", sport: str = "soccer_epl", market_p: float | None = None) -> LegCandidate:
    """A leg whose scoreline models are fitted to ``p``; the market's own view is ``market_p`` (default ``p``)."""
    models = build_models(MarketTargets(p, 0.27, 1 - p - 0.27))
    quotes = {"1xbet": Quote("1xbet", retail, seen), "parimatch": Quote("parimatch", round(retail - 0.05, 2), seen)}
    return LegCandidate(fixture, home, away, MarketRef(MarketKind.MATCH_ODDS), selection, quotes, consensus=p if market_p is None else market_p, consensus_books=4, models=models,
                        sport_key=sport, league="EPL", kickoff=NOW + timedelta(hours=1))


def slip_of(legs: list[LegCandidate]) -> SlipCandidate:
    engine = ParlayEngine(GateThresholds(0.075, 0.55, 180, 2), paths=4_000, kelly_fraction=1.0, max_stake_pct=1.0, value_max_stake_pct=1.0, priority=("parimatch", "1xbet"), seed=7)
    slip = engine.evaluate(legs, None, NOW)
    assert slip is not None
    return slip


def inputs(legs: list[LegCandidate] | None = None, *, intel: FixtureIntel | None | Callable[[LegCandidate], FixtureIntel | None] = None, sharp: dict[str, float] | None = None,
           steam: frozenset[str] | None = frozenset(), traps: tuple[str, ...] = (), public_share: float | None = None, bankroll: Decimal | None = D("100000"),
           drawdown: float = 0.0, kill: bool | None = False, max_stake: Decimal | None = None, weights: dict[str, float] | None = None) -> FortressInputs:
    legs = legs or [leg()]
    slip = slip_of(legs)
    sharp = sharp or PINNACLE
    evidence = []
    for candidate, quote in zip(slip.legs, slip.quotes, strict=True):
        found = intel(candidate) if callable(intel) else (intel if intel is not None else intel_ok(NOW))
        evidence.append(LegEvidence(
            leg=candidate, quote=quote, sharp_market={s: {"pinnacle": Quote("pinnacle", price, NOW)} for s, price in sharp.items()}, intel=found,
            steam_selections=steam, public_share=public_share, public_traps=traps,
        ))
    return FortressInputs(slip=slip, legs=evidence, bankroll=bankroll, drawdown=drawdown, kill_switch=kill, model_weights=weights or {}, book_max_stake=max_stake, now=NOW)


def status_of(verdict: fortress.FortressVerdict, number: int) -> Status:
    return verdict.pillars[number - 1].status


# ================================================================ the brief's proofs
def test_shin_devig_sums_to_one_and_corrects_the_favourite_longshot_bias() -> None:
    even, z_even = shin_devig([1.95, 1.95])
    assert sum(even) == pytest.approx(1.0, abs=1e-12) and even[0] == pytest.approx(even[1], abs=1e-12) and 0 < z_even < 0.1
    odds = [1.25, 6.0, 13.0]
    fair, z = shin_devig(odds)
    proportional = [(1 / o) / sum(1 / x for x in odds) for o in odds]
    assert sum(fair) == pytest.approx(1.0, abs=1e-12) and 0 < z < 0.4
    assert fair[0] > proportional[0] and fair[2] < proportional[2]  # the favourite gets more, the longshot less
    # z solves sum p_i = 1, i.e. sum sqrt(z^2 + 4(1-z) pi^2 / B) = 2 + (n - 2) z (2 - z, as sometimes quoted, holds for no n)
    implied = [1 / o for o in odds]
    booksum = sum(implied)
    assert sum((z * z + 4 * (1 - z) * p * p / booksum) ** 0.5 for p in implied) == pytest.approx(2 + (len(odds) - 2) * z, abs=1e-9)
    # no overround: proportional, z = 0
    assert shin_devig([2.0, 2.0]) == ([0.5, 0.5], 0.0)
    with pytest.raises(ValueError):
        shin_devig([1.0, 3.0])


def test_reverse_line_movement_and_steam_veto_a_leg(settings: Settings) -> None:
    rule = policy(settings)
    splits = {"source": "ticket-feed", "observed_at": (NOW - timedelta(minutes=10)).isoformat(),
              "splits": {"Match Odds": {"HOME": {"tickets_pct": 0.80, "money_pct": 0.35}}}, "opening_odds": {"Match Odds": {"HOME": 2.30}}}
    verdict = fortress.run(inputs(intel=intel_ok(NOW, public_splits=splits)), rule, ("pinnacle",))
    p6 = verdict.pillars[5]
    assert p6.status is Status.FAIL and "reverse line movement" in p6.reason and "2.3 -> 2.45" in p6.reason
    # the same public share with the price shortening is not RLM
    shortened = {**splits, "opening_odds": {"Match Odds": {"HOME": 2.60}}}
    assert status_of(fortress.run(inputs(intel=intel_ok(NOW, public_splits=shortened)), rule, ("pinnacle",)), 6) is Status.PASS
    # BetDoc's own measured share counts as the public's when no feed gives one
    opening_only = {**splits, "splits": {}}
    assert status_of(fortress.run(inputs(intel=intel_ok(NOW, public_splits=opening_only), public_share=0.9), rule, ("pinnacle",)), 6) is Status.FAIL
    # sharp steam on the other side moves the line against the leg
    steam = fortress.run(inputs(steam=frozenset({"AWAY"})), rule, ("pinnacle",)).pillars[5]
    assert steam.status is Status.FAIL and "sharp steam on AWAY" in steam.reason
    assert status_of(fortress.run(inputs(steam=frozenset({"HOME"})), rule, ("pinnacle",)), 6) is Status.PASS  # steam with us is fine
    assert status_of(fortress.run(inputs(steam=None), rule, ("pinnacle",)), 6) is Status.UNVERIFIED  # edges unreadable


def test_quarter_kelly_caps_dampens_and_halts(settings: Settings) -> None:
    rule = policy(settings)
    bank = D("100000")
    plain = size_stake(0.08, bank, 0.0, rule)
    assert plain.fraction == pytest.approx(0.02) and plain.stake == D("2000") and not plain.scaled
    capped = size_stake(0.60, bank, 0.0, rule)
    assert capped.fraction == pytest.approx(0.025) and capped.stake == D("2500") and capped.capped  # Group 76: never above 2.5% of bankroll
    halved = size_stake(0.08, bank, 0.12, rule)
    assert halved.scaled and halved.fraction == pytest.approx(0.01) and halved.stake == D("1000") and halved.regime == "CAUTIOUS_THROTTLED"  # 0.08 x 0.25 x 0.5
    quartered = size_stake(0.08, bank, 0.17, rule)
    assert quartered.fraction == pytest.approx(0.005) and quartered.regime == "DEFENSIVE_CAPITAL_PRESERVATION"  # 0.08 x 0.25 x 0.25
    capped_then_halved = size_stake(0.60, bank, 0.12, rule)
    assert capped_then_halved.fraction == pytest.approx(0.0125)  # the damper bites under the ceiling, whatever the edge
    halted = size_stake(0.08, bank, 0.21, rule)
    assert halted.halted and halted.stake == 0
    assert size_stake(0.08, bank, 0.0, rule, latched=True).regime == "LATCHED_HALT"  # a halt holds until signed off
    odd = size_stake(0.0791, D("100000"), 0.0, rule)
    assert odd.stake == D("1950")  # ₹1,977.50 rounds down to the ₹50 step
    assert size_stake(0.0, bank, 0.0, rule).stake == 0
    # the rolling drawdown: the deepest fall from a peak over the window
    assert rolling_drawdown(D("90000"), [D("5000"), D("-15000")]) == pytest.approx(15000 / 105000)
    assert rolling_drawdown(D("100000"), []) == 0.0


def test_asian_handicap_quarter_lines_half_win_and_half_lose() -> None:
    stake, odds = D("1000.00"), 2.0
    plus = settle_selection(MarketRef(MarketKind.ASIAN_HANDICAP, 0.25), "HOME", 0, 0)
    assert plus is LegResult.HALF_WON
    assert stake * D(str(payout_factor(plus, odds))) == D("1500.00")  # half at 2.00, half back: +₹500
    minus = settle_selection(MarketRef(MarketKind.ASIAN_HANDICAP, -0.25), "HOME", 0, 0)
    assert minus is LegResult.HALF_LOST
    assert stake * D(str(payout_factor(minus, odds))) == D("500.00")  # half lost, half back: -₹500


# ================================================================ the fortress, pillar by pillar
def test_a_clean_slip_clears_every_pillar(settings: Settings) -> None:
    verdict = fortress.run(inputs(max_stake=D("50000")), policy(settings), ("pinnacle", "betfair"))
    assert [p.status for p in verdict.pillars] == [Status.PASS] * 15, verdict.reasons  # Group 75: pillar 15, an empty vault
    assert verdict.is_vetted and verdict.passed == 15 and verdict.conviction == 100.0 and not verdict.reasons
    assert verdict.sizing is not None and verdict.sizing.stake > 0 and verdict.sizing.fraction <= 0.05
    assert verdict.consensus_ev is not None and verdict.consensus_ev >= 0.045 and verdict.sharp_edge is not None and verdict.sharp_edge >= 0.05
    assert [p.as_dict()["key"] for p in verdict.pillars][:2] == ["model_consensus", "weather"]


def _break(settings: Settings, **kwargs: Any) -> fortress.FortressVerdict:
    return fortress.run(inputs(**kwargs), policy(settings), ("pinnacle",))


@pytest.mark.parametrize(("number", "status", "change", "words"), [
    (2, Status.FAIL, {"intel": intel_ok(NOW, weather={"source": "radar", "observed_at": NOW.isoformat(), "wind_kmh": 41, "precipitation_mmh": 0.2})}, "wind 41"),
    (2, Status.FAIL, {"intel": intel_ok(NOW, weather={"source": "radar", "observed_at": NOW.isoformat(), "wind_kmh": 5, "precipitation_mmh": 4.0})}, "rain 4"),
    (2, Status.PASS, {"intel": intel_ok(NOW, weather={"source": "venue", "observed_at": NOW.isoformat(), "indoor": True})}, "inside the limits"),
    (2, Status.UNVERIFIED, {"intel": intel_ok(NOW, weather={"source": "radar", "observed_at": (NOW - timedelta(hours=9)).isoformat(), "wind_kmh": 5, "precipitation_mmh": 0})}, "weather"),
    (3, Status.FAIL, {"intel": intel_ok(NOW, travel={"source": "flights", "observed_at": NOW.isoformat(), "home": {"rest_hours": 100, "flight_delay_hours": 4.5}, "away": {"rest_hours": 100}})}, "charter delayed 4.5h"),
    (3, Status.FAIL, {"intel": intel_ok(NOW, travel={"source": "flights", "observed_at": NOW.isoformat(), "home": {"rest_hours": 60}, "away": {"rest_hours": 100}})}, "60h since"),
    (3, Status.PASS, {"intel": intel_ok(NOW, travel={"source": "flights", "observed_at": NOW.isoformat(), "home": {"rest_hours": 100}, "away": {"rest_hours": 50, "flight_delay_hours": 6}})}, "no delayed"),
    (4, Status.FAIL, {"intel": intel_ok(NOW, injuries={"source": "club", "observed_at": NOW.isoformat(), "absences": [{"side": "HOME", "player": "No. 9", "impact": 0.9, "status": "DOUBTFUL"}]})}, "No. 9 DOUBTFUL"),
    (4, Status.FAIL, {"intel": intel_ok(NOW, injuries={"source": "club", "observed_at": NOW.isoformat(), "manager_changed_at": {"AWAY": (NOW - timedelta(days=3)).isoformat()}})}, "managerial change"),
    (5, Status.FAIL, {"intel": intel_ok(NOW, lineups={"source": "league", "observed_at": NOW.isoformat(), "home_confirmed": True, "away_confirmed": False})}, "Chelsea"),
    (5, Status.UNVERIFIED, {"intel": intel_ok(NOW, lineups=None)}, "lineups"),
    (7, Status.FAIL, {"sharp": {"HOME": 2.40, "DRAW": 3.40, "AWAY": 3.30}}, "needs +5.0%"),
    (8, Status.FAIL, {"traps": ("Saturday favourites treble",)}, "public-trap"),
    (9, Status.FAIL, {"intel": intel_ok(NOW, referee={"source": "league", "observed_at": NOW.isoformat(), "name": "Spot Kick", "cards_per_game": 5.1, "penalties_per_90": 0.6})}, "0.6 penalties"),
    (9, Status.UNVERIFIED, {"intel": intel_ok(NOW, referee=None)}, "referee"),
    (10, Status.FAIL, {"intel": intel_ok(NOW, motivation={"source": "table", "observed_at": NOW.isoformat(), "home": 0.1, "away": 0.9})}, "opponent's stake"),
    (11, Status.FAIL, {"intel": intel_ok(NOW, liquidity={"source": "book", "observed_at": NOW.isoformat(), "max_stake_inr": {"1xbet": "500"}})}, "over 1xbet's"),
    (11, Status.UNVERIFIED, {"intel": intel_ok(NOW, liquidity=None)}, "no known maximum"),
    (13, Status.FAIL, {"drawdown": 0.25}, "betting halts"),
    (13, Status.UNVERIFIED, {"bankroll": None}, "no bankroll"),
    (14, Status.FAIL, {"kill": True}, "kill switch is engaged"),
    (14, Status.UNVERIFIED, {"kill": None}, "cannot be read"),
])
def test_each_pillar_vetoes_on_its_own_evidence(settings: Settings, number: int, status: Status, change: dict[str, Any], words: str) -> None:
    verdict = _break(settings, **change)
    pillar = verdict.pillars[number - 1]
    assert pillar.status is status and words in pillar.reason, pillar.reason
    if status is not Status.PASS:
        assert not verdict.is_vetted and any(r.startswith(f"P{number} ") for r in verdict.reasons)


def test_the_model_veto_the_derby_bar_and_correlated_legs(settings: Settings) -> None:
    rule = policy(settings)
    # the scoreline models like it (+12.7%) and so does the consensus, but the market's own view prices it at -2%: vetoed
    dissent = [leg(p=0.46, market_p=0.40, retail=2.45)]
    vetoed = fortress.run(inputs(dissent), rule, ("pinnacle",)).pillars[0]
    assert vetoed.status is Status.FAIL and vetoed.reason.endswith("EV <= 0 under market (veto)") and vetoed.metrics["legs"][0]["consensus_ev"] > 0.045
    # weights tilt the consensus but never lift a voting model's veto ...
    assert fortress.run(inputs(dissent, weights={"market": 0.05}), rule, ("pinnacle",)).pillars[0].reason.endswith("EV <= 0 under market (veto)")
    # ... while a model the recalibration engine benched (weight 0, Group 74) neither votes nor vetoes
    benched = fortress.run(inputs(dissent, weights={"market": 0.0}), rule, ("pinnacle",)).pillars[0]
    assert benched.status is Status.FAIL and "(veto)" not in benched.reason and "2 voting model(s) price it, 3 needed (market benched)" in benched.reason
    assert fortress.run(inputs(dissent, weights={"market": 0.0}), policy(settings, min_models=2), ("pinnacle",)).pillars[0].status is Status.PASS
    # every model below the price: the veto and the consensus both fail
    thin = _break(settings, legs=[leg(p=0.40, retail=2.45)]).pillars[0]
    assert "dixon_coles, market (veto)" in thin.reason and "weighted model EV" in thin.reason
    # too few models
    assert "4 needed" in fortress.run(inputs(), policy(settings, min_models=4), ("pinnacle",)).pillars[0].reason
    # a derby needs 1.5x the EV bar: the leg's +13.9% clears a 10% bar but not the derby's 15%
    derby = {"source": "table", "observed_at": NOW.isoformat(), "home": 0.9, "away": 0.9, "derby": True}
    strict = fortress.run(inputs(intel=intel_ok(NOW, motivation=derby)), policy(settings, min_ev=0.10), ("pinnacle",))
    assert status_of(strict, 10) is Status.PASS and "derby" in strict.pillars[9].reason
    assert status_of(strict, 1) is Status.FAIL and "under +15.00%" in strict.pillars[0].reason
    # two legs on one fixture: the anti-correlation gate
    same = _break(settings, legs=[leg(), leg(selection="AWAY", p=0.27, retail=4.6)])
    assert status_of(same, 12) is Status.FAIL and "SAME_FIXTURE" in same.pillars[11].reason


def test_missing_evidence_is_never_a_pass_and_advisory_pillars_report_without_vetoing(settings: Settings) -> None:
    bare = fortress.run(inputs(intel=lambda _leg: None), policy(settings), ("pinnacle",))
    unverified = {p.number for p in bare.pillars if p.status is Status.UNVERIFIED}
    assert unverified == {2, 3, 4, 5, 9, 10, 11} and not bare.is_vetted and bare.passed == 8 and bare.conviction == round(8 / 15 * 100, 2)
    relaxed = fortress.run(inputs(intel=intel_ok(NOW, referee=None)), policy(settings, advisory=frozenset({9})), ("pinnacle",))
    assert status_of(relaxed, 9) is Status.ADVISORY and relaxed.is_vetted and relaxed.passed == 14
    assert FortressPolicy.from_settings(settings.model_copy(update={"TWIN_ADVISORY_PILLARS": "9, 11,x,99"})).advisory == frozenset({9, 11})


def test_the_circadian_cut_keeps_a_strong_leg_and_drops_a_marginal_one(settings: Settings) -> None:
    jet = {"source": "flights", "observed_at": NOW.isoformat(), "home": {"rest_hours": 100, "timezones_crossed": 5}, "away": {"rest_hours": 100}}
    strong = _break(settings, intel=intel_ok(NOW, travel=jet))
    assert status_of(strong, 3) is Status.PASS and "circadian cut 4.5%" in strong.pillars[2].reason
    marginal = _break(settings, legs=[leg(p=0.43, retail=2.45)], intel=intel_ok(NOW, travel=jet))
    assert status_of(marginal, 3) is Status.FAIL and "after the 4.5% cut" in marginal.pillars[2].reason
    assert backed_side(MarketRef(MarketKind.DOUBLE_CHANCE), "X2") == "AWAY" and backed_side(MarketRef(MarketKind.TOTALS, 2.5), "OVER") is None


def test_stale_prices_and_drift_fail_the_execution_gate(settings: Settings) -> None:
    rule = policy(settings)
    old = _break(settings, legs=[leg(seen=NOW - timedelta(seconds=120))])
    assert status_of(old, 14) is Status.FAIL and "older than 60s" in old.pillars[13].reason
    quotes = [Quote("1xbet", 2.40, NOW)]
    assert fortress.pillar_14(False, quotes, NOW, rule, floors=fortress.drift_floors([2.45], rule)).status is Status.FAIL  # 2.40 < 2.4255
    assert fortress.pillar_14(False, [Quote("1xbet", 2.43, NOW)], NOW, rule, floors=fortress.drift_floors([2.45], rule)).status is Status.PASS


def test_twin_alerts_go_to_the_phone_and_the_halt_to_the_incident_channels() -> None:
    def alert(kind: AlertKind, severity: Severity) -> SentinelAlert:
        return SentinelAlert(kind=kind, severity=severity, title="t", source="digital_twin")
    assert row_of(alert(AlertKind.TWIN_SLIP_VETTED, Severity.INFO)) == HYPE
    assert row_of(alert(AlertKind.TWIN_PULLOUT, Severity.INFO)) == HYPE
    assert row_of(alert(AlertKind.TWIN_DRAWDOWN_HALT, Severity.CRITICAL)) == "CRITICAL"


def test_the_inplay_tick_is_on_the_beat_schedule() -> None:
    from app.core.celery_app import celery_app  # noqa: PLC0415

    entry = celery_app.conf.beat_schedule["twin-inplay-tick"]
    assert entry["task"] == "twin.inplay_tick" and entry["schedule"] == get_settings().TWIN_INPLAY_POLL_SECONDS
    assert "app.workers.twin_tasks" in celery_app.conf.include


# ================================================================ end to end: Redis, the API, the ledger, the watch
async def seed(redis: Redis, settings: Settings, fixture: str, home: str, away: str, *, sharp: dict[str, float], soft: dict[str, dict[str, float]],
               kickoff: datetime | None = None, age: float = 3.0) -> None:
    """The board (fixture discovery), the market index, one hash entry per book: what Garuda and Aryabhata write."""
    from app.core.live_odds import publish_board_ticks  # noqa: PLC0415
    from app.schemas.market import MarketTick  # noqa: PLC0415
    from app.services.aryabhata_pipeline import AryabhataKeys  # noqa: PLC0415

    kickoff = kickoff or datetime.now(UTC) + timedelta(hours=2)
    await publish_board_ticks(redis, [MarketTick(match_id=fixture, home_team=home, away_team=away, market_type="Match Odds", selection="HOME", odds=D("2"),
                                                 true_probability=D("0.5"), is_suspended=False, sport_key="soccer_epl", commence_time=kickoff)])
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    await redis.sadd(keys.markets(fixture), "Match Odds")
    for book, prices in {"pinnacle": sharp, **soft}.items():
        await redis.hset(keys.books(f"{fixture}|Match Odds"), f"odds_api|{book}", json.dumps({"s": "odds_api", "b": book, "p": prices, "t": time.time() - age, "x": False}))


SOFT = {"williamhill": {"HOME": 2.00, "DRAW": 3.50, "AWAY": 4.10}, "onexbet": {"HOME": 2.45, "DRAW": 3.10, "AWAY": 3.40}, "parimatch": {"HOME": 2.40, "DRAW": 3.15, "AWAY": 3.35}}


def twin_app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(twin_api.router, prefix="/api/v1")
    app.state.redis = redis
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


async def stream(redis: Redis, settings: Settings) -> list[SentinelAlert]:
    return [decode(fields["a"]) for _, fields in await redis.xrange(SentinelKeys(settings).stream)]


@pytest.mark.asyncio
async def test_vet_confirm_place_with_a_booking_code_and_watch_the_pullout(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    fixture = "fx-ars-che"
    await seed(redis, settings, fixture, "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT)
    leg_id = f"{fixture}|Match Odds|HOME"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=twin_app(sessions, redis, settings, user)), base_url="http://test") as client:
        # no evidence yet: every context pillar is unverified and nothing is vetted
        bare = (await client.post("/api/v1/twin/vet", json={"leg_ids": [leg_id], "bankroll_inr": "100000"})).json()
        assert bare["is_vetted"] is False and bare["bookmaker"] == "1xbet" and bare["developer_credit"] == "Amit Ashok Kumar Patnaik"
        assert {p["number"] for p in bare["pillars"] if p["status"] == "UNVERIFIED"} == {2, 3, 4, 5, 9, 10, 11}
        assert {p["number"] for p in bare["pillars"] if p["status"] == "PASS"} == {1, 6, 7, 8, 12, 13, 14, 15}, bare["rejection_reasons"]

        # the evidence arrives (an administrator, or a feed through the same endpoint)
        written = (await client.put(f"/api/v1/twin/intel/{fixture}", json=intel_ok(datetime.now(UTC)).model_dump(mode="json", exclude_none=True))).json()
        assert written["written"] == ["injuries", "lineups", "liquidity", "motivation", "referee", "travel", "weather"]
        shown = (await client.get(f"/api/v1/twin/intel/{fixture}")).json()
        assert shown["sections"]["referee"]["name"] == "A. Referee"
        vetted = (await client.post("/api/v1/twin/vet", json={"leg_ids": [leg_id], "bankroll_inr": "100000"})).json()
        assert vetted["is_vetted"] is True and vetted["pillars_passed"] == 15 and vetted["conviction_score"] == 100.0, vetted["rejection_reasons"]
        stake = D(vetted["stake_inr"])
        assert D("0") < stake <= D("5000") and stake % D("50") == 0 and vetted["sharp_edge"] >= 0.05
        slip = vetted["slip"]
        assert slip["stake_inr"] == vetted["stake_inr"] and "Twin fortress 15/15 · vetted" in slip["quick_copy"] and slip["legs"][0]["prices"]["1xbet"] == 2.45
        assert {b["bookmaker"] for b in slip["books"] if b["available"]} == {"1xbet", "parimatch"}  # the twin prices at retail books only

        # the phone hears about it, once per slip
        alerts = [a for a in await stream(redis, settings) if a.kind is AlertKind.TWIN_SLIP_VETTED]
        assert len(alerts) == 1 and alerts[0].detail["audit_id"] == vetted["id"] and "15/15" in alerts[0].title

        listed = (await client.get("/api/v1/twin/audits", params={"vetted": True})).json()
        assert [a["id"] for a in listed["audits"]] == [vetted["id"]]

        # just before placing: pillar 14 again on freshly read prices
        check = (await client.post(f"/api/v1/twin/audits/{vetted['id']}/confirm")).json()
        assert check["status"] == "PASS" and check["legs"][0]["current_odds"] == 2.45
        await seed(redis, settings, fixture, "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft={**SOFT, "onexbet": {"HOME": 2.38, "DRAW": 3.2, "AWAY": 3.6}})
        drifted = (await client.post(f"/api/v1/twin/audits/{vetted['id']}/confirm")).json()
        assert drifted["status"] == "FAIL" and "drifted below the floor" in drifted["reason"]
        await seed(redis, settings, fixture, "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT)

        # Pathway A: placed at 1xBet under the book's own booking code, watched in play
        placed = await client.post(f"/api/v1/twin/audits/{vetted['id']}/ledger", json={"bookmaker": "1XBET", "stake_inr": vetted["stake_inr"], "placed_odds": "2.45",
                                                                                         "booking_code": "1x-49102"})
        assert placed.status_code == 201, placed.text
        bet = placed.json()
        assert bet["booking_code"] == "1X-49102" and bet["status"] == "PENDING" and bet["watch"]["started"] is True
        monitor = bet["watch"]["monitor"]
        assert monitor["initial_win_prob"] == pytest.approx(vetted["slip"]["legs"][0]["fair_probability"], abs=0.02)
        async with sessions() as session:
            row = await session.get(UserPlacedBet, uuid.UUID(bet["bet_id"]))
            assert row is not None and str(row.vetting_audit_id) == vetted["id"] and row.slip_id == vetted["slip_id"] and row.placed_odds == D("2.4500")

        # a quiet tick: priced, nothing to call
        quiet = (await client.post("/api/v1/twin/monitors/tick")).json()
        assert quiet["ran"] and quiet["priced"] == 1 and quiet["alerts"] == []

        # a red card for Arsenal: the market collapses and the watch calls the pullout
        await seed(redis, settings, fixture, "Arsenal", "Chelsea", sharp={"HOME": 15.0, "DRAW": 5.5, "AWAY": 1.22},
                   soft={b: {"HOME": 14.0, "DRAW": 5.2, "AWAY": 1.20} for b in SOFT})
        fired = (await client.post("/api/v1/twin/monitors/tick")).json()
        assert [a["reason"] for a in fired["alerts"]] == ["PROBABILITY_COLLAPSE"] and fired["alerts"][0]["booking_code"] == "1X-49102"
        pullouts = [a for a in await stream(redis, settings) if a.kind is AlertKind.TWIN_PULLOUT]
        assert len(pullouts) == 1 and "1X-49102" in pullouts[0].title and "cash out or hedge now" in pullouts[0].body
        watches = (await client.get("/api/v1/twin/monitors")).json()
        assert watches[0]["pullout_reason"] == "PROBABILITY_COLLAPSE" and watches[0]["is_active"] is False and watches[0]["bet"]["booking_code"] == "1X-49102"
        again = (await client.post("/api/v1/twin/monitors/tick")).json()
        assert again["watched"] == 0  # it fires once

        # the twin's own scorecard
        ledger = (await client.get("/api/v1/twin/ledger")).json()
        assert ledger["pending"] == {"bets": 1, "staked_inr": vetted["stake_inr"]} and ledger["bets"][0]["booking_code"] == "1X-49102"


@pytest.mark.asyncio
async def test_the_watch_takes_a_good_offer_then_locks_a_hedge(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    fixture = "fx-liv-eve"
    await seed(redis, settings, fixture, "Liverpool", "Everton", sharp=PINNACLE, soft=SOFT)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=twin_app(sessions, redis, settings, user)), base_url="http://test") as client:
        audit = (await client.post("/api/v1/twin/vet", json={"leg_ids": [f"{fixture}|Match Odds|HOME"], "bankroll_inr": "100000"})).json()
        bet = (await client.post(f"/api/v1/twin/audits/{audit['id']}/ledger", json={"bookmaker": "1XBET", "stake_inr": "1000", "placed_odds": "2.45", "target_profit_pct": 0.4})).json()
        assert bet["booking_code"] is None and bet["watch"]["started"]
        monitor_id = bet["watch"]["monitor"]["id"]
        # Liverpool score: home win ~75%, the slip's fair value ~₹1,840
        await seed(redis, settings, fixture, "Liverpool", "Everton", sharp={"HOME": 1.30, "DRAW": 5.0, "AWAY": 11.0}, soft={b: {"HOME": 1.28, "DRAW": 4.8, "AWAY": 10.0} for b in SOFT})
        offer = (await client.put(f"/api/v1/twin/monitors/{monitor_id}/offer", json={"cashout_offer_inr": "2000"})).json()
        assert offer["cashout_offer_inr"] == "2000.00"
        took = (await client.post("/api/v1/twin/monitors/tick")).json()
        assert [a["reason"] for a in took["alerts"]] == ["CASHOUT_ADVISED"] and "take the ₹2,000.00 cashout" in took["alerts"][0]["action"]
        # re-armed with no offer on the table: dutching the draw and Everton at Pinnacle locks a profit whatever happens
        rearmed = (await client.post(f"/api/v1/twin/monitors/{bet['bet_id']}", json={})).json()
        assert rearmed["is_active"] and not rearmed["pullout_triggered"] and rearmed["initial_win_prob"] > 0.7
        await client.put(f"/api/v1/twin/monitors/{monitor_id}/offer", json={"cashout_offer_inr": None})
        hedged = (await client.post("/api/v1/twin/monitors/tick")).json()
        assert [a["reason"] for a in hedged["alerts"]] == ["HEDGE_LOCK"] and "Back ₹" in hedged["alerts"][0]["action"] and "locks ₹" in hedged["alerts"][0]["action"]
        stopped = (await client.delete(f"/api/v1/twin/monitors/{monitor_id}")).json()
        assert stopped["is_active"] is False


def test_the_target_call_uses_the_offer_else_fair_value(settings: Settings) -> None:
    from app.domain.oracle.cashout_advisor import Advice, CashoutAdvice  # noqa: PLC0415

    bet = UserPlacedBet(stake_inr=D("1000"))
    monitor = TwinInPlayMonitor(initial_win_prob=0.5, target_profit_pct=0.4)

    def valuation(fair: str, offer: str | None = None, p: float = 0.5) -> inplay.Valuation:
        advice = CashoutAdvice(Advice.HOLD, D(fair), D("2450"), p, None if offer is None else D(offer), None, None, None, None, ("hold",))
        return inplay.Valuation(p, advice)

    hit = inplay.pullout(monitor, bet, valuation("1450"), settings)
    assert hit is not None and hit[0].value == "TARGET_PROFIT_REACHED" and "fair value ₹1,450" in hit[1] and "45% over the stake" in hit[1]
    assert inplay.pullout(monitor, bet, valuation("1390"), settings) is None
    assert inplay.pullout(monitor, bet, valuation("1600", offer="1300"), settings) is None  # the offer is what can be banked
    collapse = inplay.pullout(monitor, bet, valuation("300", p=0.12), settings)
    assert collapse is not None and collapse[0].value == "PROBABILITY_COLLAPSE"


@pytest.mark.asyncio
async def test_refusals_kill_switch_drawdown_halt_and_pathway_b_guards(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    fixture = "fx-new-ast"
    await seed(redis, settings, fixture, "Newcastle", "Aston Villa", sharp=PINNACLE, soft=SOFT)
    await write_intel(redis, settings, fixture, intel_ok(datetime.now(UTC)))
    assert set((await read_intel(redis, settings, [fixture]))[fixture].model_dump(exclude_none=True)) == {"weather", "travel", "injuries", "lineups", "referee", "motivation", "liquidity"}
    leg_id = f"{fixture}|Match Odds|HOME"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=twin_app(sessions, redis, settings, user)), base_url="http://test") as client:
        gone = await client.post("/api/v1/twin/vet", json={"leg_ids": ["fx-nowhere|Match Odds|HOME"], "bankroll_inr": "100000"})
        assert gone.status_code == 410 and gone.json()["detail"]["reason"] == "NO_LONGER_QUOTED"
        # Stake is not a retail book: a slip only it quotes has nowhere to go
        await seed(redis, settings, "fx-sta-only", "Fulham", "Brentford", sharp=PINNACLE, soft={"stake": {"HOME": 2.5, "DRAW": 3.3, "AWAY": 3.4}, "williamhill": SOFT["williamhill"]})
        nowhere = await client.post("/api/v1/twin/vet", json={"leg_ids": ["fx-sta-only|Match Odds|HOME"], "bankroll_inr": "100000"})
        assert nowhere.status_code == 409 and nowhere.json()["detail"]["reason"] == "NO_RETAIL_BOOK"

        # model weights from the calibration store reach pillar 1
        await redis.hset(weights_key(settings), mapping={"market": "2.0", "poisson": "1.0", "dixon_coles": "1.0", "junk": "x"})
        await redis.set(settings.CFO_KILL_SWITCH_KEY, "1")
        halted = (await client.post("/api/v1/twin/vet", json={"leg_ids": [leg_id], "bankroll_inr": "100000"})).json()
        assert halted["is_vetted"] is False and halted["pillars"][13]["status"] == "FAIL" and halted["pillars"][0]["metrics"]["weights"] == {"market": 2.0, "poisson": 1.0, "dixon_coles": 1.0}
        await redis.delete(settings.CFO_KILL_SWITCH_KEY)

        # a 25% rolling drawdown halts the twin, latches, and KUMBHA pages CRITICAL (Group 76)
        async with sessions() as session:
            for i, pnl in enumerate((D("10000"), D("-35000"))):
                session.add(UserPlacedBet(id=uuid.uuid4(), user_id=user.id, bookmaker="1XBET", structure="SINGLE", stake_inr=D("35000"), placed_odds=D("2"),
                                          placed_at=datetime.now(UTC) - timedelta(days=2, hours=i), status="WON" if pnl > 0 else "LOST", pnl_inr=pnl,
                                          return_inr=D("45000") if pnl > 0 else D("0"), settled_at=datetime.now(UTC) - timedelta(days=1, hours=2 - i)))
            await session.commit()
        drained = (await client.post("/api/v1/twin/vet", json={"leg_ids": [leg_id], "bankroll_inr": "75000"})).json()
        assert drained["pillars"][12]["status"] == "FAIL" and drained["pillars"][12]["metrics"]["halted"] is True and drained["stake_inr"] == "0.00"
        assert [a.severity for a in await stream(redis, settings) if a.kind is AlertKind.CFO_REGIME_CHANGE] == [Severity.CRITICAL]

        # Pathway B: only a vetted single, and only after the re-check; then the router's own stack takes over
        refused = await client.post(f"/api/v1/twin/audits/{drained['id']}/route")
        assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "NOT_VETTED"
        async with sessions() as session:
            for bet in (await session.execute(select(UserPlacedBet).where(UserPlacedBet.user_id == user.id))).scalars():
                await session.delete(bet)
            await session.commit()
        clean = (await client.post("/api/v1/twin/vet", json={"leg_ids": [leg_id], "bankroll_inr": "100000"})).json()
        assert clean["is_vetted"], clean["rejection_reasons"]
        unrouted = await client.post(f"/api/v1/twin/audits/{clean['id']}/route")
        assert unrouted.status_code == 503 and unrouted.json()["detail"]["reason"] == "BOOKMAKER_UNCONFIGURED"  # the re-check passed; no gateway here

        # another user's audit is not yours
        stranger = User(id=uuid.uuid4(), username="stranger", hashed_password="x")
        app = twin_app(sessions, redis, settings, stranger)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
            assert (await other.get(f"/api/v1/twin/audits/{clean['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_the_tick_lock_lets_one_tick_run_at_a_time(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    await redis.set(inplay.lock_key(settings), "someone-else", ex=30)
    report = await inplay.tick(sessions, redis, settings, datetime.now(UTC))
    assert report.ran is False
    assert await redis.get(inplay.lock_key(settings)) == "someone-else"  # never released by a tick that did not take it
    await redis.delete(inplay.lock_key(settings))
    assert (await inplay.tick(sessions, redis, settings, datetime.now(UTC))).ran is True
    assert await redis.get(inplay.lock_key(settings)) is None
    assert (await inplay.tick(sessions, None, settings, datetime.now(UTC))).ran is False


def test_place_request_carries_the_audited_legs_at_the_chosen_book() -> None:
    audit = TwinVettingAudit(id=uuid.uuid4(), slip_id="abc", kind="DOUBLE", bookmaker="1xbet", leg_ids=["a|Match Odds|HOME", "b|Totals 2.5|OVER"], pillars_passed=14, slip={"legs": [
        {"fixture_id": "a", "home": "A", "away": "B", "market": "Match Odds", "selection": "HOME", "prices": {"1xbet": 2.1, "parimatch": 2.05}, "fair_probability": 0.5, "kickoff": NOW.isoformat()},
        {"fixture_id": "b", "home": "C", "away": "D", "market": "Totals 2.5", "selection": "OVER", "prices": {"1xbet": 1.9}, "fair_probability": 0.55},
    ]})
    from app.schemas.twin import LedgerFromAudit  # noqa: PLC0415

    body = LedgerFromAudit(bookmaker="PARIMATCH", stake_inr=D("500"))
    request = vetting.place_request(audit, body)
    assert [str(leg.odds) for leg in request.legs] == ["2.050", "1.900"]  # Parimatch where it quoted, else the audited book's price
    assert request.structure.value == "DOUBLE" and request.slip_id == "abc" and request.legs[0].kickoff == NOW
    with pytest.raises(ValueError):
        LedgerFromAudit(bookmaker="1XBET", stake_inr=D("500"), booking_code="bad code!")
