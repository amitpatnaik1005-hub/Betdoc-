"""Group 76: KUMBHA's capital growth. One sizing policy, forecasts over the ledger, the regime and its latch, rebalancing.

The brief's proofs on the real code paths:

* full Kelly (b p - q) / b; the damper phi(D) at 1, 0.5, 0.25 and 0 across the regimes; psi(BSS) and the long-shot
  discount at their bounds; the ceiling applied before the damper, so the damper always bites;
* the ruin bound (1 - D)^(2/kappa - 1): 1/2, 1/8 and 1/128 for full, half and quarter Kelly at a 50% drawdown;
* the Monte Carlo: exact on a deterministic history, reproducible from its seed, ordered percentiles, the halt
  latch freezing a losing path short of ruin;
* water-filling: the EV-share targets, the largest surplus into the largest deficit, the minimum transfer.

Then the services over SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set): the history the forecast
bootstraps, the regime advisories and the latch (pillar 13 stays at zero until a sign-off), the skill term, the
rebalance plan from the Vault's balances and the ledger's EV, and the API end to end.
"""

from __future__ import annotations

import dataclasses
import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import numpy as np
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_admin, get_current_user
from app.api.v1 import cfo_growth as growth_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.cfo import growth_math as gm
from app.domain.oracle import fortress
from app.models import User
from app.models.cfo_growth import CFOAdvisoryLog, CFOGrowthSimulation, CFORebalanceRecommendation, InsightCode, RebalanceStatus
from app.models.cfo_vault import BankrollAccount
from app.models.control_panel import SystemSettingsModel
from app.models.hive_bots import TradingBot
from app.models.model_calibration import ModelRecalibrationRun, ModelWeightAudit
from app.models.omni_vault import OmniFleetSource, VaultBookmakerAccount
from app.models.user_bets_ledger import UserPlacedBet, UserPlacedLeg
from app.services.cfo import growth_optimizer as growth
from app.services.sentinel_bus import AlertKind, SentinelKeys, decode

