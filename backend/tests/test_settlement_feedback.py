"""Group 73: the post-execution feedback loop. Settlement, closing-line value, model attribution, root causes,
and the inverse-Brier weights that teach the fortress's pillar 1.

The brief's proofs on the real code paths:

* Brier and log loss on the brief's numbers (0.8 -> 0.04; ln 2 at a coin flip; eps clipping keeps a
  certain-and-wrong prediction finite), the ranked probability score over the five ordered results;
* CLV against the sharp close (2.10 taken, 1.95 closing: +7.69%) and against the de-vigged close;
* the Asian handicap matrix, every quarter line, in exact rupees;
* the root-cause classifier, one tag per kind of evidence;
* the bridge: recalibration writes the very hash Group 72's pillar 1 reads, and pillar 1 reads it back.

Then the loop end to end: closing lines from Nalanda's tick lake (the last sharp price before kickoff,
never one after), one feedback row per settled leg and predictor, the root-cause audits, the Sentinel
paging the phone for fresh settlements only, idempotent re-runs, concurrent sweeps, and the API.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_admin, get_current_user
from app.api.v1 import settlement_feedback as feedback_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.oracle import feedback_math as fm
from app.domain.oracle.fortress import FortressPolicy, pillar_1, shin_devig
from app.domain.oracle.markets import LegResult, MarketKind, MarketRef, payout_factor, settle_selection
from app.models import User
from app.models.cfo_vault import BankrollAccount, MarketResult
from app.models.control_panel import SystemSettingsModel
from app.models.digital_twin import TwinInPlayMonitor, TwinVettingAudit
from app.models.feedback import ModelPredictionFeedback, RootCauseTag, SettlementRootCauseAudit
from app.models.hive_bots import TradingBot
from app.models.model_calibration import ModelRecalibrationRun, ModelWeightAudit, RecalibrationTrigger
from app.models.nalanda_lake import NalandaTick
from app.models.omni_vault import OmniFleetSource
from app.models.popular_picks import ParlayReviewGateModel, PopularParlayModel
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.twin import FixtureIntel
from app.services import user_pnl_tracker as tracker
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, decode
from app.services.sentinel_routing import HYPE, row_of
from app.services.twin import feedback_tracker as feedback
from app.services.twin import model_calibrator
from app.services.twin.intel import model_weights, weights_key, write_intel

D = Decimal
TABLES = [
    User.__table__, TradingBot.__table__, BankrollAccount.__table__, OmniFleetSource.__table__, MarketResult.__table__, SystemSettingsModel.__table__,
    UserPlacedBet.__table__, UserPlacedLeg.__table__, FixtureScore.__table__, PopularParlayModel.__table__, ParlayReviewGateModel.__table__,
    TwinVettingAudit.__table__, TwinInPlayMonitor.__table__, NalandaTick.__table__, ModelPredictionFeedback.__table__, SettlementRootCauseAudit.__table__,
    ModelRecalibrationRun.__table__, ModelWeightAudit.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-feedback"
EPS = 1e-6


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
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=TABLES))  # nalanda_ticks comes with its default partition
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
    return get_settings().model_copy(update={"TWIN_PREFIX": "test_twin_fb", "TWIN_RECALIBRATION_MIN_SAMPLES": 2, "TWIN_SHARP_BOOKS": "pinnacle,betfair"})


@pytest_asyncio.fixture
async def user(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions() as session:
        row = User(username=f"fb_{uuid.uuid4().hex[:6]}", hashed_password="x", role="ADMIN")
        session.add(row)
        await session.commit()
        return row


# ================================================================ the brief's proofs
def test_brier_and_log_loss_on_the_briefs_numbers() -> None:
    assert fm.brier_score(1.0, 1.0) == 0.0 and fm.brier_score(1.0, 0.0) == 1.0 and fm.brier_score(0.5, 1.0) == 0.25
    assert fm.brier_score(0.8, 1.0) == pytest.approx(0.04) and fm.brier_score(0.2, 0.0) == pytest.approx(0.04)
    assert fm.brier_score(0.6, 0.75) == pytest.approx(0.0225)  # a half win scores 0.75
    assert fm.log_loss(0.99, 1.0, EPS) < 0.02 and fm.log_loss(0.01, 1.0, EPS) > 4.0
    assert fm.log_loss(0.5, 1.0, EPS) == pytest.approx(math.log(2))
    assert fm.log_loss(0.0, 1.0, EPS) == pytest.approx(-math.log(EPS))  # certain and wrong: finite, -ln(eps)
    assert fm.log_loss(1.0, 1.0, EPS) == pytest.approx(-math.log(1 - EPS))
    assert fm.outcome_score(LegResult.VOID) is None and fm.outcome_score(LegResult.HALF_LOST) == 0.25


def test_the_ranked_probability_score_is_crps_on_the_five_ordered_results() -> None:
    won = {"WON": 1.0}
    assert fm.ranked_probability_score(won, LegResult.WON) == 0.0 and fm.ranked_probability_score(won, LegResult.LOST) == 1.0
    # two outcomes: RPS equals the Brier score of the win probability
    binary = {"WON": 0.6, "LOST": 0.4}
    assert fm.ranked_probability_score(binary, LegResult.WON) == pytest.approx(fm.brier_score(0.6, 1.0))
    assert fm.ranked_probability_score(binary, LegResult.LOST) == pytest.approx(fm.brier_score(0.6, 0.0))
    # a near miss costs less than a far one: half lost is closer to "won" than lost
    spread = {"WON": 0.5, "HALF_WON": 0.1, "VOID": 0.1, "HALF_LOST": 0.1, "LOST": 0.2}
    assert fm.ranked_probability_score(spread, LegResult.HALF_LOST) < fm.ranked_probability_score(spread, LegResult.LOST)
    # sum over k of (F_k - O_k)^2 / 4, by hand for HALF_WON: F = .2 .3 .4 .5, O = 0 0 0 1
    assert fm.ranked_probability_score(spread, LegResult.HALF_WON) == pytest.approx((0.04 + 0.09 + 0.16 + 0.25) / 4)
    assert fm.expected_score(spread) == pytest.approx(0.5 + 0.075 + 0.05 + 0.025) and fm.win_probability(spread) == pytest.approx(0.6)


def test_closing_line_value_raw_and_de_vigged() -> None:
    assert fm.clv_pct(2.10, 1.95) == pytest.approx(7.6923, abs=1e-4)
    assert fm.clv_pct(1.80, 2.00) == pytest.approx(-10.0)
    assert fm.clv_pct(2.0, None) is None and fm.clv_pct(2.0, 0.5) is None and fm.clv_pct(None, 2.0) is None
    fair, _ = shin_devig([1.95, 3.60, 4.00])
    assert fm.clv_sharp_pct(2.10, fair[0]) == pytest.approx((2.10 * fair[0] - 1) * 100)
    assert fm.clv_sharp_pct(2.10, 0.0) is None


def test_calibration_bins_and_expected_calibration_error() -> None:
    pairs = [(0.15, 0.0)] * 8 + [(0.15, 1.0)] * 2 + [(0.85, 1.0)] * 7 + [(0.85, 0.0)] * 3
    bins, ece = fm.calibration(pairs, 10)
    assert bins[1].count == 10 and bins[1].observed_mean == pytest.approx(0.2) and bins[8].observed_mean == pytest.approx(0.7)
    assert ece == pytest.approx(0.5 * 0.05 + 0.5 * 0.15)
    assert fm.calibration([(1.0, 1.0)], 10)[0][9].count == 1  # p = 1 falls in the top bin
    assert fm.calibration([], 5)[1] is None


@pytest.mark.parametrize(("line", "home", "away", "result", "returned"), [
    (0.25, 0, 0, LegResult.HALF_WON, "1500.00"),  # E = +0.25: half at 2.00, half back
    (-0.25, 0, 0, LegResult.HALF_LOST, "500.00"),  # E = -0.25: half lost, half back
    (0.0, 1, 1, LegResult.VOID, "1000.00"),  # E = 0: push
    (-0.25, 1, 0, LegResult.WON, "2000.00"),  # E = +0.75: full win
    (0.75, 0, 1, LegResult.HALF_LOST, "500.00"),  # E = -0.25
    (-0.75, 1, 0, LegResult.HALF_WON, "1500.00"),  # E = +0.25
    (-0.75, 2, 0, LegResult.WON, "2000.00"),  # E = +1.25
    (0.75, 0, 2, LegResult.LOST, "0.00"),  # E = -1.25
    (-1.0, 1, 0, LegResult.VOID, "1000.00"),  # E = 0 on an integer line
])
def test_the_asian_handicap_settlement_matrix(line: float, home: int, away: int, result: LegResult, returned: str) -> None:
    got = settle_selection(MarketRef(MarketKind.ASIAN_HANDICAP, line), "HOME", home, away)
    assert got is result
    assert (D("1000.00") * D(str(payout_factor(got, 2.0)))).quantize(D("0.01")) == D(returned)


def test_the_root_cause_classifier_names_the_strongest_evidence() -> None:
    policy = fm.RcaPolicy(confident_prob=0.65, steam_clv_pct=-4.0)
    lost = dict(status="LOST", win_probability=0.40, clv_pct=1.5)
    assert fm.classify(fm.LossEvidence(**{**lost, "status": "WON"}), policy)[0] is RootCauseTag.NONE
    assert fm.classify(fm.LossEvidence(**lost, inplay_collapse=(0.55, 0.08)), policy)[0] is RootCauseTag.INPLAY_SHOCK_RED_CARD
    steam = fm.classify(fm.LossEvidence(**{**lost, "clv_pct": -6.5}), policy)
    assert steam[0] is RootCauseTag.STEAM_ADVERSE_SELECTION and "-6.5%" in steam[1]
    assert fm.classify(fm.LossEvidence(**lost, weather_breach="wind 48 km/h"), policy)[0] is RootCauseTag.WEATHER_ANOMALY
    confident = fm.classify(fm.LossEvidence(**{**lost, "win_probability": 0.72}, model_brier=0.51), policy)
    assert confident[0] is RootCauseTag.MODEL_UNDERESTIMATION and "72%" in confident[1] and "0.510" in confident[1]
    assert fm.classify(fm.LossEvidence(**lost, strict_referee="X, 5.2 cards a game"), policy)[0] is RootCauseTag.REFEREE_STRICTNESS_BIAS
    variance = fm.classify(fm.LossEvidence(**{**lost, "status": "HALF_LOST"}), policy)
    assert variance[0] is RootCauseTag.VARIANCE_BAD_LUCK and "40% chance" in variance[1] and "+1.5%" in variance[1]


def test_settlement_alerts_reach_the_phone() -> None:
    alert = SentinelAlert(kind=AlertKind.TWIN_SETTLED, severity="INFO", title="t", source="feedback_loop")
    assert row_of(alert) == HYPE


def test_the_feedback_tasks_are_on_the_beat_schedule() -> None:
    from app.core.celery_app import celery_app  # noqa: PLC0415

    beat = celery_app.conf.beat_schedule
    assert beat["feedback-sweep"]["task"] == "feedback.sweep" and beat["feedback-sweep"]["schedule"] == get_settings().FEEDBACK_SWEEP_INTERVAL_SECONDS
    assert "app.workers.feedback_tasks" in celery_app.conf.include
    assert "feedback-recalibrate" not in beat and beat["model-recalibration"]["task"] == "calibration.recalibrate"  # Group 74 publishes the weights


# ================================================================ closing lines from the tick lake
def tick(fixture: str, market: str, selection: str, book: str, odds: str, observed: datetime, *, suspended: bool = False, anomaly: bool = False) -> NalandaTick:
    return NalandaTick(created_at=observed + timedelta(seconds=2), observed_at=observed, fixture_id=fixture, market=market, selection=selection, source="odds_api",
                       bookmaker_id=book, odds=D(odds), is_suspended=suspended, is_anomaly=anomaly, stream_id=uuid.uuid4().hex[:16])


def market_ticks(fixture: str, prices: dict[str, str], book: str, at: datetime, market: str = "Match Odds") -> list[NalandaTick]:
    return [tick(fixture, market, s, book, o, at) for s, o in prices.items()]


def test_the_close_is_the_last_complete_sharp_market_before_kickoff() -> None:
    kickoff = datetime(2026, 10, 10, 19, 0, tzinfo=UTC)
    ticks = [
        *market_ticks("fx", {"HOME": "2.05", "DRAW": "3.5", "AWAY": "3.9"}, "pinnacle", kickoff - timedelta(hours=3)),
        *market_ticks("fx", {"HOME": "1.95", "DRAW": "3.6", "AWAY": "4.0"}, "pinnacle", kickoff - timedelta(minutes=2)),
        *market_ticks("fx", {"HOME": "1.40", "DRAW": "4.5", "AWAY": "8.0"}, "pinnacle", kickoff + timedelta(minutes=20)),  # in play: never the close
        tick("fx", "Match Odds", "HOME", "betfair", "1.99", kickoff - timedelta(minutes=1)),  # betfair: HOME only, incomplete
        *market_ticks("fx", {"HOME": "2.40", "DRAW": "3.0", "AWAY": "3.1"}, "onexbet", kickoff - timedelta(minutes=1)),  # not sharp
    ]
    close = feedback.close_from_ticks(ticks, "Match Odds", ("HOME", "DRAW", "AWAY"), kickoff, ("betfair", "pinnacle"))
    fair, z = shin_devig([1.95, 3.6, 4.0])
    assert close["HOME"].book == "pinnacle" and close["HOME"].odds == 1.95 and close["HOME"].fair_probability == pytest.approx(fair[0]) and close["HOME"].shin_z == pytest.approx(z)
    assert feedback.close_from_ticks(ticks, "Match Odds", ("HOME", "DRAW", "AWAY"), kickoff, ("betfair",)) == {}
    assert feedback.close_from_ticks(ticks, "Totals 2.5", ("OVER", "UNDER"), kickoff, ("pinnacle",)) == {}


# ================================================================ the loop end to end
NOW = datetime.now(UTC).replace(microsecond=0)
KICKOFF = NOW - timedelta(hours=3)
POISSON = {"WON": 0.52, "LOST": 0.48}
DIXON = {"WON": 0.50, "LOST": 0.50}
MARKET = {"WON": 0.47, "LOST": 0.53}


async def seed(sessions: async_sessionmaker[AsyncSession], user: User) -> dict[str, uuid.UUID]:
    """Four bets on four fixtures, their scores, their sharp closes in the tick lake, a twin audit and a watch."""
    ids: dict[str, uuid.UUID] = {}
    async with sessions() as session:
        audit = TwinVettingAudit(
            id=uuid.uuid4(), user_id=user.id, slip_id="slip-ars", kind="SINGLE", leg_ids=["fx-ars|Match Odds|HOME"], bookmaker="1xbet", total_odds=D("2.10"),
            stake_inr=D("1000"), kelly_fraction=0.01, pillars_passed=14, conviction_score=100.0, is_vetted=True, pillars=[], rejection_reasons=[], created_at=NOW - timedelta(hours=5),
            slip={"legs": [{"fixture_id": "fx-ars", "market": "Match Odds", "selection": "HOME", "models": {"poisson": 0.52, "dixon_coles": 0.50, "market": 0.47},
                            "distributions": {"poisson": POISSON, "dixon_coles": DIXON, "market": MARKET}, "prices": {"1xbet": 2.10}}]},
        )
        session.add(audit)
        await session.flush()

        def bet(key: str, odds: str, fixture: str, market: str, fair: float, *, audit_id: uuid.UUID | None = None, home: str = "H", away: str = "A") -> None:
            bet_id = ids[key] = uuid.uuid4()
            session.add(UserPlacedBet(id=bet_id, user_id=user.id, bookmaker="1XBET", structure="SINGLE", stake_inr=D("1000"), placed_odds=D(odds),
                                      placed_at=KICKOFF - timedelta(hours=4), status="PENDING", vetting_audit_id=audit_id, booking_code=f"1X-{key.upper()}"))
            session.add(UserPlacedLeg(id=uuid.uuid4(), bet_id=bet_id, position=0, fixture_id=fixture, home=home, away=away, sport_key="soccer_epl", kickoff=KICKOFF,
                                      market=market, selection="HOME", odds=D(odds), fair_probability=fair, result="PENDING"))

        bet("ars", "2.10", "fx-ars", "Match Odds", 0.50, audit_id=audit.id, home="Arsenal", away="Chelsea")  # wins, beat the close
        bet("liv", "1.45", "fx-liv", "Match Odds", 0.72, home="Liverpool", away="Everton")  # loses at 72%: the models were wrong
        bet("che", "1.90", "fx-che", "Asian Handicap -0.25", 0.55, home="Chelsea", away="Spurs")  # half loses, the line steamed away
        bet("mci", "1.80", "fx-mci", "Match Odds", 0.58, home="City", away="Wolves")  # loses after an in-play collapse
        await session.flush()
        session.add(TwinInPlayMonitor(id=uuid.uuid4(), bet_id=ids["mci"], user_id=user.id, target_profit_pct=0.5, initial_win_prob=0.58, current_win_prob=0.07,
                                      pullout_triggered=True, pullout_reason="PROBABILITY_COLLAPSE", is_active=False, ticks=40, detail={}, created_at=KICKOFF))
        for fixture, home, away, hg, ag in (("fx-ars", "Arsenal", "Chelsea", 2, 1), ("fx-liv", "Liverpool", "Everton", 0, 1), ("fx-che", "Chelsea", "Spurs", 1, 1), ("fx-mci", "City", "Wolves", 0, 2)):
            session.add(FixtureScore(fixture_id=fixture, home=home, away=away, sport_key="soccer_epl", kickoff=KICKOFF, home_goals=hg, away_goals=ag, status="FINAL", source="test"))
        close = KICKOFF - timedelta(minutes=3)
        for t in [
            *market_ticks("fx-ars", {"HOME": "1.95", "DRAW": "3.60", "AWAY": "4.00"}, "pinnacle", close),
            *market_ticks("fx-ars", {"HOME": "1.30", "DRAW": "5.00", "AWAY": "11.0"}, "pinnacle", KICKOFF + timedelta(minutes=30)),
            *market_ticks("fx-liv", {"HOME": "1.47", "DRAW": "4.50", "AWAY": "7.00"}, "pinnacle", close),
            *market_ticks("fx-che", {"HOME": "2.10", "AWAY": "1.80"}, "pinnacle", close, market="Asian Handicap -0.25"),
        ]:
            session.add(t)
        await session.commit()
    return ids


async def stream(redis: Redis, settings: Settings) -> list[SentinelAlert]:
    return [decode(fields["a"]) for _, fields in await redis.xrange(SentinelKeys(settings).stream)]


@pytest.mark.asyncio
async def test_settle_attribute_explain_and_page(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    ids = await seed(sessions, user)
    await write_intel(redis, settings, "fx-liv", FixtureIntel.model_validate(
        {"referee": {"source": "league", "observed_at": KICKOFF.isoformat(), "name": "Strict Sam", "cards_per_game": 5.6, "penalties_per_90": 0.2}}))
    report = await feedback.sweep(sessions, redis, settings, NOW)
    assert (report.settled_bets, report.attributed_bets, report.alerts) == (4, 4, 4)
    assert report.root_causes == {"MODEL_UNDERESTIMATION": 1, "STEAM_ADVERSE_SELECTION": 1, "INPLAY_SHOCK_RED_CARD": 1}
    async with sessions() as session:
        bets = {k: await session.get(UserPlacedBet, v) for k, v in ids.items()}
        ars, liv, che, mci = bets["ars"], bets["liv"], bets["che"], bets["mci"]
        assert (ars.status, ars.pnl_inr, ars.settlement_source, ars.root_cause_tag) == ("WON", D("1100.00"), "AUTOMATED", "NONE")
        fair, _ = shin_devig([1.95, 3.60, 4.00])
        assert ars.closing_odds == D("1.9500") and ars.clv_pct == pytest.approx(7.6923, abs=1e-3) and ars.clv_sharp_pct == pytest.approx((2.10 * fair[0] - 1) * 100)
        assert (che.status, che.return_inr, che.root_cause_tag) == ("HALF_LOST", D("500.00"), "STEAM_ADVERSE_SELECTION") and che.clv_pct == pytest.approx((1.90 / 2.10 - 1) * 100)
        # liv closed 1.47 (a +1.4% CLV, no steam) and lost at 72%: the models were wrong, ahead of the strict referee
        assert liv.root_cause_tag == "MODEL_UNDERESTIMATION" and liv.clv_pct == pytest.approx((1.45 / 1.47 - 1) * 100)
        assert mci.root_cause_tag == "INPLAY_SHOCK_RED_CARD" and mci.closing_odds is None and mci.clv_pct is None  # no close in the lake
        leg = (await session.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id == ars.id))).scalar_one()
        assert (leg.closing_odds, leg.closing_book) == (D("1.9500"), "pinnacle") and leg.closing_fair_probability == pytest.approx(fair[0], abs=1e-6)

        rows = {r.model_name: r for r in (await session.execute(select(ModelPredictionFeedback).where(ModelPredictionFeedback.bet_id == ars.id))).scalars()}
        assert set(rows) == {"poisson", "dixon_coles", "market", "ensemble", "closing_sharp"}  # the audit's models, Ashoka's ensemble, the close
        assert rows["poisson"].brier_score == pytest.approx((0.52 - 1) ** 2) and rows["poisson"].rps == pytest.approx((0.52 - 1) ** 2)
        assert rows["poisson"].log_loss == pytest.approx(-math.log(0.52)) and rows["poisson"].details["basis"] == "distribution"
        assert rows["closing_sharp"].predicted_prob == pytest.approx(fair[0], abs=1e-6) and rows["ensemble"].rps is None
        assert all(r.actual_outcome == 1.0 and r.clv_pct == pytest.approx(7.6923, abs=1e-3) and r.details["score"] == "2-1" for r in rows.values())
        half = (await session.execute(select(ModelPredictionFeedback).where(ModelPredictionFeedback.bet_id == che.id))).scalars().all()
        assert {(r.model_name, r.actual_outcome) for r in half} == {("ensemble", 0.25), ("closing_sharp", 0.25)}
        assert (await session.scalar(select(func.count()).select_from(ModelPredictionFeedback))) == 5 + 2 + 2 + 1
        audits = {a.bet_id: a for a in (await session.execute(select(SettlementRootCauseAudit))).scalars()}
        assert set(audits) == {liv.id, che.id, mci.id}
        assert audits[liv.id].evidence["referee"] == "Strict Sam, 5.6 cards a game" and audits[mci.id].evidence["inplay_collapse"] == [0.58, 0.07]

    alerts = [a for a in await stream(redis, settings) if a.kind is AlertKind.TWIN_SETTLED]
    by_bet = {a.detail["bet_id"]: a for a in alerts}
    win = by_bet[str(ids["ars"])]
    assert "+₹1,100.00" in win.title and "CLV +7.69%" in win.title and "1X-ARS" in win.body and "Developer: Amit Ashok Kumar Patnaik" in win.body
    debrief = by_bet[str(ids["che"])]
    assert "debrief half lost" in debrief.title and "steam adverse selection" in debrief.title and "Why: the sharp line closed" in debrief.body

    # idempotent: nothing left to settle or attribute, no new rows, no new pages
    again = await feedback.sweep(sessions, redis, settings, NOW + timedelta(minutes=5))
    assert (again.settled_bets, again.attributed_bets, again.feedback_records, again.alerts) == (0, 0, 0, 0)
    assert len([a for a in await stream(redis, settings) if a.kind is AlertKind.TWIN_SETTLED]) == 4


@pytest.mark.asyncio
async def test_a_backlog_is_attributed_silently_and_cashouts_are_not_paged(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    async with sessions() as session:
        old = UserPlacedBet(id=uuid.uuid4(), user_id=user.id, bookmaker="PARIMATCH", structure="SINGLE", stake_inr=D("500"), placed_odds=D("2.0"), placed_at=NOW - timedelta(days=4),
                            status="LOST", return_inr=D("0"), pnl_inr=D("-500"), settled_at=NOW - timedelta(days=3), settlement_source="AUTOMATED")
        session.add(old)
        session.add(UserPlacedLeg(id=uuid.uuid4(), bet_id=old.id, position=0, fixture_id="fx-old", home="X", away="Y", market="Match Odds", selection="AWAY", odds=D("2.0"),
                                  fair_probability=0.52, result="LOST", kickoff=NOW - timedelta(days=3, hours=2)))
        cashed = UserPlacedBet(id=uuid.uuid4(), user_id=user.id, bookmaker="PARIMATCH", structure="SINGLE", stake_inr=D("500"), placed_odds=D("2.0"), placed_at=NOW - timedelta(hours=2), status="PENDING")
        session.add(cashed)
        session.add(UserPlacedLeg(id=uuid.uuid4(), bet_id=cashed.id, position=0, fixture_id="fx-live", home="P", away="Q", market="Match Odds", selection="HOME", odds=D("2.0"), result="PENDING"))
        await session.flush()
        await tracker.record_cashout(session, cashed, D("700"), NOW)
        await session.commit()
    report = await feedback.sweep(sessions, redis, settings, NOW)
    assert (report.attributed_bets, report.alerts, report.feedback_records) == (2, 0, 1)  # the cashout's leg never settled: no prediction to score
    async with sessions() as session:
        assert (await session.get(UserPlacedBet, cashed.id)).settlement_source == "CASHOUT"
        assert (await session.get(UserPlacedBet, old.id)).root_cause_tag == "VARIANCE_BAD_LUCK"


@pytest.mark.asyncio
async def test_two_sweeps_at_once_attribute_each_bet_once(request: pytest.FixtureRequest, sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    if request.node.callspec.params["sessions"] == "sqlite":
        pytest.skip("row locks need PostgreSQL: SQLite serialises every writer on one shared connection")
    await seed(sessions, user)
    first, second = await asyncio.gather(
        feedback.sweep(sessions, redis, settings, NOW), feedback.sweep(sessions, redis, settings.model_copy(update={"FEEDBACK_BATCH_SIZE": 2}), NOW),
    )
    third = await feedback.sweep(sessions, redis, settings, NOW)
    assert first.attributed_bets + second.attributed_bets + third.attributed_bets == 4
    async with sessions() as session:
        rows = (await session.execute(select(ModelPredictionFeedback.leg_id, ModelPredictionFeedback.model_name))).all()
        assert len(rows) == len(set(rows)) == 10
        assert (await session.scalar(select(func.count()).select_from(SettlementRootCauseAudit))) == 3


@pytest.mark.asyncio
async def test_what_the_loop_records_is_what_the_engine_learns(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    """Group 73's feedback rows feed Group 74's engine, and its weights reach pillar 1 through the very hash it reads."""
    await seed(sessions, user)
    await feedback.sweep(sessions, redis, settings, NOW)
    # one settled prediction per model: under TWIN_RECALIBRATION_MIN_SAMPLES (2), every model stays ACTIVE, shrunk toward 1
    await redis.hset(weights_key(settings), mapping={"poisson": "1.3"})
    first = await model_calibrator.recalibrate(sessions, redis, settings, NOW, RecalibrationTrigger.SCHEDULED)
    assert first.published and set(first.published_weights) == {"poisson", "dixon_coles", "market"}  # ensemble and closing_sharp are references
    assert all(0.9 < w < 1.1 for w in first.published_weights.values())
    async with sessions() as session:
        audit = (await session.execute(select(ModelWeightAudit).where(ModelWeightAudit.run_id == first.id, ModelWeightAudit.model_name == "poisson"))).scalar_one()
        assert (audit.previous_weight, audit.status, audit.sample_count) == (1.3, "ACTIVE", 1)
    # a second settled Arsenal win, priced by the same audit snapshot
    async with sessions() as session:
        audit_id = (await session.execute(select(TwinVettingAudit.id))).scalar_one()
        bet = UserPlacedBet(id=uuid.uuid4(), user_id=user.id, bookmaker="1XBET", structure="SINGLE", stake_inr=D("100"), placed_odds=D("2.10"), placed_at=KICKOFF, status="PENDING", vetting_audit_id=audit_id)
        session.add(bet)
        session.add(UserPlacedLeg(id=uuid.uuid4(), bet_id=bet.id, position=0, fixture_id="fx-ars", home="Arsenal", away="Chelsea", market="Match Odds", selection="HOME",
                                  odds=D("2.10"), fair_probability=0.5, result="PENDING", kickoff=KICKOFF))
        await session.commit()
    await feedback.sweep(sessions, redis, settings, NOW)
    second = await model_calibrator.recalibrate(sessions, redis, settings, NOW, RecalibrationTrigger.SCHEDULED)
    # poisson (Brier 0.2304) beat the de-vigged close on both legs with +7.69% CLV: alpha boosted; dixon_coles (0.25) and
    # market (0.2809) are no better than a coin flip: benched
    weights = await model_weights(redis, settings)  # Group 72's reader
    assert weights == second.published_weights == {"poisson": 1.2, "dixon_coles": 0.0, "market": 0.0}  # alone, the softmax gives 1: clamped up to the alpha floor
    assert (second.models_promoted, second.models_demoted) == (1, 2)
    # pillar 1: the benched models neither vote nor veto
    from tests.test_ultra_vetting import inputs  # noqa: PLC0415 - the fortress's own fixtures

    strict, _ = pillar_1(inputs(weights=weights), FortressPolicy.from_settings(settings), {})
    assert strict.status.value == "FAIL" and "1 voting model(s) price it, 3 needed (dixon_coles, market benched)" in strict.reason
    lenient, _ = pillar_1(inputs(weights=weights), dataclasses.replace(FortressPolicy.from_settings(settings), min_models=1), {})
    assert lenient.status.value == "PASS" and lenient.metrics["legs"][0]["benched"] == ["dixon_coles", "market"]


# ================================================================ the API
def feedback_app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(feedback_api.router, prefix="/api/v1")
    app.state.redis = redis
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


@pytest.mark.asyncio
async def test_the_api_sweeps_reports_overrides_and_recalibrates(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User) -> None:
    ids = await seed(sessions, user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=feedback_app(sessions, redis, settings, user)), base_url="http://test") as client:
        swept = (await client.post("/api/v1/twin/settlement/sweep")).json()
        assert swept["settled_bets"] == 4 and swept["attributed_bets"] == 4 and swept["total_pnl_inr"] == "-1400.00" and swept["developer_credit"] == "Amit Ashok Kumar Patnaik"
        summary = (await client.get("/api/v1/twin/settlement/summary")).json()
        assert summary["clv"]["bets"] == 3 and summary["clv"]["beat_close_rate"] == pytest.approx(1 / 3, abs=1e-4)
        assert summary["root_causes"] == {"NONE": 1, "MODEL_UNDERESTIMATION": 1, "STEAM_ADVERSE_SELECTION": 1, "INPLAY_SHOCK_RED_CARD": 1}
        assert {r["root_cause_tag"] for r in summary["recent"]} == {"MODEL_UNDERESTIMATION", "STEAM_ADVERSE_SELECTION", "INPLAY_SHOCK_RED_CARD"}
        rows = (await client.get("/api/v1/twin/settlement/feedback", params={"model_name": "poisson"})).json()
        assert len(rows) == 1 and rows[0]["brier_score"] == pytest.approx(0.2304)
        accuracy = (await client.get("/api/v1/twin/settlement/model-accuracy")).json()
        models = {m["model_name"]: m for m in accuracy["models"]}
        assert models["closing_sharp"]["reference"] and models["closing_sharp"]["status"] is None and not models["poisson"]["eligible"]
        assert models["ensemble"]["predictions"] == 4
        curve = (await client.get("/api/v1/twin/settlement/calibration", params={"model_name": "ensemble"})).json()
        assert curve["predictions"] == 4 and sum(b["count"] for b in curve["bins"]) == 4 and curve["expected_calibration_error"] is not None
        published = (await client.post("/api/v1/twin/settlement/recalibrate-weights")).json()  # Group 74's engine, on demand
        assert published["published"] is True and published["trigger_type"] == "ON_DEMAND_ADMIN" and published["developer_credit"] == "Amit Ashok Kumar Patnaik"
        assert all(0.9 < w < 1.1 for w in published["weights"].values())  # one prediction per model, two needed for a verdict: shrunk toward 1
        after_run = {m["model_name"]: m for m in (await client.get("/api/v1/twin/settlement/model-accuracy")).json()["models"]}
        assert after_run["poisson"]["status"] == "ACTIVE" and after_run["poisson"]["published_weight"] == published["weights"]["poisson"]

        # the bookmaker voided the Liverpool bet after all: the administrator overrides, and attribution re-runs
        fixed = (await client.post("/api/v1/twin/settlement/override", json={"bet_id": str(ids["liv"]), "status": "VOID", "return_inr": "1000"})).json()
        assert fixed == {"bet_id": str(ids["liv"]), "status": "VOID", "return_inr": "1000.00", "pnl_inr": "0.00", "settlement_source": "MANUAL_OVERRIDE",
                         "feedback_due": True, "developer_credit": "Amit Ashok Kumar Patnaik"}
        assert (await client.post("/api/v1/twin/settlement/override", json={"bet_id": str(ids["liv"]), "status": "PENDING", "return_inr": "0"})).status_code == 422
        assert (await client.post("/api/v1/twin/settlement/override", json={"bet_id": str(uuid.uuid4()), "status": "WON", "return_inr": "1"})).status_code == 404
        rerun = (await client.post("/api/v1/twin/settlement/sweep")).json()
        assert rerun["attributed_bets"] == 1 and rerun["feedback_records"] == 0 and rerun["alerts"] == 0  # a void is not paged; its rows already exist
        after = (await client.get("/api/v1/twin/settlement/summary")).json()
        assert after["root_causes"].get("MODEL_UNDERESTIMATION") is None and after["root_causes"]["NONE"] == 2
        async with sessions() as session:
            assert (await session.scalar(select(func.count()).select_from(SettlementRootCauseAudit).where(SettlementRootCauseAudit.bet_id == ids["liv"]))) == 0

        # another user's rows are not theirs to see
        stranger = User(id=uuid.uuid4(), username="stranger", hashed_password="x")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=feedback_app(sessions, redis, settings, stranger)), base_url="http://test") as other:
            assert (await other.get("/api/v1/twin/settlement/feedback")).json() == []
            assert (await other.get("/api/v1/twin/settlement/summary")).json()["attributed_bets"] == 0

