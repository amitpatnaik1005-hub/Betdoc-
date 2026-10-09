"""Group 65: The Hive's autonomous trading bots.

The brief's two proofs come first: Bot B betting against Bot A's open position is blocked (a wash
trade), and a ₹50,000 order with slicing on becomes 5 Celery tasks with jittered countdowns. Then
the rest of the zero-loophole list: the registry seed, pipeline validation, the thundering-herd
merge, sub-account isolation, the velocity / drawdown / flash-crash breakers, the master halt,
shadow mode, liquidity gates, TWAP slices re-checking the edge, the API and the stream worker.

The ledger runs on SQLite, and again on PostgreSQL when ``TEST_POSTGRES_URL`` is set. Redis is a
real server on a dedicated, flushed index (skipped when none is reachable).
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import Settings, get_settings
from app.models import BetLedger, ExchangeAccount, RiskMandate, User
from app.models.cfo_vault import AccountFunding, AuditEvent, AuditLog, BankrollAccount, LedgerEntry, MarketResult, PhantomLedger, RiskGuardSettings
from app.models.control_panel import SystemSettingsModel
from app.models.hive_bots import (
    BotExecutionMode,
    BotStatus,
    HiveBotEvent,
    HiveEventKind,
    HiveOrderPlan,
    HiveShadowPosition,
    PlanStatus,
    ShadowStatus,
    TradingBot,
)
from app.models.the_core import ComponentKind, SmallcaseRegistryModel
from app.schemas.aryabhata import EdgeSignal
from app.services import cfo_ledger as ledger
from app.services.bookmaker_gateway import PaperBookmaker
from app.services.cfo_ledger import CfoError, InsufficientFundsError, OrderTicket, allocate, deallocate, lock_bankroll, reserve, verify_account
from app.services.hive_engine import HiveEngine, HiveKeys, Proposal, read_halt, set_halt, slice_countdowns, slice_stake
from app.services.hive_pipeline import LIVE_COMPONENTS, validate_pipeline
from app.services.hive_registry import CATALOGUE, parse_blueprint, reconcile, registry_components, seed_registry
from app.workers import hive_worker
from app.workers.hive_worker import CelerySliceScheduler, HiveWorker, gateways

D = Decimal
TABLES = [
    User.__table__,
    ExchangeAccount.__table__,
    RiskMandate.__table__,
    BetLedger.__table__,
    SystemSettingsModel.__table__,
    TradingBot.__table__,
    BankrollAccount.__table__,
    PhantomLedger.__table__,
    LedgerEntry.__table__,
    AuditLog.__table__,
    RiskGuardSettings.__table__,
    MarketResult.__table__,
    HiveBotEvent.__table__,
    HiveOrderPlan.__table__,
    HiveShadowPosition.__table__,
    SmallcaseRegistryModel.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_SENTINEL = "betdoc:test-sentinel"
FIXTURE = "fx-ars-lee"
BANK = D("1000000.00")
PIPELINE = {"math_models": ["math.consensus", "math.kelly_criterion"], "risk_models": [], "target_bet_types": ["bet.match_winner_1x2", "bet.single"]}


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
        if await client.dbsize() and not await client.exists(_SENTINEL) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):  # claimed by tests/conftest.py
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_SENTINEL, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "starting_bankroll": float(BANK),
            "CFO_KILL_SWITCH_KEY": "test:kill_switch",
            "CFO_STREAK_KEY_PREFIX": "test:risk:streak",
            "CFO_IDEMPOTENCY_KEY_PREFIX": "test:idempotency",
            "ARYABHATA_PREFIX": "test_arya",
            "SNIPER_PREFIX": "test_sniper",
            "PORTFOLIO_CHANNEL_PREFIX": "test:live_portfolio",
            "LIVE_ODDS_CHANNEL": "test:live_odds",
            "HIVE_PREFIX": "test_hive",
            "CFO_EXECUTION_MODE": "paper",
        }
    )


@pytest_asyncio.fixture
async def owner(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> uuid.UUID:
    async with sessions() as session:
        user = User(username=f"hive_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(user)
        await session.commit()
        await lock_bankroll(session, user.id, settings)  # the ₹10 lakh main account
        await session.commit()
        await seed_registry(session)
        await session.commit()
        return user.id


async def make_bot(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, owner: uuid.UUID, *, name: str, capital: str = "100000.00",
    mode: BotExecutionMode = BotExecutionMode.PAPER_TRADE, **fields: Any,
) -> TradingBot:
    now = datetime.now(UTC)
    bot = TradingBot(
        id=uuid.uuid4(), user_id=owner, name=name, description="", execution_mode=mode, status=BotStatus.ACTIVE, allocated_capital=D(capital),
        pipeline_updated_at=now, created_at=now, updated_at=now, **{**PIPELINE, **fields},
    )
    async with sessions() as session:
        session.add(bot)
        await session.commit()
        if mode is not BotExecutionMode.SHADOW_MODE and D(capital) > 0:
            funding = AccountFunding.TRANSFER if mode is BotExecutionMode.LIVE_EXECUTION else AccountFunding.VIRTUAL
            await allocate(session, settings, user_id=owner, bot_id=bot.id, amount=D(capital), funding=funding)
            await session.commit()
    return bot


async def put_books(redis: Redis, settings: Settings, fixture: str = FIXTURE, prices: tuple[str, str, str] = ("2.00", "3.50", "3.80"), n: int = 3) -> None:
    """``n`` books quoting the same 1X2 market: Shin consensus HOME is about 0.476."""
    for i in range(n):
        value = json.dumps({"s": "odds_api", "b": f"book{i}", "p": dict(zip(("HOME", "DRAW", "AWAY"), prices, strict=True)), "t": time.time() - 2, "x": False})
        await redis.hset(f"{settings.ARYABHATA_PREFIX}:books:{fixture}|Match Odds", f"odds_api|book{i}", value)


def edge(selection: str = "HOME", odds: str = "2.30", fixture: str = FIXTURE, bookmaker: str = "book9") -> EdgeSignal:
    now = datetime.now(UTC)
    return EdgeSignal(
        signal_id=uuid.uuid4(), fixture_id=fixture, market_id=uuid.uuid4(), market_type="Match Odds", selection=selection, home_team="Arsenal",
        away_team="Leeds", commence_time=now + timedelta(hours=3), bookmaker_id=bookmaker, source="odds_api", odds=D(odds), true_prob=D("0.47"),
        ev=D("0.08"), ev_percent=D("8"), full_kelly=D("0.06"), devig_method="shin", overround=D("0.05"), books=3, timestamp=now,
        expires_at=now + timedelta(seconds=15),
    )


class Recorder:
    """A slice scheduler that remembers what it was asked to queue."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, int, int]] = []

    def schedule(self, plan_id: uuid.UUID, index: int, countdown: int) -> None:
        self.calls.append((plan_id, index, countdown))


