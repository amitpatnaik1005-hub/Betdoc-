"""Group 71: the Smart Order Router and multi-venue execution slicer.

The brief's proofs:

* deadlock-free atomic reservation: two concurrent ₹10,000 orders on an account holding ₹15,000 end
  in exactly one fill and one clean rejection, never a deadlock (and two orders locking the same two
  accounts in opposite target orders both finish, because the lock order is the primary key's);
* cross-venue slicing: a ₹50,000 order over two venues is exactly two slices with deterministic
  ``{order_id}_{venue_id}_{slice_index}`` keys, and retrying it never holds a rupee twice;
* a sudden odds drop trips the slippage guard and releases every held reservation;
* two consecutive rejected slices at one venue trip its circuit breaker (pause, holds released,
  Sentinel CRITICAL);
* a partial fill flags a legged, hedge-eligible position in the Active Portfolio.

Then: the pure slicer, the commission-adjusted EV check, the deficit report, partial-fill shrinking,
unanswered slices staying held, the SHA-256 receipt mirrored into Nalanda's chain, manual release of
orphans, the sweep, the CFO executor's classification, and the API. SQLite (a file, so two
connections really contend) and PostgreSQL when ``TEST_POSTGRES_URL`` is set (real ``FOR UPDATE``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_current_admin
from app.api.v1 import execution_router as router_api
from app.core.config import Settings, get_settings
from app.core.security import get_password_hash
from app.models import User
from app.models.cfo_vault import LedgerStatus, PhantomLedger
from app.models.execution_router import RoutedOrder, RoutedSlice, SliceStatus, VenueCircuitBreaker
from app.models.hive_bots import TradingBot
from app.models.nalanda_lake import ChainState, MirrorIndex, SettlementArchive
from app.models.omni_vault import VaultAccountReservation, VaultBookmakerAccount
from app.services.bookmaker_gateway import PaperBookmaker
from app.services.execution import circuit_breaker
from app.services.execution.slippage_guard import LiveQuote, check_slice, net_odds
from app.services.execution.smart_router import (
    Deficit,
    ExecutionOrder,
    InsufficientFleetBalance,
    NoExecutableVenue,
    RedisRouterEvents,
    SmartOrderRouter,
    VenueCapacity,
    plan_slices,
    receipt_digest,
    receipt_payload,
    slice_key,
    slice_ref,
    sweep_router,
)
from app.services.nalanda_chain import canonical_json, verify_chain
from app.services.portfolio_positions import legged_key
from app.services.venue_costs import BookmakerTerms
from app.workers.execution_dispatcher import CfoSliceExecutor, ExecutionDispatcher, SliceOutcome, SliceOutcomeKind, SliceTicket

D = Decimal
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
TABLES = [
    User.__table__, TradingBot.__table__, PhantomLedger.__table__, VaultBookmakerAccount.__table__, VaultAccountReservation.__table__,
    RoutedOrder.__table__, RoutedSlice.__table__, VenueCircuitBreaker.__table__, SettlementArchive.__table__, ChainState.__table__, MirrorIndex.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-router"


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        # A file, not :memory:: every session gets its own connection, so two orders really contend
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'router.sqlite3').as_posix()}", poolclass=NullPool, connect_args={"timeout": 15})
        from app.models.nalanda_lake import APPEND_ONLY_TRIGGERS_SQLITE  # noqa: PLC0415

        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
            for statement in APPEND_ONLY_TRIGGERS_SQLITE:
                if "settlement_archive" in statement:
                    await conn.execute(text(statement))
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


def is_pg(sessions: async_sessionmaker[AsyncSession]) -> bool:
    return sessions.kw["bind"].dialect.name == "postgresql"


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):
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
    return get_settings().model_copy(update={
        "ROUTER_MIN_SLICE_STAKE": D(100), "ROUTER_STAKE_QUANTUM": D(1), "ROUTER_VENUE_STAKE_LIMITS": {}, "ROUTER_MAX_QUOTE_AGE_SECONDS": 30.0,
        "ROUTER_MIN_SLICE_EV": D(0), "ROUTER_SLICE_TIMEOUT_SECONDS": 2.0, "ROUTER_ORPHAN_SECONDS": 120.0, "ROUTER_BREAKER_FAILURES": 2,
        "ROUTER_BREAKER_WINDOW_SECONDS": 60.0, "ROUTER_BREAKER_PAUSE_SECONDS": 300.0, "EXCHANGE_COMMISSION_RATES": {"betfair": 0.05},
        "BOOKMAKER_CURRENCIES": {}, "SENTINEL_ENABLED": True, "PORTFOLIO_CHANNEL_PREFIX": "test_router_portfolio",
    })


class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class FakeQuotes:
    """The Garuda stream as a dict; ``after_reads`` swaps in new prices from the Nth read on (an odds drop)."""

    def __init__(self, clock: Clock, prices: dict[str, str], *, after_reads: int | None = None, then: dict[str, str] | None = None) -> None:
        self.clock, self.prices, self.after_reads, self.then, self.reads = clock, prices, after_reads, then or {}, 0

    async def quotes(self, match_id: str, market: str, selection: str, bookmakers: Sequence[str]) -> dict[str, LiveQuote]:  # noqa: ARG002
        self.reads += 1
        prices = self.then if self.after_reads is not None and self.reads > self.after_reads else self.prices
        return {b: LiveQuote(b, D(prices[b]), self.clock(), source="test") for b in bookmakers if b in prices}


Script = Callable[[SliceTicket], Awaitable[SliceOutcome] | SliceOutcome]


@dataclass
class ScriptedExecutor:
    """Per venue: fill (default), reject, partial, hang. Every ticket it saw is kept."""

    plan: dict[str, Script] = field(default_factory=dict)
    seen: list[SliceTicket] = field(default_factory=list)

    async def execute(self, ticket: SliceTicket) -> SliceOutcome:
        self.seen.append(ticket)
        action = self.plan.get(ticket.venue_id)
        if action is None:
            return SliceOutcome(SliceOutcomeKind.FILLED, "FILLED", ticket.stake, ticket.odds, f"{ticket.venue_id}-bet-{len(self.seen)}", uuid.uuid4())
        result = action(ticket)
        return await result if asyncio.iscoroutine(result) else result  # type: ignore[return-value]


def reject(reason: str = "BOOKMAKER_HTTP_500") -> Script:
    return lambda t: SliceOutcome(SliceOutcomeKind.REJECTED, reason, venue_fault=True)


def partial(share: str) -> Script:
    return lambda t: SliceOutcome(SliceOutcomeKind.PARTIAL, "PARTIAL_FILL", (t.stake * D(share)).quantize(D(1)), t.odds, f"{t.venue_id}-partial", uuid.uuid4())


def hang(seconds: float) -> Script:
    async def run(t: SliceTicket) -> SliceOutcome:
        await asyncio.sleep(seconds)
        return SliceOutcome(SliceOutcomeKind.FILLED, "FILLED", t.stake, t.odds, "late", None)
    return run


@dataclass
class RecordingEvents:
    inner: RedisRouterEvents | None = None
    legs: list[tuple[uuid.UUID | None, dict[str, Any]]] = field(default_factory=list)
    alerts: list[Any] = field(default_factory=list)

    async def legged(self, user_id: uuid.UUID | None, position: dict[str, Any]) -> None:
        self.legs.append((user_id, position))
        if self.inner is not None:
            await self.inner.legged(user_id, position)

    async def alert(self, alert: Any) -> None:
        self.alerts.append(alert)
        if self.inner is not None:
            await self.inner.alert(alert)


def make_router(sessions: async_sessionmaker[AsyncSession], settings: Settings, quotes: FakeQuotes, executor: ScriptedExecutor, clock: Clock,
                events: RecordingEvents | None = None, lock_hook: Callable[[], Awaitable[None]] | None = None) -> tuple[SmartOrderRouter, RecordingEvents]:
    events = events or RecordingEvents()
    terms = lambda book: BookmakerTerms(D("0.05") if book == "betfair" else D(0), "INR")  # noqa: E731
    router = SmartOrderRouter(sessions, settings, quotes=quotes, dispatcher=ExecutionDispatcher(executor, settings), events=events, terms=terms,
                              clock=clock, lock_hook=lock_hook)
    return router, events


async def add_account(sessions: async_sessionmaker[AsyncSession], book: str, balance: str | None, *, cap: str | None = None, priority: int = 1,
                      currency: str = "INR", active: bool = True) -> uuid.UUID:
    async with sessions() as session:
        row = VaultBookmakerAccount(
            id=uuid.uuid4(), bookmaker_id=book, label=f"{book} main", identity_digest=uuid.uuid4().hex, secrets_fingerprint=uuid.uuid4().hex,
            currency=currency, adapter_key="GenericAdapter", is_active=active, priority=priority, balance=None if balance is None else D(balance),
            stake_cap=None if cap is None else D(cap), reserved=D(0), source="test", verification_status="UNVERIFIED",
        )
        session.add(row)
        await session.commit()
        return row.id


async def add_user(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        row = User(username=f"router_{uuid.uuid4().hex[:6]}", hashed_password=get_password_hash("Correct-Horse-9"))
        session.add(row)
        await session.commit()
        return row.id


def order(order_id: str, stake: str, books: Sequence[str], *, odds: str = "2.10", floor: str = "2.00", slip: str = "5", prob: str | None = None) -> ExecutionOrder:
    return ExecutionOrder(order_id=order_id, match_id="epl-ars-che", market="h2h", selection="HOME", odds=D(odds), desired_total_stake=D(stake),
                          max_slippage_pct=D(slip), min_acceptable_odds=D(floor), target_bookmakers=tuple(books), true_prob=None if prob is None else D(prob))


async def reserved(sessions: async_sessionmaker[AsyncSession], account_id: uuid.UUID) -> Decimal:
    async with sessions() as session:
        return D((await session.get(VaultBookmakerAccount, account_id)).reserved)  # type: ignore[union-attr]


async def open_holds(sessions: async_sessionmaker[AsyncSession]) -> list[VaultAccountReservation]:
    async with sessions() as session:
        return list((await session.execute(select(VaultAccountReservation).where(VaultAccountReservation.released_at.is_(None)))).scalars())


# ================================================================ the slicer (pure)
def _venue(book: str, cap: str, odds: str = "2.10", *, min_stake: str = "100", commission: str = "0") -> VenueCapacity:
    o, c = D(odds), D(commission)
    return VenueCapacity(book, uuid.uuid4(), book, 1, D(cap), D(min_stake), o, net_odds(o, c), c)


def test_the_slicer_is_proportional_respects_minimums_and_gives_the_remainder_to_the_best_price() -> None:
    one = plan_slices(D(20_000), [_venue("parimatch", "30000", "2.12"), _venue("1xbet", "30000", "2.10")], D(1))
    assert [(s.venue.venue_id, s.stake) for s in one] == [("parimatch", D(20_000))]  # the best price carries it alone
    two = plan_slices(D(50_000), [_venue("parimatch", "30000"), _venue("1xbet", "30000")], D(1))
    assert [(s.index, s.venue.venue_id, s.stake) for s in two] == [(0, "1xbet", D(25_000)), (1, "parimatch", D(25_000))]
    lopsided = plan_slices(D(10_001), [_venue("parimatch", "9000", "2.15"), _venue("1xbet", "3000", "2.10")], D(1))
    assert {s.venue.venue_id: s.stake for s in lopsided} == {"parimatch": D(7501), "1xbet": D(2500)}  # proportional 3:1, odd rupee to the better price
    assert sum(s.stake for s in lopsided) == D(10_001) and all(s.stake <= s.venue.capacity for s in lopsided)
    tiny = plan_slices(D(9_050), [_venue("parimatch", "9000"), _venue("stake", "100000", min_stake="5000"), _venue("1xbet", "100", "2.20")], D(1))
    assert all(s.stake >= s.venue.min_stake for s in tiny) and sum(s.stake for s in tiny) == D(9_050)
    short = plan_slices(D(50_000), [_venue("parimatch", "20000"), _venue("1xbet", "12000")], D(1))
    assert short == Deficit(D(50_000), D(32_000))


def test_commission_is_priced_into_every_slice_before_dispatch(settings: Settings) -> None:
    o = order("o-ev", "1000", ["parimatch", "betfair"], odds="2.10", floor="2.05")
    pm = check_slice(o, "parimatch", LiveQuote("parimatch", D("2.10"), T0), BookmakerTerms(D(0), "INR"), now=T0, settings=settings)
    bf = check_slice(o, "betfair", LiveQuote("betfair", D("2.10"), T0), BookmakerTerms(D("0.05"), "GBP"), now=T0, settings=settings)
    assert pm.ok and pm.net_odds == D("2.1000")
    assert not bf.ok and bf.reason == "NET_BELOW_FLOOR" and bf.net_odds == D("2.0450")  # 1 + 1.10 x 0.95: Betfair's 5% sinks it
    with_prob = order("o-ev2", "1000", ["betfair"], odds="2.10", floor="2.00", prob="0.48")
    verdict = check_slice(with_prob, "betfair", LiveQuote("betfair", D("2.10"), T0), BookmakerTerms(D("0.05"), "GBP"), now=T0, settings=settings)
    assert not verdict.ok and verdict.reason == "NEGATIVE_EV" and verdict.net_ev == D("-0.018400")  # 0.48 x 2.045 - 1
    stale = check_slice(o, "parimatch", LiveQuote("parimatch", D("2.10"), T0 - timedelta(seconds=31)), BookmakerTerms(D(0), "INR"), now=T0, settings=settings)
    assert stale.reason == "QUOTE_STALE"


# ================================================================ proof 1: deadlock-free atomic reservation
async def test_two_concurrent_orders_on_one_account_make_one_fill_and_one_clean_rejection(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    account = await add_account(sessions, "parimatch", "15000")
    entered = 0

    async def contend() -> None:  # both orders are inside the reservation together: the lock must serialise them
        nonlocal entered
        entered += 1
        await asyncio.sleep(0.3)

    router, _ = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10"}), ScriptedExecutor(), clock, lock_hook=contend)
    results = await asyncio.wait_for(asyncio.gather(
        router.route(order("race-a", "10000", ["parimatch"])), router.route(order("race-b", "10000", ["parimatch"])), return_exceptions=True,
    ), timeout=60)  # a deadlock would hang (or raise): it must do neither
    fills = [r for r in results if isinstance(r, dict) and r["status"] == "FILLED"]
    refusals = [r for r in results if isinstance(r, InsufficientFleetBalance)]
    assert len(fills) == 1 and len(refusals) == 1, results
    assert str(refusals[0]) == "InsufficientFleetBalance: required ₹10,000, available ₹5,000"
    assert await reserved(sessions, account) == D(10_000)  # exactly one stake held, never both
    assert [h.amount for h in await open_holds(sessions)] == [D(10_000)]
    assert entered >= 1


async def test_opposite_lock_orders_cannot_deadlock(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    a = await add_account(sessions, "parimatch", "60000")
    b = await add_account(sessions, "1xbet", "60000")

    async def hold() -> None:
        await asyncio.sleep(0.2)

    router, _ = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10", "1xbet": "2.10"}), ScriptedExecutor(), clock, lock_hook=hold)
    # The same two accounts, named in opposite orders: without ORDER BY id each would lock "its" first account
    results = await asyncio.wait_for(asyncio.gather(
        router.route(order("x-1", "90000", ["parimatch", "1xbet"])), router.route(order("x-2", "20000", ["1xbet", "parimatch"])), return_exceptions=True,
    ), timeout=60)
    assert not any(isinstance(r, BaseException) and not isinstance(r, InsufficientFleetBalance) for r in results), results
    total = await reserved(sessions, a) + await reserved(sessions, b)
    assert total == sum((D(r["filled_stake"]) for r in results if isinstance(r, dict)), D(0)) and total <= D(120_000)


# ================================================================ proof 2: cross-venue slicing + idempotency
async def test_a_50k_order_over_two_venues_is_two_slices_with_deterministic_keys_and_a_retry_holds_nothing_twice(
    sessions: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    clock = Clock()
    pm = await add_account(sessions, "parimatch", "30000")
    onex = await add_account(sessions, "1xbet", "30000")
    executor = ScriptedExecutor()
    router, _ = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10", "1xbet": "2.08"}), executor, clock)
    view = await router.route(order("ORD-50K", "50000", ["parimatch", "1xbet"]))
    assert view["status"] == "FILLED" and D(view["filled_stake"]) == D(50_000)
    slices = view["slices"]
    assert len(slices) == 2
    assert [s["idempotency_key"] for s in slices] == ["ORD-50K_1xbet_0", "ORD-50K_parimatch_1"] == [slice_key("ORD-50K", "1xbet", 0), slice_key("ORD-50K", "parimatch", 1)]
    assert [s["client_ref"] for s in slices] == [str(slice_ref(s["idempotency_key"])) for s in slices]
    assert sorted(D(s["stake"]) for s in slices) == [D(25_000), D(25_000)] and all(s["status"] == "FILLED" for s in slices)
    assert D(view["blended_odds"]) == D("2.0900")  # (25k x 2.08 + 25k x 2.10) / 50k
    holds = await open_holds(sessions)
    assert sorted(h.order_ref for h in holds) == sorted(s["client_ref"] for s in slices)  # the slice UUID is the reservation key
    assert (await reserved(sessions, pm), await reserved(sessions, onex)) == (D(25_000), D(25_000))
    assert {t.client_ref for t in executor.seen} == {uuid.UUID(s["client_ref"]) for s in slices}  # and the ledger's idempotency key
    again = await router.route(order("ORD-50K", "50000", ["parimatch", "1xbet"]))  # a retry: the same order
    assert again["id"] == view["id"] and len(await open_holds(sessions)) == 2 and len(executor.seen) == 2
    assert (await reserved(sessions, pm), await reserved(sessions, onex)) == (D(25_000), D(25_000))


async def test_the_fleet_refuses_an_order_it_cannot_carry_with_the_deficit(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    await add_account(sessions, "parimatch", "20000")
    await add_account(sessions, "1xbet", "12000")
    await add_account(sessions, "stake", "99999", currency="USDT")  # another currency: not part of a rupee order's fleet
    router, _ = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10", "1xbet": "2.10", "stake": "2.10"}), ScriptedExecutor(), clock)
    with pytest.raises(InsufficientFleetBalance) as refused:
        await router.route(order("too-big", "50000", ["parimatch", "1xbet", "stake"]))
    assert str(refused.value) == "InsufficientFleetBalance: required ₹50,000, available ₹32,000"
    assert D(refused.value.detail["required"]) == D(50_000) and D(refused.value.detail["available"]) == D(32_000)
    assert refused.value.detail["excluded"]["stake"]["reason"] == "NO_ACCOUNT"
    assert await open_holds(sessions) == []
    with pytest.raises(InsufficientFleetBalance):  # the refusal is the order's answer: a retry gets it again, nothing held
        await router.route(order("too-big", "50000", ["parimatch", "1xbet"]))
    view = await router.view("too-big")
    assert view["status"] == "REJECTED" and view["reason"] == "INSUFFICIENT_FLEET_BALANCE" and view["slices"] == []


# ================================================================ proof 3: the slippage guard
async def test_a_sudden_odds_drop_aborts_the_order_and_releases_every_hold(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    pm = await add_account(sessions, "parimatch", "30000")
    onex = await add_account(sessions, "1xbet", "30000")
    executor = ScriptedExecutor()
    quotes = FakeQuotes(clock, {"parimatch": "2.10", "1xbet": "2.10"}, after_reads=1, then={"parimatch": "2.10", "1xbet": "1.90"})
    router, _ = make_router(sessions, settings, quotes, executor, clock)
    view = await router.route(order("drop", "50000", ["parimatch", "1xbet"], floor="2.00"))
    assert view["status"] == "ABORTED" and view["reason"] == "SLIPPAGE_GUARD"
    assert [c["reason"] for c in view["detail"]["guard"]["checks"] if not c["ok"]] == ["BELOW_MIN_ODDS"]
    assert all(s["status"] == "RELEASED" and s["reason"] == "SLIPPAGE_GUARD" for s in view["slices"]) and len(view["slices"]) == 2
    assert executor.seen == []  # nothing left
    assert await open_holds(sessions) == [] and (await reserved(sessions, pm), await reserved(sessions, onex)) == (D(0), D(0))
    slipped = FakeQuotes(clock, {"parimatch": "2.10"}, after_reads=1, then={"parimatch": "2.04"})  # above the floor, but 2.9% below the ask
    router2, _ = make_router(sessions, settings, slipped, executor, clock)
    tight = await router2.route(order("slip", "1000", ["parimatch"], floor="2.00", slip="2"))
    assert tight["status"] == "ABORTED" and tight["detail"]["guard"]["checks"][0]["reason"] == "SLIPPAGE_EXCEEDED"
    assert await reserved(sessions, pm) == D(0)


# ================================================================ proof 4: the venue circuit breaker
async def test_two_consecutive_rejections_trip_the_venue_breaker(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    onex = await add_account(sessions, "1xbet", "100000")
    await add_account(sessions, "parimatch", "100000")
    executor = ScriptedExecutor({"1xbet": reject()})
    router, events = make_router(sessions, settings, FakeQuotes(clock, {"1xbet": "2.10", "parimatch": "2.05"}), executor, clock)
    # A third order sits reserved on 1xbet, not yet dispatched, when the breaker trips
    waiting = order("waiting", "1000", ["1xbet"])
    assert await router._claim(waiting, None)
    planned = await router._reserve(waiting, await router.guard.read(waiting, ["1xbet"]))
    assert planned.refusal is None and await reserved(sessions, onex) == D(1000)
    first = await router.route(order("rej-1", "1000", ["1xbet"]))
    assert first["status"] == "REJECTED" and first["slices"][0]["status"] == "REJECTED"
    async with sessions() as session:
        assert (await circuit_breaker.paused_venues(session, ["1xbet"], now=clock())) == {}
    clock.tick(30)  # within 60 s of the first
    second = await router.route(order("rej-2", "1000", ["1xbet"]))
    assert second["status"] == "REJECTED"
    async with sessions() as session:
        paused = await circuit_breaker.paused_venues(session, ["1xbet"], now=clock())
    assert paused == {"1xbet": clock() + timedelta(minutes=5)}
    critical = [a for a in events.alerts if a.kind == "VENUE_CIRCUIT_OPEN"]
    assert len(critical) == 1 and critical[0].severity == "CRITICAL" and critical[0].detail["released"] == ["waiting_1xbet_0"]
    assert (await router.view("waiting"))["slices"][0]["status"] == "RELEASED" and await reserved(sessions, onex) == D(0)  # pending hold released
    with pytest.raises(NoExecutableVenue):
        await router.route(order("while-paused", "1000", ["1xbet"]))
    routed = await router.route(order("around", "1000", ["1xbet", "parimatch"]))  # planned around the paused venue
    assert [s["venue_id"] for s in routed["slices"]] == ["parimatch"] and routed["detail"]["excluded"]["1xbet"]["reason"] == "VENUE_PAUSED"
    clock.tick(301)
    report = await sweep_router(sessions, settings, events, now=clock())
    assert report["venues_live"] == ["1xbet"] and any(a.kind == "VENUE_CIRCUIT_CLOSED" for a in events.alerts)


async def test_failures_further_apart_than_the_window_do_not_trip(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    await add_account(sessions, "1xbet", "100000")
    router, events = make_router(sessions, settings, FakeQuotes(clock, {"1xbet": "2.10"}), ScriptedExecutor({"1xbet": reject()}), clock)
    await router.route(order("slow-1", "1000", ["1xbet"]))
    clock.tick(61)
    await router.route(order("slow-2", "1000", ["1xbet"]))
    async with sessions() as session:
        assert await circuit_breaker.paused_venues(session, ["1xbet"], now=clock()) == {}
    user_limits = ScriptedExecutor({"1xbet": lambda t: SliceOutcome(SliceOutcomeKind.REJECTED, "BLOCKED_BY_DRAWDOWN")})  # the user's limit, not the venue
    router2, _ = make_router(sessions, settings, FakeQuotes(clock, {"1xbet": "2.10"}), user_limits, clock, events)
    await router2.route(order("dd-1", "1000", ["1xbet"]))
    await router2.route(order("dd-2", "1000", ["1xbet"]))
    async with sessions() as session:
        assert await circuit_breaker.paused_venues(session, ["1xbet"], now=clock()) == {}
    assert not any(a.kind == "VENUE_CIRCUIT_OPEN" for a in events.alerts)


# ================================================================ proof 5: partial fills reach the Active Portfolio
async def test_a_partial_fill_releases_the_rejected_slice_and_flags_a_legged_position(sessions: async_sessionmaker[AsyncSession], settings: Settings, redis: Redis) -> None:
    clock = Clock()
    pm = await add_account(sessions, "parimatch", "30000")
    onex = await add_account(sessions, "1xbet", "30000")
    user = uuid.uuid4()
    events = RecordingEvents(inner=RedisRouterEvents(redis, settings))
    router, _ = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10", "1xbet": "2.10"}), ScriptedExecutor({"1xbet": reject()}), clock, events)
    channel = redis.pubsub()
    await channel.subscribe(f"{settings.PORTFOLIO_CHANNEL_PREFIX}:{user}")
    await channel.get_message(timeout=1)  # the subscribe confirmation
    view = await router.route(order("legged", "50000", ["parimatch", "1xbet"]), user_id=user)
    assert view["status"] == "PARTIAL" and view["hedge_state"] == "ELIGIBLE" and D(view["filled_stake"]) == D(25_000)
    by_venue = {s["venue_id"]: s for s in view["slices"]}
    assert by_venue["parimatch"]["status"] == "FILLED" and by_venue["1xbet"]["status"] == "REJECTED"
    assert await reserved(sessions, onex) == D(0) and await reserved(sessions, pm) == D(25_000)  # slice B's hold released at once
    assert len(events.legs) == 1 and events.legs[0][0] == user and events.legs[0][1]["unfilled_stake"] == "25000.0000"
    stored = json.loads(await redis.hget(legged_key(settings, user), "legged"))
    assert stored["type"] == "legged_position" and stored["hedge_eligible"] is True and stored["filled_stake"] == "25000.0000"
    frame = None
    for _ in range(20):
        frame = await channel.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if frame is not None:
            break
    assert frame is not None and json.loads(frame["data"])["type"] == "legged_position"
    await channel.aclose()
    warned = [a for a in events.alerts if a.kind == "LEGGED_POSITION"]
    assert len(warned) == 1 and warned[0].severity == "WARNING"
    assert view["receipt_sha256"] is not None  # a partial fill is receipted too


async def test_a_partly_matched_slice_keeps_only_the_matched_stake_on_hold(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    pm = await add_account(sessions, "parimatch", "30000")
    router, events = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10"}), ScriptedExecutor({"parimatch": partial("0.6")}), clock)
    view = await router.route(order("p60", "10000", ["parimatch"]))
    assert view["status"] == "PARTIAL" and view["slices"][0]["status"] == "PARTIAL" and D(view["filled_stake"]) == D(6000)
    assert await reserved(sessions, pm) == D(6000) and [h.amount for h in await open_holds(sessions)] == [D(6000)]
    assert len(events.legs) == 1


async def test_an_unanswered_slice_stays_held_until_the_ledger_confirms_it(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    pm = await add_account(sessions, "parimatch", "30000")
    user = await add_user(sessions)
    router, events = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10"}), ScriptedExecutor({"parimatch": hang(10)}), clock)
    view = await router.route(order("silent", "5000", ["parimatch"]), user_id=user)
    assert view["status"] == "UNCONFIRMED" and view["slices"][0]["status"] == "UNKNOWN" and view["slices"][0]["reason"] == "TIMEOUT"
    assert await reserved(sessions, pm) == D(5000)  # it may be live: never released by the router
    async with sessions() as session:  # reconciliation later finds the bet in the ledger
        session.add(PhantomLedger(user_id=user, idempotency_key=uuid.UUID(view["slices"][0]["client_ref"]), fixture_id="epl-ars-che", market="h2h",
                                  selection="HOME", bookmaker_id="parimatch", stake_inr=D(5000), odds=D("2.10"), potential_pnl=D(5500), status=LedgerStatus.PENDING))
        await session.commit()
    clock.tick(30)
    report = await sweep_router(sessions, settings, events, now=clock())
    assert report["reconciled"] == 1
    done = await router.view("silent")
    assert done["status"] == "FILLED" and done["slices"][0]["reason"] == "CONFIRMED_BY_LEDGER" and done["receipt_sha256"] is not None


# ================================================================ Nalanda: the hash-chained receipt
async def test_every_fill_gets_a_sha256_receipt_mirrored_into_the_nalanda_chain(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    await add_account(sessions, "parimatch", "30000")
    await add_account(sessions, "1xbet", "30000")
    router, _ = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10", "1xbet": "2.10"}), ScriptedExecutor(), clock)
    first = await router.route(order("rcpt-1", "50000", ["parimatch", "1xbet"]))
    second = await router.route(order("rcpt-2", "1000", ["parimatch"]))
    async with sessions() as session:
        row = (await session.execute(select(RoutedOrder).where(RoutedOrder.order_id == "rcpt-1"))).scalar_one()
        slices = list((await session.execute(select(RoutedSlice).where(RoutedSlice.routed_order_id == row.id))).scalars())
        payload = receipt_payload(row, slices)
        archived = list((await session.execute(select(SettlementArchive).where(SettlementArchive.record_kind == "ROUTED_ORDER").order_by(SettlementArchive.seq))).scalars())
    assert first["receipt_sha256"] == receipt_digest(payload) == hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    assert [a.source_id for a in archived] == ["routed_order:rcpt-1", "routed_order:rcpt-2"]
    assert archived[1].prev_hash == archived[0].row_hash  # chained
    assert (first["nalanda_seq"], first["nalanda_hash"]) == (archived[0].seq, archived[0].row_hash) and second["nalanda_seq"] == archived[1].seq
    linked = archived[0].payload
    assert linked["receipt_sha256"] == first["receipt_sha256"] and linked["order_id"] == "rcpt-1"
    assert sorted(s["idempotency_key"] for s in linked["slices"]) == sorted(s["idempotency_key"] for s in first["slices"])
    assert all(s["remote_bet_id"] for s in linked["slices"])  # each child's bookmaker confirmation
    assert await router.mirror("rcpt-1") == first["nalanda_seq"]  # idempotent: archived once
    async with sessions() as session:
        assert (await session.scalar(select(func.count()).select_from(SettlementArchive))) == 2
        report = await verify_chain(session)
    assert report.ok, report.as_dict()


# ================================================================ operator release + sweep
async def test_orphaned_reservations_are_released_by_hand_only_after_the_timeout(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    clock = Clock()
    pm = await add_account(sessions, "parimatch", "30000")
    router, events = make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10"}), ScriptedExecutor(), clock)
    stuck = order("stuck", "2000", ["parimatch"])
    assert await router._claim(stuck, None)
    planned = await router._reserve(stuck, await router.guard.read(stuck, ["parimatch"]))  # the process "dies" here
    piece = planned.slices[0]
    with pytest.raises(ValueError, match="orphaned after"):
        await router.release_orphan(piece.id)
    clock.tick(121)
    assert (await router.view("stuck"))["slices"][0]["orphaned"] is True
    assert (await sweep_router(sessions, settings, events, now=clock()))["orphans"] == 1
    view = await router.release_orphan(piece.id)
    assert view["status"] == "ABORTED" and view["slices"][0]["status"] == "RELEASED" and await reserved(sessions, pm) == D(0)
    filled = await router.route(order("sent", "1000", ["parimatch"]))
    with pytest.raises(ValueError, match="never dispatched"):
        await router.release_orphan(uuid.UUID(filled["slices"][0]["id"]), force=True)


# ================================================================ the CFO executor's classification
@dataclass
class FakeTrade:
    answer: Any

    async def execute_leg(self, ticket: Any, *, min_odds: Decimal) -> Any:  # noqa: ARG002
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_the_cfo_executor_classifies_every_answer() -> None:
    from app.schemas.cfo_vault import ExecutionReceipt  # noqa: PLC0415
    from app.services.cfo_ledger import CfoError  # noqa: PLC0415

    ticket = SliceTicket(uuid.uuid4(), "o_parimatch_0", uuid.uuid4(), uuid.uuid4(), "parimatch", None, "m", "h2h", "HOME", D("2.10"), D("2.00"), D(1000), "INR", D(0))

    def receipt(**kw: Any) -> ExecutionReceipt:
        base = dict(status="EXECUTED", message="ok", ledger_id=uuid.uuid4(), remote_bet_id="pm-1", bookmaker_id="parimatch", fixture_id="m", selection="HOME",
                    stake_inr=D(1000), odds=D("2.10"), potential_pnl=D(1100), available_balance=D(0), exposure_balance=D(1000), execution_mode="paper")
        return ExecutionReceipt(**(base | kw))

    filled = await CfoSliceExecutor(FakeTrade(receipt())).execute(ticket)  # type: ignore[arg-type]
    assert filled.kind is SliceOutcomeKind.FILLED and filled.filled_stake == D(1000) and filled.remote_bet_id == "pm-1"
    part = await CfoSliceExecutor(FakeTrade(receipt(stake_inr=D(400), partial_fill=True))).execute(ticket)  # type: ignore[arg-type]
    assert part.kind is SliceOutcomeKind.PARTIAL and part.filled_stake == D(400)
    unknown = await CfoSliceExecutor(FakeTrade(receipt(status="UNKNOWN"))).execute(ticket)  # type: ignore[arg-type]
    assert unknown.kind is SliceOutcomeKind.UNKNOWN and unknown.venue_fault
    venue = await CfoSliceExecutor(FakeTrade(CfoError("BOOKMAKER_HTTP_500", "x", audited=True))).execute(ticket)  # type: ignore[arg-type]
    assert venue.kind is SliceOutcomeKind.REJECTED and venue.venue_fault
    mine = await CfoSliceExecutor(FakeTrade(CfoError("BLOCKED_BY_DRAWDOWN", "x"))).execute(ticket)  # type: ignore[arg-type]
    assert mine.kind is SliceOutcomeKind.REJECTED and not mine.venue_fault  # the user's own limit never pauses a bookmaker
    dup = await CfoSliceExecutor(FakeTrade(CfoError("DUPLICATE_REQUEST", "x"))).execute(ticket)  # type: ignore[arg-type]
    assert dup.kind is SliceOutcomeKind.UNKNOWN  # submitted before: may have filled, so it stays held


# ================================================================ the API
async def test_the_router_api_routes_lists_and_reports_breakers(sessions: async_sessionmaker[AsyncSession], settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(datetime.now(UTC))
    await add_account(sessions, "parimatch", "30000")
    await add_account(sessions, "1xbet", "30000")
    admin = User(id=uuid.uuid4(), username="admin", hashed_password="x", role="ADMIN")
    executor = ScriptedExecutor({"1xbet": reject()})
    monkeypatch.setattr(router_api, "build_router", lambda request, s, st: make_router(sessions, settings, FakeQuotes(clock, {"parimatch": "2.10", "1xbet": "2.10"}), executor, clock)[0])
    app = FastAPI()
    app.include_router(router_api.router, prefix="/api/v1")
    app.state.bookmaker, app.state.redis = PaperBookmaker(), None
    app.dependency_overrides[get_current_admin] = lambda: admin
    app.dependency_overrides[router_api.get_router_sessions] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"order_id": "api-1", "match_id": "epl-ars-che", "selection": "HOME", "odds": "2.10", "desired_total_stake": "50000",
                "min_acceptable_odds": "2.00", "target_bookmakers": ["Parimatch", "onexbet"]}
        made = await client.post("/api/v1/router/orders", json=body)
        assert made.status_code == 201 and made.json()["status"] == "PARTIAL"
        assert {s["venue_id"] for s in made.json()["slices"]} == {"parimatch", "1xbet"}  # names canonicalised
        refused = await client.post("/api/v1/router/orders", json=body | {"order_id": "api-2", "desired_total_stake": "99000"})
        assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "INSUFFICIENT_FLEET_BALANCE"
        bad = await client.post("/api/v1/router/orders", json=body | {"order_id": "api-3", "min_acceptable_odds": "2.50"})
        assert bad.status_code == 422
        active = await client.get("/api/v1/router/orders", params={"active": "true"})
        assert [o["order_id"] for o in active.json()] == ["api-1"]  # legged: still on the monitor
        assert (await client.get("/api/v1/router/orders/api-1")).json()["hedge_state"] == "ELIGIBLE"
        breakers = (await client.get("/api/v1/router/venues")).json()
        assert {v["venue_id"]: v["state"] for v in breakers["venues"]} == {"1xbet": "LIVE", "parimatch": "LIVE"}
        assert (await client.get("/api/v1/router/legged")).json()["orders"][0]["order_id"] == "api-1"
        slice_id = made.json()["slices"][0]["id"]
        assert (await client.post(f"/api/v1/router/slices/{slice_id}/release")).status_code == 409  # dispatched: may be live
        assert (await client.post("/api/v1/router/venues/1xbet/reset")).json()["state"] == "LIVE"