D = Decimal
TABLES = [
    User.__table__, TradingBot.__table__, OmniFleetSource.__table__, BankrollAccount.__table__, SystemSettingsModel.__table__, UserPlacedBet.__table__,
    UserPlacedLeg.__table__, ModelRecalibrationRun.__table__, ModelWeightAudit.__table__, VaultBookmakerAccount.__table__,
    CFOGrowthSimulation.__table__, CFOAdvisoryLog.__table__, CFORebalanceRecommendation.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-cfo-growth"
NOW = datetime.now(UTC).replace(microsecond=0)


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
    return get_settings().model_copy(update={"TWIN_PREFIX": "test_twin_cfo", "CFO_MIN_HISTORY_BETS": 30, "CFO_COMPARISON_PATHS": 400})


def policy(settings: Settings) -> gm.SizingPolicy:
    return gm.SizingPolicy.from_settings(settings)


async def make_user(sessions: async_sessionmaker[AsyncSession], *, role: str = "ADMIN", bankroll: Decimal | None = D("100000")) -> User:
    async with sessions() as session:
        user = User(id=uuid.uuid4(), username=f"kumbha_{uuid.uuid4().hex[:6]}", hashed_password="x", role=role)
        session.add(user)
        await session.flush()
        if bankroll is not None:
            session.add(BankrollAccount(id=uuid.uuid4(), user_id=user.id, available_balance=bankroll, exposure_balance=D("0"), peak_balance=bankroll))
        await session.commit()
        return user


async def seed_bets(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID, n: int, *, odds: str = "2.10", p: float = 0.52, wins_every: int = 2,
                    book: str = "1XBET", stake: str = "1000", days: float = 30.0, settled: bool = True) -> None:
    """``n`` singles placed evenly over ``days``: every ``wins_every``-th wins at the price, the rest lose."""
    async with sessions() as session:
        for i in range(n):
            won = i % wins_every == 0
            placed = NOW - timedelta(days=days) + timedelta(days=days * i / n)
            bet = UserPlacedBet(id=uuid.uuid4(), user_id=user_id, bookmaker=book, structure="SINGLE", stake_inr=D(stake), placed_odds=D(odds), placed_at=placed,
                                status=("WON" if won else "LOST") if settled else "PENDING",
                                pnl_inr=(D(stake) * (D(odds) - 1) if won else -D(stake)) if settled else None, settled_at=placed + timedelta(hours=2) if settled else None)
            session.add(bet)
            session.add(UserPlacedLeg(id=uuid.uuid4(), bet_id=bet.id, fixture_id=f"fx-{i}", home="A", away="B", market="Match Odds", selection="HOME", odds=D(odds),
                                      fair_probability=p, result=("WON" if won else "LOST") if settled else "PENDING"))
        await session.commit()


# ================================================================ the mathematics
def test_kelly_the_damper_and_the_ceiling_before_it(settings: Settings) -> None:
    assert gm.kelly_fraction(2.0, 0.55) == pytest.approx(0.10)  # b = 1: (0.55 - 0.45) / 1
    assert gm.kelly_fraction(2.0, 0.45) == 0.0 and gm.kelly_fraction(1.0, 0.9) == 0.0 and gm.kelly_fraction(3.0, 1.0) == 0.0
    rules = policy(settings)
    assert [(gm.damper(d, rules).name, gm.damper(d, rules).multiplier) for d in (0.04, 0.10, 0.12, 0.17, 0.20, 0.21)] == [
        ("OPTIMAL_GROWTH", 1.0), ("CAUTIOUS_THROTTLED", 0.5), ("CAUTIOUS_THROTTLED", 0.5), ("DEFENSIVE_CAPITAL_PRESERVATION", 0.25),
        ("CIRCUIT_BREAKER_HALT", 0.0), ("CIRCUIT_BREAKER_HALT", 0.0)]
    assert gm.damper(0.0, rules, latched=True).name == "LATCHED_HALT" and gm.damper(0.0, rules, latched=True).halted
    with pytest.raises(ValueError):
        gm.SizingPolicy.from_settings(settings.model_copy(update={"CFO_DRAWDOWN_DEFENSIVE_THRESHOLD_PCT": 25.0}))
    # psi and lambda at their bounds
    assert gm.skill_multiplier(None, rules) == 1.0 and gm.skill_multiplier(0.1, rules) == pytest.approx(1.2)
    assert gm.skill_multiplier(-0.5, rules) == 0.6 and gm.skill_multiplier(0.5, rules) == 1.5
    assert gm.longshot_discount(2.1, rules) == 1.0 and gm.longshot_discount(15.0, rules) == pytest.approx((5 / 15) ** 0.75) and gm.longshot_discount(500.0, rules) == 0.2
    # f = min(f* kappa psi lambda, f_max) x phi
    s = gm.size(0.08, 2.1, D("100000"), 0.0, rules, D("50"), bss=0.1)
    assert s.fraction == pytest.approx(0.08 * 0.25 * 1.2) and s.stake == D("2400") and s.skill == pytest.approx(1.2)
    big = gm.size(0.60, 2.1, D("100000"), 0.17, rules, D("50"))
    assert big.capped and big.fraction == pytest.approx(0.025 * 0.25) and big.stake == D("600")  # capped, then the defensive damper


def test_the_ruin_bound() -> None:
    assert gm.ruin_probability(1.0, 0.5) == pytest.approx(0.5)
    assert gm.ruin_probability(0.5, 0.5) == pytest.approx(0.125)
    assert gm.ruin_probability(0.25, 0.5) == pytest.approx(0.5 ** 7) == pytest.approx(0.0078125)
    with pytest.raises(ValueError):
        gm.ruin_probability(0.0, 0.5)


def history(n: int, odds: float, p: float, returns: list[float], per_day: float) -> gm.History:
    return gm.History(np.full(n, odds), np.full(n, p), np.array(returns), per_day)


def test_the_forecast_is_exact_on_certain_wins_and_reproducible(settings: Settings) -> None:
    rules = policy(settings)
    fixed = gm.Strategy("FIXED_FRACTION_1PCT", "1%", fixed=0.01)
    sure = history(10, 2.0, 0.6, [1.0] * 10, 2.0)
    f = gm.simulate(sure, fixed, rules, start=100000.0, horizon_days=30, paths=1000, seed=1)
    assert f.trades == 60 and f.median_end == pytest.approx(100000 * 1.01 ** 60) and f.prob_halt == 0.0 and f.prob_ruin == 0.0
    assert f.cagr == pytest.approx((1.01 ** 60) ** (365 / 30) - 1) and f.curve[0]["p50"] == 100000.0 and f.curve[-1]["day"] == 30
    mixed = history(4, 2.1, 0.52, [1.1, -1.0, 1.1, -1.0], 3.0)
    quarter = gm.Strategy("QUARTER_KELLY", "q", kelly=0.25)
    a = gm.simulate(mixed, quarter, rules, start=50000.0, horizon_days=90, paths=2000, seed=42)
    b = gm.simulate(mixed, quarter, rules, start=50000.0, horizon_days=90, paths=2000, seed=42)
    c = gm.simulate(mixed, quarter, rules, start=50000.0, horizon_days=90, paths=2000, seed=43)
    assert a.as_dict() == b.as_dict() and a.median_end != c.median_end  # the seed reproduces the run
    for point in a.curve:
        values = [point[f"p{q}"] for q in gm.PERCENTILES]
        assert values == sorted(values)
    assert a.sharpe is not None and a.sortino is not None


def test_the_circuit_breaker_stops_a_losing_path_short_of_ruin(settings: Settings) -> None:
    rules = policy(settings)
    losing = history(5, 2.0, 0.55, [-1.0] * 5, 5.0)
    fixed = gm.Strategy("FIXED_FRACTION_2PCT", "2%", fixed=0.02)
    f = gm.simulate(losing, fixed, rules, start=100000.0, horizon_days=90, paths=200, seed=3)
    assert f.prob_halt == 1.0 and f.prob_ruin == 0.0  # it halts at 20% and never gets near 50%
    assert 0.79 < f.median_end / 100000 < 0.81 and f.median_max_drawdown == pytest.approx(1 - f.median_end / 100000)
    unguarded = dataclasses.replace(rules, regimes=(gm.Regime("OPEN", 0.0, 1.0), gm.Regime("NEVER", 0.999999, 0.0)))
    assert gm.simulate(losing, fixed, unguarded, start=100000.0, horizon_days=90, paths=200, seed=3).prob_ruin == 1.0  # without it: ruin


def test_water_filling(settings: Settings) -> None:
    balances = {"parimatch": D("50000"), "1xbet": D("10000"), "stake": D("40000")}
    plan = gm.plan(balances, {"parimatch": 100.0, "1xbet": 300.0, "stake": 0.0}, 1.0, D("5000"))
    assert plan is not None and plan.targets == {"parimatch": D("25000.00"), "1xbet": D("75000.00"), "stake": D("0.00")}
    assert [(t.source, t.destination, t.amount) for t in plan.transfers] == [("stake", "1xbet", D("40000.00")), ("parimatch", "1xbet", D("25000.00"))]
    weights = gm.plan(balances, {"parimatch": 100.0, "1xbet": 300.0}, 0.8, D("5000")).weights  # type: ignore[union-attr]
    assert weights["1xbet"] == pytest.approx(300 ** 0.8 / (100 ** 0.8 + 300 ** 0.8)) and weights["stake"] == 0.0
    near = gm.plan({"a": D("50000"), "b": D("49000")}, {"a": 1.0, "b": 1.0}, 1.0, D("5000"))
    assert near is not None and near.transfers == []  # a ₹500 imbalance is under the minimum transfer
    assert gm.plan(balances, {}, 0.8, D("5000")) is None  # no EV anywhere: no allocation to make


# ================================================================ the services
@pytest.mark.asyncio
async def test_the_history_the_forecast_bootstraps(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    user = await make_user(sessions)
    await seed_bets(sessions, user.id, 40, days=20)
    await seed_bets(sessions, user.id, 5, settled=False)  # pending: not history
    async with sessions() as session:
        bet = UserPlacedBet(id=uuid.uuid4(), user_id=user.id, bookmaker="1XBET", structure="SINGLE", stake_inr=D("100"), placed_odds=D("2"), placed_at=NOW - timedelta(days=1),
                            status="LOST", pnl_inr=D("-100"), settled_at=NOW)
        session.add(bet)
        session.add(UserPlacedLeg(id=uuid.uuid4(), bet_id=bet.id, fixture_id="manual", home="A", away="B", market="Match Odds", selection="HOME", odds=D("2"), result="LOST"))
        await session.commit()  # no model probability: not history
        hist = await growth.history(session, user.id, settings, NOW)
    assert hist.odds.size == 40 and hist.per_day == pytest.approx(2.0, rel=0.01)
    assert float(np.mean(hist.unit_return > 0)) == 0.5 and hist.unit_return.max() == pytest.approx(1.1) and hist.full_kelly[0] == pytest.approx((1.1 * 0.52 - 0.48) / 1.1)


@pytest.mark.asyncio
async def test_a_forecast_needs_history_and_is_recorded(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    user = await make_user(sessions)
    await seed_bets(sessions, user.id, 10)
    with pytest.raises(growth.NotEnoughHistory) as refused:
        await growth.forecast(sessions, None, settings, user.id, NOW, strategy="QUARTER_KELLY", horizon_days=30, paths=1000)
    assert (refused.value.found, refused.value.needed) == (10, 30)
    await seed_bets(sessions, user.id, 30)
    with pytest.raises(ValueError):
        await growth.forecast(sessions, None, settings, user.id, NOW, strategy="QUARTER_KELLY", horizon_days=45, paths=1000)
    with pytest.raises(ValueError):
        await growth.forecast(sessions, None, settings, user.id, NOW, strategy="MARTINGALE", horizon_days=30, paths=1000)
    row = await growth.forecast(sessions, None, settings, user.id, NOW, strategy="QUARTER_KELLY", horizon_days=30, paths=1000, seed=11)
    view = growth.simulation_view(row)
    assert view["paths"] == 1000 and view["trades"] == math.floor(40 / 30 * 30) and view["starting_bankroll_inr"] == "100000.00" and view["seed"] == "11"
    assert view["parameters"]["history"]["bets"] == 40 and view["parameters"]["skill_bss"] is None and view["developer_credit"] == "Amit Ashok Kumar Patnaik"
    assert len(view["percentile_curve"]) >= 2 and 0 <= view["prob_circuit_breaker"] <= 1
    again = await growth.forecast(sessions, None, settings, user.id, NOW, strategy="QUARTER_KELLY", horizon_days=30, paths=1000, seed=11)
    assert again.median_ending_bankroll_inr == row.median_ending_bankroll_inr
    broke = await make_user(sessions, bankroll=None)
    await seed_bets(sessions, broke.id, 30)
    with pytest.raises(ValueError):
        await growth.forecast(sessions, None, settings, broke.id, NOW, strategy="QUARTER_KELLY", horizon_days=30, paths=1000)


@pytest.mark.asyncio
async def test_the_regime_its_advisories_and_the_latch(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    user = await make_user(sessions)
    steps = []
    t = NOW
    for drawdown in (0.05, 0.06, 0.12, 0.17, 0.22, 0.05):
        t += timedelta(minutes=5)
        state = await growth.observe_regime(sessions, None, settings, user.id, drawdown, D("100000"), t)
        steps.append((state.regime.name, state.latched, None if state.advisory is None else (state.advisory.insight_code, state.advisory.severity)))
    assert steps == [
        ("OPTIMAL_GROWTH", False, ("OPTIMAL_GROWTH_TRAJECTORY", "INFO")),
        ("OPTIMAL_GROWTH", False, None),  # same regime: nothing new
        ("CAUTIOUS_THROTTLED", False, ("VARIANCE_THROTTLE", "RECOMMENDATION")),
        ("DEFENSIVE_CAPITAL_PRESERVATION", False, ("VARIANCE_THROTTLE", "WARNING")),
        ("CIRCUIT_BREAKER_HALT", True, ("CAPITAL_PRESERVATION_HALT", "CRITICAL")),
        ("LATCHED_HALT", True, None),  # recovered to 5%, still halted: nobody signed it off
    ]
    async with sessions() as session:
        halt = (await session.execute(select(CFOAdvisoryLog).where(CFOAdvisoryLog.insight_code == "CAPITAL_PRESERVATION_HALT"))).scalar_one()
        assert "₹100,000" in halt.message and halt.metrics_snapshot["damper"] == 0.0 and halt.developer_credit == "Amit Ashok Kumar Patnaik"
        throttle = (await session.execute(select(CFOAdvisoryLog).where(CFOAdvisoryLog.regime == "DEFENSIVE_CAPITAL_PRESERVATION"))).scalar_one()
        assert throttle.metrics_snapshot["stake_ceiling_inr"] == "625.00"  # 2.5% x 0.25 of ₹100,000
        await growth.acknowledge(session, halt, user.id, "losses reviewed; resume", t)
        await session.commit()
    resumed = await growth.observe_regime(sessions, None, settings, user.id, 0.05, D("100000"), t + timedelta(minutes=5))
    assert resumed.regime.name == "OPTIMAL_GROWTH" and not resumed.latched and resumed.advisory is not None
    # the scan re-confirms a steady regime at most every CFO_ADVISORY_CONFIRM_HOURS, with the ledger's own figures
    await seed_bets(sessions, user.id, 4, days=2)
    later = t + timedelta(hours=settings.CFO_ADVISORY_CONFIRM_HOURS + 1)
    report = await growth.scan(sessions, None, settings, later)
    assert report == {"users": 1, "advisories": 1, "halted": 0}
    async with sessions() as session:
        latest = (await session.execute(select(CFOAdvisoryLog).order_by(CFOAdvisoryLog.created_at.desc()).limit(1))).scalar_one()
    assert latest.title == "Steady growth, re-confirmed" and "bets settled" in latest.message
    assert (await growth.scan(sessions, None, settings, later + timedelta(minutes=15)))["advisories"] == 0


@pytest.mark.asyncio
async def test_the_skill_term_follows_pillar_1s_weights(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with sessions() as session:
        assert await growth.fleet_skill(session, {}) is None  # nothing measured: psi stays 1
        run = ModelRecalibrationRun(id=uuid.uuid4(), trigger_type="SCHEDULED", models_evaluated=3, models_promoted=0, models_demoted=0, benchmark_model="closing_sharp",
                                    published=True, published_weights={}, parameters={}, developer_credit="x", created_at=NOW)
        session.add(run)
        for name, bss in (("poisson", 0.10), ("market", -0.05), ("elo", None)):
            session.add(ModelWeightAudit(id=uuid.uuid4(), run_id=run.id, model_name=name, sample_count=40, sample_count_30d=20, paired_count=40, brier_skill_score=bss,
                                         new_weight=1.0, status="ACTIVE", status_reason="x", metrics_snapshot={}, created_at=NOW))
        await session.commit()
        assert await growth.fleet_skill(session, {"poisson": 2.0, "market": 1.0}) == pytest.approx((0.10 * 2 - 0.05) / 3)
        assert await growth.fleet_skill(session, {"poisson": 0.0, "market": 1.0}) == pytest.approx(-0.05)  # a benched model has no say


@pytest.mark.asyncio
async def test_the_rebalance_plan_from_the_vault_and_the_ledger(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    admin = await make_user(sessions)

    def account(book: str, balance: str | None, currency: str = "INR", reserved: str = "0") -> VaultBookmakerAccount:
        return VaultBookmakerAccount(id=uuid.uuid4(), bookmaker_id=book, label=book, identity_digest=uuid.uuid4().hex, secrets_fingerprint=uuid.uuid4().hex,
                                     currency=currency, balance=None if balance is None else D(balance), reserved=D(reserved))

    async with sessions() as session:
        session.add_all([account("parimatch", "60000", reserved="10000"), account("1xbet", "10000"), account("stake", None), account("pinnacle", "500", currency="USD")])
        await session.commit()
    await seed_bets(sessions, admin.id, 30, book="1XBET", odds="2.10", p=0.52, days=20)  # EV 30 x 1000 x 0.092, inside the 28-day lookback
    await seed_bets(sessions, admin.id, 10, book="PARIMATCH", odds="2.00", p=0.51, days=20)  # EV 10 x 1000 x 0.02
    async with sessions() as session:
        plan = await growth.rebalance_plan(session, None, settings, NOW)
    assert plan["venue_balances"] == {"1xbet": "10000.00", "parimatch": "50000.00"}  # free balance: less the reserved stakes
    assert set(plan["unpriced"]) == {"pinnacle", "stake"} and "missing" in plan["unpriced"]["pinnacle"]  # USD with no FX rate; no balance recorded
    ev = {"1xbet": 30 * 1000 * (0.52 * 2.1 - 1), "parimatch": 10 * 1000 * (0.51 * 2.0 - 1)}
    assert plan["ev_flow_inr"] == {k: pytest.approx(v, abs=0.01) for k, v in ev.items()}
    share = ev["1xbet"] ** 0.8 / (ev["1xbet"] ** 0.8 + ev["parimatch"] ** 0.8)
    assert Decimal(plan["target_allocations"]["1xbet"]) == (D("60000") * D(str(share))).quantize(D("0.01"), rounding="ROUND_DOWN")
    assert [t["source_venue"] + ">" + t["destination_venue"] for t in plan["transfers"]] == ["parimatch>1xbet"]

    first, rows = await growth.record_plan(sessions, None, settings, admin.id, NOW)
    assert len(rows) == 1 and rows[0].status == "PENDING" and "withdraw, then deposit" in rows[0].reason and rows[0].developer_credit == "Amit Ashok Kumar Patnaik"
    second, again = await growth.record_plan(sessions, None, settings, admin.id, NOW + timedelta(minutes=1))
    async with sessions() as session:
        old = await session.get(CFORebalanceRecommendation, rows[0].id)
        assert old is not None and old.status == "SUPERSEDED"
        row = await session.get(CFORebalanceRecommendation, again[0].id)
        await growth.set_transfer_status(session, row, RebalanceStatus.EXECUTED, admin.id, "moved at both books", NOW)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await growth.set_transfer_status(session, row, RebalanceStatus.DISMISSED, admin.id, None, NOW)  # type: ignore[arg-type]
        await session.commit()
        notices = (await session.execute(select(func.count()).select_from(CFOAdvisoryLog).where(CFOAdvisoryLog.insight_code == InsightCode.VENUE_REBALANCE.value))).scalar_one()
    assert notices == 2 and first["plan_id"] != second["plan_id"]


# ================================================================ pillar 13 and the API
def test_pillar_13_reads_the_latch_and_the_skill(settings: Settings) -> None:
    from tests.test_ultra_vetting import inputs  # noqa: PLC0415

    rule = fortress.FortressPolicy.from_settings(settings)
    plain = fortress.run(inputs(max_stake=D("50000")), rule, ("pinnacle",))
    p13 = plain.pillars[12]
    assert p13.status.value == "PASS" and p13.metrics["regime"] == "OPTIMAL_GROWTH" and p13.metrics["skill_multiplier"] == 1.0
    skilled = fortress.run(dataclasses.replace(inputs(max_stake=D("50000")), skill=0.1), rule, ("pinnacle",)).pillars[12]
    assert skilled.metrics["fraction"] == pytest.approx(min(p13.metrics["fraction"] * 1.2, 0.025), rel=1e-3) and "skill x1.20" in skilled.reason
    latched = fortress.run(dataclasses.replace(inputs(max_stake=D("50000")), halt_latched=True), rule, ("pinnacle",))
    assert latched.pillars[12].status.value == "FAIL" and "sign-off" in latched.pillars[12].reason and not latched.is_vetted and latched.sizing.stake == 0  # type: ignore[union-attr]


def app_for(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = FastAPI()
    app.include_router(growth_api.router, prefix="/api/v1")
    app.state.redis = redis
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_admin] = lambda: user
    return app


@pytest.mark.asyncio
async def test_the_api(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    admin = await make_user(sessions)
    quant = await make_user(sessions, role="QUANT")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, None, settings, admin)), base_url="http://test") as client:
        rules = (await client.get("/api/v1/the-vault/cfo/growth/policy")).json()
        assert rules["regime"] == "OPTIMAL_GROWTH" and rules["stake_ceiling_inr"] == "2500.00" and rules["active_strategy"] == "QUARTER_KELLY"
        assert rules["ruin_bound_halving"] == pytest.approx(0.0078125, abs=1e-6) and rules["developer_credit"] == "Amit Ashok Kumar Patnaik"
        refused = await client.get("/api/v1/the-vault/cfo/growth/strategies")
        assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "INSUFFICIENT_HISTORY"
        await seed_bets(sessions, admin.id, 45)
        board = (await client.get("/api/v1/the-vault/cfo/growth/strategies")).json()
        codes = [s["strategy_code"] for s in board["strategies"]]
        assert codes == ["FULL_KELLY", "HALF_KELLY", "QUARTER_KELLY", "FIXED_FRACTION_1PCT", "FIXED_FRACTION_2PCT"] and board["history"]["bets"] == 45
        quarter = next(s for s in board["strategies"] if s["strategy_code"] == "QUARTER_KELLY")
        assert quarter["ruin_bound_halving"] == pytest.approx(0.0078125, abs=1e-6) and quarter["effective_fraction"] <= 0.025
        assert quarter["recommended_stake_on_next_bet_inr"] == str(gm.round_stake(D("100000"), quarter["effective_fraction"], settings.TWIN_STAKE_STEP_INR))
        bad = await client.post("/api/v1/the-vault/cfo/growth/simulate", json={"strategy": "QUARTER_KELLY", "horizon_days": 45, "paths": 1000})
        assert bad.status_code == 422
        run = await client.post("/api/v1/the-vault/cfo/growth/simulate", json={"strategy": "HALF_KELLY", "horizon_days": 30, "paths": 1000})
        assert run.status_code == 201 and run.json()["strategy"] == "HALF_KELLY" and run.json()["percentile_curve"][0]["p50"] == 100000.0
        assert [s["id"] for s in (await client.get("/api/v1/the-vault/cfo/growth/simulations")).json()] == [run.json()["id"]]

        scanned = (await client.post("/api/v1/the-vault/cfo/growth/advisories/scan")).json()
        assert scanned["regime"] == "OPTIMAL_GROWTH" and scanned["advisory"]["insight_code"] == "OPTIMAL_GROWTH_TRAJECTORY"
        await growth.observe_regime(sessions, None, settings, admin.id, 0.25, D("75000"), datetime.now(UTC) + timedelta(minutes=1))  # after the scan above, on the real clock
        listed = (await client.get("/api/v1/the-vault/cfo/growth/advisories")).json()
        halt = listed[0]
        assert halt["insight_code"] == "CAPITAL_PRESERVATION_HALT" and halt["severity"] == "CRITICAL"
        assert (await client.post(f"/api/v1/the-vault/cfo/growth/advisories/{halt['id']}/ack", json={})).json()["detail"]["reason"] == "SIGN_OFF_NOTE"
        signed = (await client.post(f"/api/v1/the-vault/cfo/growth/advisories/{halt['id']}/ack", json={"note": "reviewed with the desk"})).json()
        assert signed["is_acknowledged"] and signed["signed_off"] == 1 and signed["acknowledgement_note"] == "reviewed with the desk"
        assert (await client.post(f"/api/v1/the-vault/cfo/growth/advisories/{halt['id']}/ack", json={})).status_code == 409
        plan = (await client.get("/api/v1/the-vault/cfo/growth/rebalance")).json()
        assert plan["transfers"] == [] and "no venue balance" in plan["reason"] and plan["developer_credit"] == "Amit Ashok Kumar Patnaik"
    # a QUANT cannot sign a halt off, and cannot see another user's advisories
    await growth.observe_regime(sessions, None, settings, quant.id, 0.30, D("10000"), NOW)
    async with sessions() as session:
        quant_halt = (await session.execute(select(CFOAdvisoryLog).where(CFOAdvisoryLog.user_id == quant.id))).scalar_one()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, None, settings, quant)), base_url="http://test") as client:
        denied = await client.post(f"/api/v1/the-vault/cfo/growth/advisories/{quant_halt.id}/ack", json={"note": "let me trade"})
        assert denied.status_code == 403 and denied.json()["detail"]["reason"] == "SIGN_OFF_REQUIRED"
        assert (await client.post(f"/api/v1/the-vault/cfo/growth/advisories/{halt['id']}/ack", json={})).status_code == 404


@pytest.mark.asyncio
async def test_a_change_of_regime_pages_and_a_reconfirmation_does_not(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> None:
    user = await make_user(sessions)
    for minutes, drawdown in ((0, 0.02), (5, 0.12), (8, 0.16), (10, 0.21)):
        await growth.observe_regime(sessions, redis, settings, user.id, drawdown, D("100000"), NOW + timedelta(minutes=minutes))
    await growth.scan(sessions, redis, settings, NOW + timedelta(hours=settings.CFO_ADVISORY_CONFIRM_HOURS + 1))
    alerts = [decode(fields["a"]) for _, fields in await redis.xrange(SentinelKeys(settings).stream)]
    regime = [a.severity.value for a in alerts if a.kind is AlertKind.CFO_REGIME_CHANGE]
    # cautious is a recommendation (INFO), defensive a WARNING, the halt CRITICAL; the first steady reading and the latch's scan stay quiet
    assert regime == ["INFO", "WARNING", "CRITICAL"]