def engine_for(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, scheduler: Any = None) -> HiveEngine:
    return HiveEngine(redis, sessions, settings, gateways(settings, None), scheduler or Recorder(), rng=random.Random(65))


async def rows(sessions: async_sessionmaker[AsyncSession], model: Any, *where: Any) -> list[Any]:
    async with sessions() as session:
        return list((await session.execute(select(model).where(*where))).scalars().all())


async def refreshed(sessions: async_sessionmaker[AsyncSession], bot: TradingBot) -> TradingBot:
    async with sessions() as session:
        found = await session.get(TradingBot, bot.id)
        assert found is not None
        return found


# ================================================================ the brief's two proofs
@pytest.mark.asyncio
async def test_bot_b_betting_against_bot_a_is_blocked_as_a_wash_trade(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID
) -> None:
    """Bot A holds HOME on Arsenal v Leeds. Bot B's model likes AWAY: firing it would pay the vig on
    both sides of the owner's own book. Blocked, nothing reserved, the reason logged."""
    await put_books(redis, settings)
    bot_a = await make_bot(sessions, settings, owner, name="Bot A")
    hive = engine_for(sessions, redis, settings)
    fired = await hive.process(edge("HOME"), [bot_a])
    assert [(d.event, d.reason) for d in fired] == [(HiveEventKind.FIRED, "EXECUTED")]

    bot_b = await make_bot(sessions, settings, owner, name="Bot B")
    blocked = await hive.process(edge("AWAY", odds="4.40"), [bot_b])
    assert [(d.bot_id, d.event, d.reason) for d in blocked] == [(bot_b.id, HiveEventKind.BLOCKED, "WASH_TRADE")]
    assert await rows(sessions, PhantomLedger, PhantomLedger.bot_id == bot_b.id) == []
    (event,) = await rows(sessions, HiveBotEvent, HiveBotEvent.bot_id == bot_b.id, HiveBotEvent.reason == "WASH_TRADE")
    a_bet = (await rows(sessions, PhantomLedger, PhantomLedger.bot_id == bot_a.id))[0]
    assert event.detail["opposing_ledger_id"] == str(a_bet.id) and event.detail["opposing_selection"] == "HOME"
    async with sessions() as session:
        account = await ledger.read_account(session, owner, bot_b.id)
        assert account is not None and account.exposure_balance == 0  # not a rupee moved


