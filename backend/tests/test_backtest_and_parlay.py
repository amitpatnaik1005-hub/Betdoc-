"""Group 77: the backtest additions, the in-play stop-loss shield, the manual parlay workbench and its rater.

The brief's proofs on the real code paths:

* rolling walk-forward folds tile the window (each tuned on its own past, judged on the next window), and the
  Lab's engine runs them end to end on the synthetic dataset; the square-root impact law and the largest share
  an order may take; the bootstrap's VaR and CVaR; Sharpe and Sortino net of the risk-free rate; Brier skill;
* the stop-loss floor (the offer, else fair value), the relative probability collapse, the recommended stop, the
  watch calling STOP_LOSS first; the bookmaker cashout tickets (manual: no book is driven), a cashout recorded on
  the bet through the API;
* the cognitive rater on a perfect, an average and a poor parlay, the cap a failed pillar puts on the score;
  the measured margins each skin prices with (overround per book, the best-price line, a parlay's compounding);
* Patent, Super Heinz and Goliath: their lines, and a Patent settling its singles;
* end to end through Redis and the API: the board, the inspection at a chosen account's book, the submission that
  arms the shield, the shield firing with its ticket, and its live frame.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import numpy as np
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.adapters.bookmakers.base_cashout_adapter import CashoutMode, adapter_for
from app.api.deps import get_current_admin, get_current_user, get_db
from app.api.v1 import digital_twin as twin_api
from app.api.v1 import manual_parlay as workbench_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.db.seed_historical_ticks import generate_dataset
from app.domain.backtesting import engine_math as em
from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.manual_parlay import cognitive_parlay_rater as rater
from app.domain.oracle.markets import LegResult
from app.domain.oracle.parlay_engine import SlipKind, lines_of
from app.models import User
from app.models.cfo_growth import CFOAdvisoryLog
from app.models.cfo_vault import BankrollAccount, MarketResult
from app.models.control_panel import SystemSettingsModel
from app.models.digital_twin import TwinInPlayMonitor, TwinVettingAudit
from app.models.hive_bots import TradingBot
from app.models.model_calibration import ModelRecalibrationRun, ModelWeightAudit
from app.models.never_forget import AshokaMistakeMemory, NeverForgetPreventionAudit, NeverForgetRule, UserXPProfile, XPAuditLog
from app.models.omni_vault import OmniFleetSource, VaultBookmakerAccount
from app.models.popular_picks import ParlayReviewGateModel, PopularParlayModel
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.lab_quant import BacktestParams
from app.services.backtesting.metrics import ratios
from app.services.backtesting.monte_carlo import risk_of_ruin
from app.services.backtesting.reality import RealityConfig, impact_multiplier, max_participation
from app.services.backtesting.replay_engine import HistoricalStore
from app.services.backtesting.runner import Window, execute, folds_for
from app.services.sentinel_bus import AlertKind
from app.services.twin import inplay
from app.services.user_pnl_tracker import bet_outcome
from tests.test_backtesting_lab import SMALL, _strategy, sim_bot
from tests.test_ultra_vetting import PINNACLE_NO_ARB, SOFT, intel_ok, seed, stream

D = Decimal
TABLES = [
    User.__table__, TradingBot.__table__, BankrollAccount.__table__, OmniFleetSource.__table__, MarketResult.__table__, SystemSettingsModel.__table__,
    UserPlacedBet.__table__, UserPlacedLeg.__table__, FixtureScore.__table__, PopularParlayModel.__table__, ParlayReviewGateModel.__table__,
    TwinVettingAudit.__table__, TwinInPlayMonitor.__table__, ModelRecalibrationRun.__table__, ModelWeightAudit.__table__, CFOAdvisoryLog.__table__,
    AshokaMistakeMemory.__table__, NeverForgetRule.__table__, NeverForgetPreventionAudit.__table__, UserXPProfile.__table__, XPAuditLog.__table__,
    VaultBookmakerAccount.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-workbench"
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


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
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya", "TWIN_PREFIX": "test_twin_wb", "ASHOKA_MC_PATHS": 4_000, "ORACLE_TIMEZONE": "Asia/Kolkata"})


async def make_user(sessions: async_sessionmaker[AsyncSession], role: str = "ADMIN") -> User:
    async with sessions() as session:
        user = User(id=uuid.uuid4(), username=f"wb_{uuid.uuid4().hex[:6]}", hashed_password="x", role=role)
        session.add(user)
        await session.commit()
        return user


# ================================================================ backtesting
def test_rolling_folds_tile_the_window_without_peeking() -> None:
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 4, 11, tzinfo=UTC)
    folds = em.rolling_folds(start, end, 4, 0.7)
    width = (end - start) / (0.7 + 4 * 0.3)
    assert len(folds) == 4 and folds[0].start == start and folds[-1].end == end
    for a, b in zip(folds, folds[1:]):
        assert a.end == b.split  # the out-of-sample windows follow one another exactly
    for f in folds:
        assert f.start < f.split < f.end and abs((f.split - f.start) - width * 0.7) < timedelta(seconds=1)  # each fold tunes on 70% of its own width
    single = em.rolling_folds(start, end, 1, 0.75)[0]
    assert single.split == (start + (end - start) * 0.75).replace(microsecond=0)  # one fold is the classic split
    with pytest.raises(ValueError):
        em.rolling_folds(start, end, 0, 0.7)


def test_square_root_impact_and_the_largest_share_kept_above_the_floor() -> None:
    assert em.sqrt_impact_multiplier(D("0.04"), D("0.5")) == D(1) - D("0.5") * D("0.2")  # 1 - k sqrt(0.04)
    assert em.executed_price(D("3.0"), D("400"), D("10000"), D("0.5")) == D(1) + D(2) * (D(1) - D("0.5") * D("0.2"))
    share = em.sqrt_max_participation(D("3.0"), D("2.8"), D("0.5"))
    impacted = D(1) + D(2) * em.sqrt_impact_multiplier(share, D("0.5"))
    assert impacted >= D("2.8") and abs(impacted - D("2.8")) < D("0.001")  # exactly the floor, no further
    config = RealityConfig(impact_model="sqrt", impact_coefficient=D("0.5"))
    assert impact_multiplier(D("0.04"), config) == em.sqrt_impact_multiplier(D("0.04"), D("0.5"))
    assert max_participation(D("3.0"), D("2.8"), config) == share
    quadratic = RealityConfig(impact_coefficient=D("2"))
    assert impact_multiplier(D("0.04"), quadratic) == D(1)  # the quadratic model leaves the first 5% alone
    with pytest.raises(ValueError):
        RealityConfig(impact_model="linear")


def test_var_cvar_brier_skill_and_the_risk_free_rate() -> None:
    pnl = np.arange(-50, 50, dtype=float)  # -50 .. 49
    tail = em.var_cvar(pnl)
    assert tail["95"]["var"] == pytest.approx(-np.quantile(pnl, 0.05)) and tail["95"]["cvar"] >= tail["95"]["var"]
    assert tail["99"]["var"] >= tail["95"]["var"] and em.var_cvar([])["95"] == {"var": 0.0, "cvar": 0.0}
    mc = risk_of_ruin([100.0, -40.0, 60.0, -80.0, 20.0] * 10, 10_000.0, iterations=500, seed=3)
    assert set(mc["bootstrap"]["var_cvar_inr"]) == {"95", "99"} and mc["bootstrap"]["var_cvar_inr"]["99"]["cvar"] >= mc["bootstrap"]["var_cvar_inr"]["95"]["var"]
    model = [(0.7, 1.0), (0.3, 0.0), (0.6, 1.0)]
    close = [(0.5, 1.0), (0.5, 0.0), (0.5, 1.0)]
    assert em.brier(model) == pytest.approx((0.09 + 0.09 + 0.16) / 3) and em.brier_skill(model, close) == pytest.approx(1 - ((0.34 / 3) / 0.25))
    assert em.implied_probability(D("2.0"), D("0"), D("0.1")) == pytest.approx(0.55)
    days = [(NOW + timedelta(days=i), 100.0 * 1.001 ** (i + 1)) for i in range(30)]
    plain, net = ratios(days, 100.0), ratios(days, 100.0, 0.04)
    assert net["sharpe"] is not None and plain["sharpe"] is not None and net["sharpe"] < plain["sharpe"] or plain["sharpe"] is None


def test_the_lab_runs_rolling_folds_with_square_root_impact(settings: Settings) -> None:
    data = generate_dataset(**SMALL)
    first, last = data.span
    params = BacktestParams(strategies=[_strategy()], sweep_steps=2, monte_carlo_iterations=200, train_ratio=0.7, walk_forward_folds=2, impact_model="sqrt", risk_free_rate=0.04)
    window = Window(first, last, first + (last - first) * 0.7)
    folds = folds_for(params, window)
    assert len(folds) == 2
    full = HistoricalStore.from_dataset(data, horizon=last + timedelta(days=3))
    in_sample = HistoricalStore.from_dataset(data, horizon=window.split)
    fold_stores = [HistoricalStore.from_dataset(data, horizon=f.split) for f in folds]
    for f, store in zip(folds, fold_stores, strict=True):
        assert store.span is None or store.span[1] <= f.split  # no fold's in-sample store holds a row from its future
    bot = sim_bot("Shin consensus", math_models=("math.consensus", "math.devig_shin", "math.kelly_criterion"), risk_models=("risk.drawdown", "risk.exposure"),
                  target_bet_types=("bet.match_winner_1x2", "bet.over_under_goals", "bet.single"), max_bets_per_minute=3, cooldown_seconds=300)
    result = execute([bot], params, window, full, in_sample, settings, None, fold_stores)
    rolling = result["walk_forward"]["rolling"]
    assert [r["fold"] for r in rolling["folds"]] == [1, 2] and rolling["summary"]["folds"] == 2 and rolling["train_ratio"] == 0.7
    assert rolling["folds"][0]["out_of_sample_window"][1] == rolling["folds"][1]["out_of_sample_window"][0]
    assert all(r["verdict"]["verdict"] in {"ROBUST", "DEGRADED", "OVERFIT", "INSUFFICIENT_DATA", "NO_EDGE"} for r in rolling["folds"])
    assert result["metrics"]["risk_free_rate"] == 0.04 and "brier_skill_score" in result["metrics"] and "var_cvar_inr" in result["monte_carlo"]["bootstrap"]
    with pytest.raises(Exception, match="one in-sample store per fold"):
        execute([bot], params, window, full, in_sample, settings, None, fold_stores[:1])


# ================================================================ the stop-loss shield
def test_the_stop_loss_rules(settings: Settings) -> None:
    policy = slm.StopLossPolicy.from_settings(settings)
    assert (policy.default_pct, policy.min_pct, policy.max_pct, policy.collapse_ratio) == (0.25, 0.15, 0.40, 0.35)
    assert policy.clamp(None) == 0.25 and policy.clamp(0.05) == 0.15 and policy.clamp(0.9) == 0.40
    assert slm.floor(D("1000"), 0.25) == D("750.00")
    offer = slm.evaluate(D("1000"), 0.25, offer=D("740"), fair_value=D("900"), entry_probability=0.5, live_probability=0.45, policy=policy)
    assert offer is not None and offer.rule == "STOP_LOSS_FLOOR" and offer.value_source == "offer"  # the offer is what can be banked
    fair = slm.evaluate(D("1000"), 0.25, offer=None, fair_value=D("750"), entry_probability=0.5, live_probability=0.45, policy=policy)
    assert fair is not None and fair.value_source == "fair_value"
    collapse = slm.evaluate(D("1000"), 0.25, offer=None, fair_value=D("800"), entry_probability=0.5, live_probability=0.17, policy=policy)
    assert collapse is not None and collapse.rule == "PROBABILITY_COLLAPSE"
    assert slm.evaluate(D("1000"), 0.25, offer=None, fair_value=D("800"), entry_probability=0.5, live_probability=0.18, policy=policy) is None
    assert slm.recommended_pct(0.0, policy) == 0.40 and slm.recommended_pct(1.0, policy) == 0.15 and 0.15 < slm.recommended_pct(0.3, policy) < 0.40
    with pytest.raises(ValueError):
        slm.StopLossPolicy.from_settings(settings.model_copy(update={"TWIN_STOP_LOSS_MIN_PCT": 0.5}))


def test_the_watch_calls_the_stop_loss_first_and_writes_the_books_ticket(settings: Settings) -> None:
    from app.domain.oracle.cashout_advisor import Advice, CashoutAdvice  # noqa: PLC0415

    bet = UserPlacedBet(id=uuid.uuid4(), stake_inr=D("1000"), bookmaker="PARIMATCH", booking_code="PM-123", placed_at=NOW)
    monitor = TwinInPlayMonitor(id=uuid.uuid4(), initial_win_prob=0.5, target_profit_pct=0.4, stop_loss_pct=0.30, detail={})
    advice = CashoutAdvice(Advice.HOLD, D("690"), D("2450"), 0.28, None, None, None, None, None, ("hold",))
    valuation = inplay.Valuation(0.28, advice)
    reason, action = inplay.pullout(monitor, bet, valuation, settings)  # type: ignore[misc]
    assert reason.value == "STOP_LOSS" and "30% stop-loss floor ₹700.00" in action
    ticket = inplay.cashout_ticket(monitor, bet, valuation, action, settings, NOW)
    assert ticket["label"] == "Parimatch" and ticket["mode"] == "MANUAL" and ticket["floor_inr"] == "700.00" and ticket["value_source"] == "fair_value"
    assert "booking code PM-123" in ticket["instructions"][0] and any("does not log in" in c for c in ticket["caveats"])
    assert [adapter_for(b).label for b in ("PARIMATCH", "1XBET", "STAKE", "BETFAIR")] == ["Parimatch", "1xBet", "Stake", "your bookmaker"]
    assert adapter_for("STAKE").mode is CashoutMode.MANUAL


# ================================================================ the rater and the margins
def pillars(statuses: list[str]) -> list[dict[str, Any]]:
    keys = ["model_consensus", "weather", "travel_fatigue", "injuries", "lineups", "market_microstructure", "sharp_price", "crowd_parlays", "referee",
            "motivation", "liquidity", "independence", "sizing", "execution_gate", "never_forget"]
    return [{"number": i + 1, "key": keys[i], "title": keys[i].replace("_", " "), "status": s, "reason": f"reason {i + 1}"} for i, s in enumerate(statuses)]


def test_the_rater_on_good_average_and_poor_parlays(settings: Settings) -> None:
    policy = rater.RaterPolicy.from_settings(settings)
    perfect = rater.rate(pillars(["PASS"] * 15), policy)
    assert (perfect.score, perfect.tier, perfect.advice) == (100.0, "PERFECT", [])
    good = rater.rate(pillars(["PASS"] * 10 + ["UNVERIFIED"] * 5), policy)  # 10 + 5 x 0.25 = 11.25 of 15
    assert good.score == 75.0 and good.tier == "BRILLIANT" and len(good.advice) == 5
    average = rater.rate(pillars(["PASS"] * 6 + ["ADVISORY"] * 2 + ["UNVERIFIED"] * 7), policy)  # (6 + 1 + 1.75) / 15
    assert average.score == pytest.approx(58.33, abs=0.01) and average.tier == "AVERAGE"
    vetoed = rater.rate(pillars(["PASS"] * 11 + ["FAIL"] + ["PASS"] * 3), policy)  # 93.3 raw, but leg independence failed
    assert vetoed.score == 59.0 and vetoed.tier == "AVERAGE" and vetoed.capped and vetoed.warnings == ["Correlation: reason 12"]
    poor = rater.rate(pillars(["FAIL"] * 3 + ["UNVERIFIED"] * 12), policy)
    assert poor.score == 20.0 and poor.tier == "POOR" and any("Line-shop" in a or "no edge" in a for a in poor.advice)
    assert [rater.tier_for(s, policy) for s in (95, 85, 75, 60, 45, 44.99)] == ["PERFECT", "EXTRAORDINARY", "BRILLIANT", "GOOD", "AVERAGE", "POOR"]


def test_chameleon_margins_are_measured_per_book() -> None:
    quotes = {"HOME": {"parimatch": 2.40, "1xbet": 2.45, "stake": 2.38}, "DRAW": {"parimatch": 3.20, "1xbet": 3.10, "stake": 3.25}, "AWAY": {"parimatch": 3.10, "1xbet": 3.20}}
    m = rater.market_margins(["HOME", "DRAW", "AWAY"], quotes)
    assert m["by_book"] == {"1xbet": pytest.approx(1 / 2.45 + 1 / 3.1 + 1 / 3.2 - 1, abs=1e-6), "parimatch": pytest.approx(1 / 2.4 + 1 / 3.2 + 1 / 3.1 - 1, abs=1e-6)}
    assert "stake" not in m["by_book"]  # Stake does not quote AWAY: no overround to measure
    assert m["best_price"] == pytest.approx(1 / 2.45 + 1 / 3.25 + 1 / 3.2 - 1, abs=1e-6) and m["best_books"] == {"HOME": "1xbet", "DRAW": "stake", "AWAY": "1xbet"}
    assert m["best_price"] < min(m["by_book"].values())  # line shopping beats every single book
    assert rater.overround({"YES": 1.9, "NO": 1.9}) == pytest.approx(2 / 1.9 - 1) and rater.overround({"YES": 1.0, "NO": 3.0}) is None


def test_patent_super_heinz_and_goliath() -> None:
    assert [len(lines_of(k, n)) for k, n in ((SlipKind.PATENT, 3), (SlipKind.SUPER_HEINZ, 7), (SlipKind.GOLIATH, 8))] == [7, 120, 247]
    bet = UserPlacedBet(structure="PATENT", stake_inr=D("70"), unit_stake_inr=D("10"))
    legs = [UserPlacedLeg(odds=D("2.0"), result=r) for r in (LegResult.WON.value, LegResult.LOST.value, LegResult.LOST.value)]
    status, payout = bet_outcome(bet, legs)  # type: ignore[misc]
    assert status is PlacedStatus.HALF_LOST and payout == D("20.00")  # one single won at 2.0: ₹20 back of ₹70 (the ledger's partial return)
    legs[1].result = LegResult.WON.value
    status, payout = bet_outcome(bet, legs)  # type: ignore[misc]
    assert status is PlacedStatus.WON and payout == D("80.00")  # two singles (₹40) and their double (₹40)


# ================================================================ the API: the shield and the accounts
def app_for(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    for module in (twin_api, workbench_api):
        app.include_router(module.router, prefix="/api/v1")
    app.state.redis = redis

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


async def watched_bet(sessions: async_sessionmaker[AsyncSession], user: User, *, book: str = "1XBET") -> tuple[UserPlacedBet, TwinInPlayMonitor]:
    async with sessions() as session:
        bet = UserPlacedBet(id=uuid.uuid4(), user_id=user.id, bookmaker=book, structure="SINGLE", stake_inr=D("1000"), placed_odds=D("2.45"), placed_at=NOW, booking_code="1X-777")
        session.add(bet)
        await session.flush()  # no relationship() orders the two inserts: PostgreSQL checks the foreign key
        monitor = TwinInPlayMonitor(id=uuid.uuid4(), bet_id=bet.id, user_id=user.id, is_active=True, target_profit_pct=0.5, stop_loss_pct=0.25, initial_win_prob=0.46,
                                    current_win_prob=0.40, fair_value_inr=D("880"), ticks=3, detail={}, created_at=NOW)
        session.add(monitor)
        await session.commit()
        return bet, monitor


@pytest.mark.asyncio
async def test_emergency_cashout_issues_the_ticket_then_records_what_was_received(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    user, stranger = await make_user(sessions), await make_user(sessions, role="QUANT")
    bet, monitor = await watched_bet(sessions, user)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, None, settings, user)), base_url="http://test") as client:
        listed = (await client.get("/api/v1/manual-parlay/live-shields")).json()
        assert [s["id"] for s in listed["shields"]] == [str(monitor.id)] and listed["shields"][0]["frame"]["floor_inr"] == "750.00"
        assert listed["developer_credit"] == "Amit Ashok Kumar Patnaik"
        issued = (await client.post(f"/api/v1/manual-parlay/emergency-cashout/{monitor.id}", json={})).json()
        assert issued["recorded"] is False and issued["ticket"]["label"] == "1xBet" and issued["ticket"]["value_inr"] == "880.00"
        assert issued["shield"]["is_active"] is False and issued["shield"]["pullout_reason"] == "MANUAL_USER_REQUEST"
        recorded = (await client.post(f"/api/v1/manual-parlay/emergency-cashout/{monitor.id}", json={"amount_inr": "712.50"})).json()
        assert recorded["recorded"] is True and recorded["status"] == "CASHED_OUT" and recorded["salvaged_inr"] == "712.50" and recorded["pnl_inr"] == "-287.50"
        again = await client.post(f"/api/v1/manual-parlay/emergency-cashout/{monitor.id}", json={"amount_inr": "1"})
        assert again.status_code == 409 and again.json()["detail"]["reason"] == "ALREADY_SETTLED"
        shown = (await client.get("/api/v1/manual-parlay/live-shields", params={"active_only": False})).json()["shields"][0]
        assert shown["detail"]["salvaged_inr"] == "712.50" and shown["detail"]["salvaged_pct_of_stake"] == 0.7125
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, None, settings, stranger)), base_url="http://test") as client:
        assert (await client.post(f"/api/v1/manual-parlay/emergency-cashout/{monitor.id}", json={})).status_code == 404  # not their shield
        hidden = (await client.get("/api/v1/manual-parlay/accounts")).json()
        assert hidden["visible"] is False and hidden["accounts"] == {}


@pytest.mark.asyncio
async def test_the_skin_balance_mirror_reads_the_vault(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    admin = await make_user(sessions)
    async with sessions() as session:
        for book, balance, currency in (("parimatch", "25000", "INR"), ("parimatch", "5000", "INR"), ("stake", "300", "USD"), ("pinnacle", "900", "EUR")):
            session.add(VaultBookmakerAccount(id=uuid.uuid4(), bookmaker_id=book, label=book, identity_digest=uuid.uuid4().hex, secrets_fingerprint=uuid.uuid4().hex,
                                              currency=currency, balance=D(balance), reserved=D("100")))
        await session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, None, settings, admin)), base_url="http://test") as client:
        shown = (await client.get("/api/v1/manual-parlay/accounts")).json()
    assert shown["visible"] is True and set(shown["accounts"]) == {"parimatch", "stake"}  # the account books only
    assert D(shown["accounts"]["parimatch"]["balances"]["INR"]) == D("30000") and shown["accounts"]["parimatch"]["accounts"] == 2
    assert D(shown["accounts"]["stake"]["balances"]["USD"]) == D("300")


# ================================================================ end to end through Redis
@pytest.mark.asyncio
async def test_board_inspect_submit_and_the_shield_firing_live(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    user = await make_user(sessions)
    fixture = "fx-ars-che"
    await seed(redis, settings, fixture, "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        board = (await client.get("/api/v1/manual-parlay/board")).json()
        assert board["sports"]["Football"] == 1 and board["fixtures"][0]["fixture_id"] == fixture
        odds = next(m for m in board["fixtures"][0]["markets"] if m["market"] == "Match Odds")
        home = next(s for s in odds["selections"] if s["selection"] == "HOME")
        assert home["prices"]["1xbet"] == 2.45 and home["best"] == {"book": "1xbet", "odds": 2.45} and set(odds["margins"]["by_book"]) >= {"1xbet", "parimatch"}
        assert (await client.get("/api/v1/manual-parlay/board", params={"sport": "Cricket"})).json()["fixtures"] == []
        assert (await client.get("/api/v1/manual-parlay/board", params={"sport": "Curling"})).status_code == 422

        await client.put(f"/api/v1/twin/intel/{fixture}", json=intel_ok(datetime.now(UTC)).model_dump(mode="json", exclude_none=True))
        refused = await client.post("/api/v1/manual-parlay/inspect", json={"leg_ids": [home["leg_id"]], "skin": "stake", "bankroll_inr": "100000"})
        assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "NO_RETAIL_BOOK"  # Stake does not quote this leg
        inspected = (await client.post("/api/v1/manual-parlay/inspect", json={"leg_ids": [home["leg_id"]], "skin": "one_xbet", "bankroll_inr": "100000"})).json()
        assert inspected["book"] == "1xbet" and inspected["audit"]["is_vetted"] is True and inspected["rating"]["tier"] in ("PERFECT", "EXTRAORDINARY")
        assert len(inspected["rating"]["breakdown"]) == 15 and inspected["margins"]["legs"][0]["account_margin"] == odds["margins"]["by_book"]["1xbet"]
        assert inspected["margins"]["parlay_overround"] == pytest.approx(odds["margins"]["by_book"]["1xbet"], abs=1e-6)
        assert 0.15 <= inspected["stop_loss"]["recommended_pct"] <= 0.40 and inspected["developer_credit"] == "Amit Ashok Kumar Patnaik"

        placed = await client.post("/api/v1/manual-parlay/submit", json={"audit_id": inspected["audit"]["id"], "skin": "one_xbet", "stake_inr": "1000", "placed_odds": "2.45",
                                                                          "booking_code": "1X-555", "stop_loss_pct": 0.3})
        assert placed.status_code == 201, placed.text
        bet = placed.json()
        assert bet["bookmaker"] == "1XBET" and bet["booking_code"] == "1X-555" and bet["shield"]["armed"] and bet["shield"]["shield"]["stop_loss_pct"] == 0.3

        # a red card for Arsenal: the shield fires on the next tick, with 1xBet's cashout ticket and a live frame
        await seed(redis, settings, fixture, "Arsenal", "Chelsea", sharp={"HOME": 15.0, "DRAW": 5.5, "AWAY": 1.22}, soft={b: {"HOME": 14.0, "DRAW": 5.2, "AWAY": 1.20} for b in SOFT})
        fired = (await client.post("/api/v1/twin/monitors/tick")).json()
        assert [a["reason"] for a in fired["alerts"]] == ["STOP_LOSS"] and fired["alerts"][0]["ticket"]["label"] == "1xBet"
        frames = await redis.hgetall(inplay.last_frames_key(settings, user.id))
        (raw,) = frames.values()
        assert '"pullout_reason":"STOP_LOSS"' in raw and '"floor_inr":"700.00"' in raw
        pages = [a for a in await stream(redis, settings) if a.kind is AlertKind.TWIN_PULLOUT]
        assert pages[-1].severity.value == "WARNING" and "STOP-LOSS" in pages[-1].title and "booking code 1X-555" in pages[-1].body
        shields = (await client.get("/api/v1/manual-parlay/live-shields", params={"active_only": False})).json()["shields"]
        assert shields[0]["pullout_reason"] == "STOP_LOSS" and shields[0]["detail"]["cashout_ticket"]["floor_inr"] == "700.00"
        assert math.isclose(shields[0]["stop_loss_pct"], 0.3)