@pytest.mark.asyncio
async def test_a_50000_stake_is_sliced_into_five_jittered_celery_tasks(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID
) -> None:
    """Slicing on, ₹10,000 slices: a ₹50,000 order becomes 5 discrete ``hive.fire_slice`` Celery
    tasks. The slices sum to exactly ₹50,000, each within 15% of an equal split; the first goes now,
    each next one 60-120s after the last."""
    bot = await make_bot(sessions, settings, owner, name="Slicer", capital="1000000.00", enable_order_slicing=True, slice_size_inr=D("10000.00"))
    hive = engine_for(sessions, redis, settings, CelerySliceScheduler())
    proposal = Proposal(bot.id, D("0.476"), D("0.0948"), D("50000.00"), D("50000.00"), {"math.consensus": "0.476"}, ())
    with patch.object(hive_worker.fire_slice, "apply_async") as queued:
        decision = await hive._plan(bot, proposal, edge("HOME"))
    assert decision.event is HiveEventKind.SLICED and queued.call_count == 5
    countdowns = [call.kwargs["countdown"] for call in queued.call_args_list]
    assert [call.kwargs["args"][1] for call in queued.call_args_list] == [0, 1, 2, 3, 4]
    assert countdowns[0] == 0 and all(60 <= b - a <= 120 for a, b in zip(countdowns, countdowns[1:]))
    assert len(set(b - a for a, b in zip(countdowns, countdowns[1:]))) > 1  # jittered, not a fixed beat
    (plan,) = await rows(sessions, HiveOrderPlan, HiveOrderPlan.bot_id == bot.id)
    stakes = [D(s["stake_inr"]) for s in plan.slices]
    assert sum(stakes) == D("50000.00") and len(set(stakes)) > 1
    assert all(D("8500") <= s <= D("11500") for s in stakes)
    assert all(call.kwargs["args"][0] == str(plan.id) for call in queued.call_args_list)


def test_slicing_maths() -> None:
    rng = random.Random(1)
    for total in ("50000.00", "12345.67", "10000.01", "999.99"):
        parts = slice_stake(D(total), D("10000"), 15, rng)
        assert sum(parts) == D(total) and all(p > 0 and p == p.quantize(D("0.01")) for p in parts)
    assert slice_stake(D("10000"), D("10000"), 15, rng) == [D("10000")]
    assert slice_countdowns(1, 60, 120, rng) == [0]


# ================================================================ the registry
@pytest.mark.asyncio
async def test_the_registry_seeds_every_component_once(sessions: async_sessionmaker[AsyncSession], owner: uuid.UUID) -> None:  # noqa: ARG001
    async with sessions() as session:
        again = await seed_registry(session)  # the fixture already seeded it: this run only updates
        await session.commit()
        components = await registry_components(session)
        pipelines = await session.scalar(select(func.count()).select_from(SmallcaseRegistryModel).where(SmallcaseRegistryModel.component_kind == ComponentKind.PIPELINE))
    assert again.inserted == 0 and again.updated == len(CATALOGUE)
    kinds = {k: sum(1 for c in components.values() if str(c.component_kind) == k) for k in ("MATH_MODEL", "RISK_MODEL", "BET_TYPE")}
    assert kinds == {"MATH_MODEL": 59, "RISK_MODEL": 24, "BET_TYPE": 28} and pipelines == 0
    assert {k for k, c in components.items() if c.live_capable} == {c.key for c in CATALOGUE if c.live_capable}
    assert {c.key for c in CATALOGUE if c.live_capable} <= LIVE_COMPONENTS  # every "live" component has an adapter


def test_blueprint_documents_are_parsed_and_reconciled() -> None:
    doc = """# BetDoc deep dive
## 1. The 59 Math Models
1. **Poisson** — goal rates
2. Dixon-Coles: low-score correction
- Kalman Filter (line tracking)
- Quantum Annealing Predictor
## Risk Models
| # | Model | Purpose |
|---|-------|---------|
| 1 | CVaR | tail |
| 2 | Value at Risk | tail |
## Bet Types
* Match Winner (1X2)
* Lucky 15
## Appendix
- not a component
"""
    found = parse_blueprint(doc)
    assert found[ComponentKind.MATH_MODEL] == ["Poisson", "Dixon-Coles", "Kalman Filter", "Quantum Annealing Predictor"]
    assert found[ComponentKind.RISK_MODEL] == ["CVaR", "Value at Risk"]
    assert found[ComponentKind.BET_TYPE] == ["Match Winner", "Lucky 15"]
    rec = reconcile(found)
    assert dict(rec.matched[ComponentKind.MATH_MODEL]) == {"Poisson": "math.poisson", "Dixon-Coles": "math.dixon_coles", "Kalman Filter": "math.kalman_filter"}
    assert rec.documented_only[ComponentKind.MATH_MODEL] == ["Quantum Annealing Predictor"]
    assert dict(rec.matched[ComponentKind.RISK_MODEL]) == {"CVaR": "risk.cvar", "Value at Risk": "risk.var"}


@pytest.mark.asyncio
async def test_a_pipeline_must_be_runnable_live(sessions: async_sessionmaker[AsyncSession], owner: uuid.UUID) -> None:  # noqa: ARG001
    async with sessions() as session:
        registry = await registry_components(session)
    assert validate_pipeline(PIPELINE["math_models"], ["risk.cvar", "risk.drawdown"], PIPELINE["target_bet_types"], registry) == []
    problems = validate_pipeline(["math.neural_net", "math.consensus"], ["risk.black_litterman"], ["bet.parlay"], registry)
    assert any("math.neural_net: backtest only" in p for p in problems) and any("risk.black_litterman: backtest only" in p for p in problems)
    assert any("Kelly staking" in p for p in problems) and any("target at least one market" in p for p in problems)
    assert any("not in the model registry" in p for p in validate_pipeline(["math.made_up", "math.kelly_criterion"], [], ["bet.match_winner_1x2"], registry))
    assert any("is a RISK_MODEL" in p for p in validate_pipeline(["risk.cvar", "math.kelly_criterion"], [], ["bet.match_winner_1x2"], registry))


# ================================================================ thundering herd, isolation
@pytest.mark.asyncio
async def test_matching_bots_merge_into_one_bet_sized_by_the_strongest(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID
) -> None:
    await put_books(redis, settings)
    timid = await make_bot(sessions, settings, owner, name="Timid", kelly_multiplier=D("0.1"))
    bold = await make_bot(sessions, settings, owner, name="Bold", kelly_multiplier=D("0.5"))
    middle = await make_bot(sessions, settings, owner, name="Middle", kelly_multiplier=D("0.25"))
    hive = engine_for(sessions, redis, settings)
    signal = edge("HOME")
    decisions = await hive.process(signal, [timid, bold, middle])
    by_bot = {d.bot_id: d for d in decisions}
    assert by_bot[bold.id].event is HiveEventKind.FIRED
    assert by_bot[timid.id].event is by_bot[middle.id].event is HiveEventKind.MERGED
    bets = await rows(sessions, PhantomLedger, PhantomLedger.fixture_id == FIXTURE)
    assert len(bets) == 1 and bets[0].bot_id == bold.id and bets[0].stake_inr == by_bot[bold.id].stake
    assert by_bot[bold.id].stake > by_bot[middle.id].stake > by_bot[timid.id].stake
    # The same edge again (the next frame): every bot that matched is on cooldown for this selection
    assert await hive.process(edge("HOME"), [timid, bold, middle]) == []
    assert len(await rows(sessions, PhantomLedger, PhantomLedger.fixture_id == FIXTURE)) == 1


@pytest.mark.asyncio
async def test_a_bot_can_lose_only_its_own_allocation(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:  # noqa: ARG001
    """A paper bot's ₹10,000 is virtual: the main account never moves. A live bot's ₹20,000 is
    transferred out of it and back. Neither can reserve past its sub-account, and every journal
    (main, each bot) still sums to zero."""
    paper = await make_bot(sessions, settings, owner, name="Paper", capital="10000.00")
    live = await make_bot(sessions, settings, owner, name="Live", capital="20000.00", mode=BotExecutionMode.LIVE_EXECUTION)
    async with sessions() as session:
        main = await ledger.read_account(session, owner)
        assert main is not None and main.available_balance == BANK - D("20000.00") and main.peak_balance == BANK - D("20000.00")
        sub = await lock_bankroll(session, owner, settings, bot_id=paper.id)
        assert (sub.funding, sub.available_balance) == ("VIRTUAL", D("10000.00"))
        ticket = OrderTicket(owner, uuid.uuid4(), FIXTURE, "Match Odds", "HOME", "book9", D("10000.01"), D("2.30"), bot_id=paper.id)
        with pytest.raises(InsufficientFundsError):
            await reserve(session, sub, ticket)
        await session.rollback()
        sub = await lock_bankroll(session, owner, settings, bot_id=paper.id)
        entry = await reserve(session, sub, OrderTicket(owner, uuid.uuid4(), FIXTURE, "Match Odds", "HOME", "book9", D("10000.00"), D("2.30"), bot_id=paper.id))
        ledger.settle(session, sub, entry, won=False)  # the worst case: everything it had, lost
        await session.commit()
    async with sessions() as session:
        main = await ledger.read_account(session, owner)
        sub = await ledger.read_account(session, owner, paper.id)
        assert sub is not None and sub.equity == 0 and main is not None and main.available_balance == BANK - D("20000.00")
        for bot_id in (None, paper.id, live.id):
            await verify_account(session, owner, bot_id)
        await deallocate(session, settings, user_id=owner, bot_id=live.id, amount=D("20000.00"))
        await session.commit()
        main = await ledger.read_account(session, owner)
        assert main is not None and main.available_balance == BANK and main.peak_balance == BANK
        with pytest.raises(CfoError) as wrong:
            await lock_bankroll(session, owner, settings, bot_id=uuid.uuid4())
        assert wrong.value.reason == "NO_SUB_ACCOUNT"


@pytest.mark.asyncio
async def test_an_order_reserves_only_in_its_own_account(sessions: async_sessionmaker[AsyncSession], settings: Settings, owner: uuid.UUID) -> None:
    bot = await make_bot(sessions, settings, owner, name="Own", capital="5000.00")
    async with sessions() as session:
        main = await lock_bankroll(session, owner, settings)
        with pytest.raises(ledger.LedgerInvariantError):
            await reserve(session, main, OrderTicket(owner, uuid.uuid4(), FIXTURE, "Match Odds", "HOME", "b", D("10"), D("2"), bot_id=bot.id))


# ================================================================ breakers, halt
@pytest.mark.asyncio
async def test_the_velocity_breaker_suspends_a_bot(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    bot = await make_bot(sessions, settings, owner, name="Fast", max_bets_per_minute=3)
    hive = engine_for(sessions, redis, settings)
    for i in range(3):
        await put_books(redis, settings, fixture=f"fx-{i}")
        assert (await hive.process(edge("HOME", fixture=f"fx-{i}"), [bot]))[0].event is HiveEventKind.FIRED
    await put_books(redis, settings, fixture="fx-3")
    (fourth,) = await hive.process(edge("HOME", fixture="fx-3"), [bot])
    assert (fourth.event, fourth.reason) == (HiveEventKind.BLOCKED, "VELOCITY_BREAKER")
    stored = await refreshed(sessions, bot)
    assert (stored.status, stored.suspended_reason) == (BotStatus.SUSPENDED, "VELOCITY_BREAKER")
    assert len(await rows(sessions, PhantomLedger, PhantomLedger.bot_id == bot.id)) == 3


@pytest.mark.asyncio
async def test_the_drawdown_breaker_suspends_a_losing_bot(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    """₹10,000 sub-bankroll, 20% limit: a ₹2,500 loss settled in the last 24h trips it."""
    bot = await make_bot(sessions, settings, owner, name="Loser", capital="10000.00", drawdown_limit_pct=D("20"))
    async with sessions() as session:
        sub = await lock_bankroll(session, owner, settings, bot_id=bot.id)
        entry = await reserve(session, sub, OrderTicket(owner, uuid.uuid4(), "fx-old", "Match Odds", "HOME", "b", D("2500.00"), D("2.0"), bot_id=bot.id))
        pnl = ledger.settle(session, sub, entry, won=False)
        session.add(ledger.audit_row(AuditEvent.SETTLED, "SETTLED_LOST", user_id=owner, ledger_id=entry.id, pnl_inr=pnl))
        await session.commit()
    hive = engine_for(sessions, redis, settings)
    assert await hive.sweep_breakers() == [bot.id]
    stored = await refreshed(sessions, bot)
    assert (stored.status, stored.suspended_reason) == (BotStatus.SUSPENDED, "DRAWDOWN_BREAKER")
    (event,) = await rows(sessions, HiveBotEvent, HiveBotEvent.bot_id == bot.id, HiveBotEvent.event == HiveEventKind.SUSPENDED)
    assert event.detail["drop_pct"] == "25.00"


@pytest.mark.asyncio
async def test_a_flash_crash_halts_every_bot(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    """The consensus probability of one selection goes 0.40 -> 0.47 in 90 seconds (+17.5%): every
    bot stops until a person resumes them."""
    bot = await make_bot(sessions, settings, owner, name="Calm")
    await put_books(redis, settings)
    now = time.time()
    cell = f"{FIXTURE}|Match Odds|HOME"
    await redis.zadd(f"{settings.LIVE_ODDS_CHANNEL}:board:ts", {cell: now})
    for i, prob in enumerate((0.40, 0.41, 0.44, 0.47)):
        at = now - 90 + i * 30
        await redis.zadd(f"{settings.LIVE_ODDS_CHANNEL}:hist:{cell}", {f"{at:.3f}|2.3|{prob}": at})
    hive = engine_for(sessions, redis, settings)
    flag = await hive.flash_crash_scan()
    assert flag is not None and flag["reason"] == "FLASH_CRASH" and flag["detail"]["market"] == cell and flag["detail"]["swing_pct"] == 17.5
    assert await hive.process(edge("HOME"), [bot]) == []
    assert await rows(sessions, PhantomLedger) == []
    assert (await rows(sessions, HiveBotEvent, HiveBotEvent.event == HiveEventKind.HALTED))[0].reason == "FLASH_CRASH"


@pytest.mark.asyncio
async def test_a_small_swing_is_not_a_crash(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:  # noqa: ARG001
    now = time.time()
    cell = f"{FIXTURE}|Match Odds|HOME"
    await redis.zadd(f"{settings.LIVE_ODDS_CHANNEL}:board:ts", {cell: now})
    for i, prob in enumerate((0.40, 0.42, 0.44)):  # +10%
        await redis.zadd(f"{settings.LIVE_ODDS_CHANNEL}:hist:{cell}", {f"{now - 60 + i:.3f}|2.3|{prob}": now - 60 + i})
    assert await engine_for(sessions, redis, settings).flash_crash_scan() is None
    assert await read_halt(redis, settings) is None


@pytest.mark.asyncio
async def test_the_master_halt_stops_everything(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    bot = await make_bot(sessions, settings, owner, name="Halted")
    await put_books(redis, settings)
    hive = engine_for(sessions, redis, settings)
    await set_halt(redis, settings, "MASTER_KILL_SWITCH", by="tester")
    assert await hive.process(edge("HOME"), [bot]) == []
    await redis.delete(HiveKeys(settings).halt)
    assert (await hive.process(edge("HOME"), [bot]))[0].event is HiveEventKind.FIRED


# ================================================================ modes, gates, slices
@pytest.mark.asyncio
async def test_shadow_mode_tracks_hypothetical_pnl_without_orders(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    bot = await make_bot(sessions, settings, owner, name="Ghost", mode=BotExecutionMode.SHADOW_MODE, capital="50000.00")
    await put_books(redis, settings)
    hive = engine_for(sessions, redis, settings)
    (decision,) = await hive.process(edge("HOME"), [bot])
    assert decision.event is HiveEventKind.SHADOW_FILLED
    assert await rows(sessions, PhantomLedger) == [] and await rows(sessions, BankrollAccount, BankrollAccount.bot_id == bot.id) == []
    async with sessions() as session:
        session.add(MarketResult(fixture_id=FIXTURE, market="Match Odds", winning_selection="HOME", is_void=False, source="test"))
        await session.commit()
    assert await hive.grade_shadow() == 1
    (position,) = await rows(sessions, HiveShadowPosition, HiveShadowPosition.bot_id == bot.id)
    assert position.status is ShadowStatus.WON and position.pnl_inr == (position.stake_inr * D("1.30")).quantize(D("0.01"))


@pytest.mark.asyncio
async def test_a_live_bot_never_fires_on_a_paper_platform(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    bot = await make_bot(sessions, settings, owner, name="Live", mode=BotExecutionMode.LIVE_EXECUTION, capital="50000.00")
    await put_books(redis, settings)
    (decision,) = await engine_for(sessions, redis, settings).process(edge("HOME"), [bot])
    assert (decision.event, decision.reason) == (HiveEventKind.BLOCKED, "LIVE_EXECUTION_UNAVAILABLE")
    assert await rows(sessions, PhantomLedger) == []


@pytest.mark.parametrize(
    ("fields", "books", "reason"),
    [({"min_quoting_books": 5}, 3, "LIQUIDITY_THIN"), ({"min_market_liquidity": D("50000")}, 3, "LIQUIDITY_UNREPORTED"), ({"min_edge_pct": D("20")}, 3, "EDGE_BELOW_MINIMUM")],
)
@pytest.mark.asyncio
async def test_ghost_lines_and_thin_edges_are_skipped(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID, fields: dict[str, Any], books: int, reason: str
) -> None:
    bot = await make_bot(sessions, settings, owner, name=f"Picky {reason}", **fields)
    await put_books(redis, settings, n=books)
    (decision,) = await engine_for(sessions, redis, settings).process(edge("HOME"), [bot])
    assert (decision.event, decision.reason) == (HiveEventKind.SKIPPED, reason)


@pytest.mark.asyncio
async def test_twap_slices_fire_only_while_the_edge_lives(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    bot = await make_bot(sessions, settings, owner, name="TWAP", enable_order_slicing=True, slice_size_inr=D("500.00"))
    await put_books(redis, settings)
    recorder = Recorder()
    hive = engine_for(sessions, redis, settings, recorder)
    signal = edge("HOME")
    (decision,) = await hive.process(signal, [bot])
    assert decision.event is HiveEventKind.SLICED and len(recorder.calls) >= 2
    plan_id = decision.plan_id
    assert plan_id is not None

    async def live(fixture: str, selection: str) -> EdgeSignal:  # noqa: ARG001
        return edge("HOME")

    async def gone(fixture: str, selection: str) -> None:  # noqa: ARG001
        return None

    first = await hive.run_slice(plan_id, 0, live)
    assert first is not None and first.event is HiveEventKind.SLICE_FIRED
    second = await hive.run_slice(plan_id, 1, gone)
    assert second is not None and (second.event, second.reason) == (HiveEventKind.SLICE_CANCELLED, "EDGE_GONE")
    (plan,) = await rows(sessions, HiveOrderPlan, HiveOrderPlan.id == plan_id)
    assert plan.slices[0]["status"] == "FIRED" and all(s["status"] == "CANCELLED" for s in plan.slices[1:]) and plan.status is PlanStatus.CANCELLED
    (bet,) = await rows(sessions, PhantomLedger, PhantomLedger.bot_id == bot.id)
    assert bet.group_id == plan_id and bet.strategy == "hive" and bet.stake_inr == D(plan.slices[0]["stake_inr"])


@pytest.mark.asyncio
async def test_coordinated_arbitrage_legs_are_not_wash_trades(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    """A Group 64 arbitrage leg on AWAY (main account, tagged) does not block a bot's HOME."""
    async with sessions() as session:
        main = await lock_bankroll(session, owner, settings)
        await reserve(session, main, OrderTicket(owner, uuid.uuid4(), FIXTURE, "Match Odds", "AWAY", "b", D("100"), D("4.0"), strategy="arbitrage", group_id=uuid.uuid4()))
        await session.commit()
    bot = await make_bot(sessions, settings, owner, name="Arb-safe")
    await put_books(redis, settings)
    (decision,) = await engine_for(sessions, redis, settings).process(edge("HOME"), [bot])
    assert decision.event is HiveEventKind.FIRED


# ================================================================ the stream worker and the API
@pytest.mark.asyncio
async def test_the_worker_decides_each_streamed_signal_once(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    bot = await make_bot(sessions, settings, owner, name="Streamer")
    await put_books(redis, settings)
    one = HiveWorker(redis, sessions, settings, gateways(settings, None), Recorder())
    two = HiveWorker(redis, sessions, settings, gateways(settings, None), Recorder())
    await one.ensure_group()
    await two.ensure_group()
    await redis.xadd(HiveKeys(settings).signals, {"e": edge("HOME").model_dump_json()})
    assert await one.step() + await two.step() == 1  # one consumer group: exactly one worker gets it
    assert len(await rows(sessions, PhantomLedger, PhantomLedger.bot_id == bot.id)) == 1


@pytest.mark.asyncio
async def test_hive_trading_api(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:
    from fastapi import FastAPI

    from app.api.deps import get_current_user
    from app.api.v1 import cfo_execution, hive_trading

    app = FastAPI()
    app.include_router(hive_trading.router, prefix="/api/v1")
    app.state.redis = redis
    async with sessions() as session:
        user = await session.get(User, owner)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[cfo_execution.get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        registry = (await client.get("/api/v1/hive/trading/registry")).json()
        assert registry["counts"] == {"BET_TYPE": 28, "MATH_MODEL": 59, "RISK_MODEL": 24} and registry["live_counts"]["MATH_MODEL"] == 10

        bad = await client.post("/api/v1/hive/trading/bots", json={"name": "Bad", "math_models": ["math.lstm", "math.kelly_criterion"]})
        assert bad.status_code == 422 and bad.json()["detail"]["reason"] == "INVALID_PIPELINE"
        created = await client.post("/api/v1/hive/trading/bots", json={"name": "Alpha", "risk_models": ["risk.drawdown", "risk.cvar"]})
        assert created.status_code == 201 and created.json()["status"] == "PAUSED" and created.json()["execution_mode"] == "PAPER_TRADE"
        bot_id = created.json()["id"]
        no_cash = await client.post(f"/api/v1/hive/trading/bots/{bot_id}/status", json={"status": "ACTIVE"})
        assert no_cash.status_code == 409 and no_cash.json()["detail"]["reason"] == "NO_CAPITAL"

        funded = await client.put(f"/api/v1/hive/trading/bots/{bot_id}/capital", json={"allocated_capital": "25000.00"})
        assert funded.status_code == 200 and funded.json()["account"]["equity"] == 25000.0 and funded.json()["account"]["funding"] == "VIRTUAL"
        active = await client.post(f"/api/v1/hive/trading/bots/{bot_id}/status", json={"status": "ACTIVE"})
        assert active.status_code == 200 and active.json()["status"] == "ACTIVE"
        switch = await client.patch(f"/api/v1/hive/trading/bots/{bot_id}", json={"execution_mode": "LIVE_EXECUTION"})
        assert switch.status_code == 409 and switch.json()["detail"]["reason"] == "PAUSE_FIRST"
        await client.post(f"/api/v1/hive/trading/bots/{bot_id}/status", json={"status": "PAUSED"})
        switch = await client.patch(f"/api/v1/hive/trading/bots/{bot_id}", json={"execution_mode": "LIVE_EXECUTION"})
        assert switch.status_code == 409 and switch.json()["detail"]["reason"] == "RELEASE_CAPITAL_FIRST"

        halted = await client.put("/api/v1/hive/trading/halt", json={"halted": True})
        assert halted.status_code == 200 and halted.json()["halted"] is True
        resume = await client.put("/api/v1/hive/trading/halt", json={"halted": False})
        assert resume.status_code == 403  # a QUANT can halt, only an admin resumes
        assert (await client.get("/api/v1/hive/trading/halt")).json()["reason"] == "MASTER_KILL_SWITCH"
        topology = (await client.get("/api/v1/hive/trading/topology")).json()
        assert topology["bots"][0]["name"] == "Alpha" and topology["markets"] == []
        events = (await client.get("/api/v1/hive/trading/events")).json()
        assert events[0]["reason"] == "CAPITAL_SET"
