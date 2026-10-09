"""Group 62: the CFO ledger, the 5-pillar risk guard, two-phase execution and steam detection.

The ledger runs on SQLite (no row locks there) for the money arithmetic; ``FOR UPDATE NOWAIT`` is
proven against a real PostgreSQL when ``TEST_POSTGRES_URL`` points at a disposable database. Redis
pieces (idempotency, kill switch, streak counter, velocity history) use a real server on a
dedicated, flushed index and skip when none is reachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.fernet import Fernet
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
from app.models.cfo_vault import (
    AuditEvent,
    AuditLog,
    BankrollAccount,
    ImmutableRecordError,
    LedgerAccount,
    LedgerEntry,
    LedgerStatus,
    MarketResult,
    PhantomLedger,
    RiskGuardSettings,
)
from app.models.hive_bots import TradingBot
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.schemas.aryabhata import SIGNAL_TTL
from app.schemas.cfo_vault import ExecuteTradeRequest
from app.services import cfo_ledger as ledger
from app.services.aryabhata_engine import (
    BookLine,
    EmaState,
    MarketState,
    ema_alpha,
    evaluate_market,
    is_steam_move,
    update_ema,
)
from app.core.security_vault import VaultCrypto
from app.models.execution import EntityMapping, ExecutionVenue
from app.services.bookmaker_gateway import PaperBookmaker
from app.services.sniper import SniperGateway
from app.services.cfo_execution import TradeExecutor
from app.services.cfo_ledger import (
    BankrollLockedError,
    CfoError,
    DuplicateExecutionError,
    InsufficientFundsError,
    OrderTicket,
    lock_bankroll,
    potential_profit,
    reconcile,
    reserve,
    settle,
    settle_markets,
    to_money,
    verify_account,
)
from app.services.risk_guard import RiskGuard, RiskGuardViolation, coefficient_of_variation

D = Decimal
TABLES = [
    User.__table__,
    ExchangeAccount.__table__,
    RiskMandate.__table__,
    BetLedger.__table__,
    SystemSettingsModel.__table__,
    TradingBot.__table__,  # bot sub-accounts reference it (Group 65)
    BankrollAccount.__table__,
    PhantomLedger.__table__,
    LedgerEntry.__table__,
    AuditLog.__table__,
    RiskGuardSettings.__table__,
    MarketResult.__table__,
    ExecutionVenue.__table__,
    EntityMapping.__table__,
]
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_SENTINEL = "betdoc:test-sentinel"


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    """Copies of TABLES minus Postgres-only regex checks (`~`), which SQLite can't compile."""
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and "~" in str(c.sqltext)]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Every ledger test runs on SQLite, and again on PostgreSQL (real row locks, separate
    connections per session, the append-only triggers) when ``TEST_POSTGRES_URL`` is set."""
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
        if await client.dbsize() and not await client.exists(_SENTINEL):
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
            "starting_bankroll": 10_000.0,
            "CFO_KILL_SWITCH_KEY": "test:kill_switch",
            "CFO_STREAK_KEY_PREFIX": "test:risk:streak",
            "CFO_IDEMPOTENCY_KEY_PREFIX": "test:idempotency",
            "ARYABHATA_PREFIX": "test_arya",
            "LIVE_ODDS_CHANNEL": "test:live_odds",
            "CFO_EXECUTION_MODE": "paper",
        }
    )


@pytest_asyncio.fixture
async def user_id(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions() as session:
        user = User(username=f"cfo_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(user)
        await session.commit()
        return user.id


@pytest_asyncio.fixture
async def opened(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID, settings: Settings) -> uuid.UUID:
    """The user's bankroll account, opened and committed at the ₹10,000 starting bankroll."""
    async with sessions() as session:
        await lock_bankroll(session, user_id, settings)
        await session.commit()
    return user_id


async def balances(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> tuple[Decimal, Decimal, Decimal]:
    async with sessions() as session:
        account = await session.scalar(select(BankrollAccount).where(BankrollAccount.user_id == user_id))
        assert account is not None
        return account.available_balance, account.exposure_balance, account.peak_balance


async def count(sessions: async_sessionmaker[AsyncSession], model: Any, *where: Any) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def ticket(user_id: uuid.UUID, stake: str = "250.00", odds: str = "2.50", fixture: str = "fx-ars-lee", selection: str = "HOME", key: uuid.UUID | None = None) -> OrderTicket:
    return OrderTicket(
        user_id=user_id,
        idempotency_key=key or uuid.uuid4(),
        fixture_id=fixture,
        market="Match Odds",
        selection=selection,
        bookmaker_id="smarkets",
        stake_inr=D(stake),
        odds=D(odds),
    )


def order(stake: str = "250.00", odds: str = "2.50", fixture: str = "fx-ars-lee", selection: str = "HOME", **extra: Any) -> ExecuteTradeRequest:
    return ExecuteTradeRequest(
        idempotency_key=extra.pop("idempotency_key", uuid.uuid4()),
        fixture_id=fixture,
        selection=selection,
        bookmaker_id="smarkets",
        odds=D(odds),
        stake_inr=D(stake),
        **extra,
    )


class Bookmaker:
    """httpx MockTransport handler: a canned response or a transport failure, and a call log."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.respond = respond
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(json.loads(request.content))
        return self.respond(request)

    def gateway(self, wire: Wire) -> SniperGateway:
        """The live Omni-Sniper path, pointed at this mock bookmaker's execution venue."""
        return SniperGateway(wire.sessions, wire.redis, wire.settings, wire.vault, httpx.AsyncClient(transport=httpx.MockTransport(self)))


@dataclass(frozen=True)
class Wire:
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis
    settings: Settings
    vault: VaultCrypto


@pytest_asyncio.fixture
async def wire(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> Wire:
    """A partner venue for "smarkets" (static key, generous rate limit) with every test fixture mapped."""
    vault = VaultCrypto(Fernet.generate_key().decode())
    async with sessions() as session:
        session.add(
            ExecutionVenue(
                id="smarkets", display_name="Partner book", adapter="generic_json", base_url="https://partner.example/api",
                auth_type="static_bearer", place_path="/bets", status_path="/bets", bets_per_second=D("50"), burst=50,
                routes=[], selection_codes={"HOME": "1", "DRAW": "X", "AWAY": "2"},
                encrypted_credentials=vault.encrypt_key(json.dumps({"api_key": "test-key-0001"})), is_enabled=True, is_sandbox=False,
                currency="INR",  # the partner account is in rupees (unset, smarkets would default to GBP)
            )
        )
        await session.flush()  # the venue row first: its mappings reference it
        for fixture in ("fx-ars-lee", "fx-other"):
            session.add(EntityMapping(venue_id="smarkets", kind="fixture", canonical_key=fixture, remote_id=f"EV-{fixture}", source="manual", detail={}))
        await session.commit()
    return Wire(sessions, redis, settings, vault)


def status(code: int, body: Any = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(code, json=body if body is not None else {"error": "server error"})


def fail(exc: type[Exception]) -> Callable[[httpx.Request], httpx.Response]:
    def raise_(request: httpx.Request) -> httpx.Response:
        raise exc("simulated", request=request)  # type: ignore[call-arg]

    return raise_


# ================================================================ money and the double-entry ledger
@pytest.mark.parametrize("value", ["1.005", "NaN", "Infinity", "abc", True, None])
def test_money_is_never_rounded_or_invented(value: object) -> None:
    with pytest.raises(ValueError):
        to_money(value)


def test_potential_profit_rounds_down_to_the_paisa() -> None:
    assert potential_profit(D("333.33"), D("2.555")) == D("518.32")  # 518.3281... never 518.33
    assert potential_profit(D("200.00"), D("2.50")) == D("300.00")


@pytest.mark.asyncio
async def test_account_opens_at_the_live_bankroll_with_a_balanced_journal(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID) -> None:
    assert await balances(sessions, opened) == (D("10000.00"), D("0.00"), D("10000.00"))
    async with sessions() as session:
        derived = await verify_account(session, opened)
    assert derived == {"AVAILABLE": D("10000.00"), "EXPOSURE": D("0"), "PNL": D("0"), "EQUITY": D("-10000.00")}


@pytest.mark.asyncio
async def test_reserve_moves_the_stake_into_exposure_double_entry(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, settings: Settings) -> None:
    async with sessions() as session:
        account = await lock_bankroll(session, opened, settings)
        entry = await reserve(session, account, ticket(opened))
        await session.commit()
    assert entry.status is LedgerStatus.PENDING and entry.potential_pnl == D("375.00")
    assert await balances(sessions, opened) == (D("9750.00"), D("250.00"), D("10000.00"))
    async with sessions() as session:
        legs = (await session.execute(select(LedgerEntry.account, LedgerEntry.amount).where(LedgerEntry.ledger_id == entry.id))).all()
        assert sorted((str(a), v) for a, v in legs) == [("AVAILABLE", D("-250.00")), ("EXPOSURE", D("250.00"))]
        await verify_account(session, opened)


@pytest.mark.asyncio
async def test_a_stake_above_the_available_balance_is_refused(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, settings: Settings) -> None:
    async with sessions() as session:
        account = await lock_bankroll(session, opened, settings)
        with pytest.raises(InsufficientFundsError):
            await reserve(session, account, ticket(opened, stake="10000.01"))
        await session.rollback()
    assert await balances(sessions, opened) == (D("10000.00"), D("0.00"), D("10000.00"))
    assert await count(sessions, PhantomLedger) == 0


@pytest.mark.asyncio
async def test_a_reused_idempotency_key_never_reserves_twice(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, settings: Settings) -> None:
    key = uuid.uuid4()
    async with sessions() as session:
        account = await lock_bankroll(session, opened, settings)
        await reserve(session, account, ticket(opened, key=key))
        with pytest.raises(DuplicateExecutionError):
            await reserve(session, account, ticket(opened, key=key))
        await session.commit()  # the first reservation survives the refused second (savepoint)
    assert await balances(sessions, opened) == (D("9750.00"), D("250.00"), D("10000.00"))
    assert await count(sessions, PhantomLedger) == 1


@pytest.mark.parametrize(
    ("won", "available", "pnl"),
    [(True, D("10300.00"), D("300.00")), (False, D("9800.00"), D("-200.00"))],
)
@pytest.mark.asyncio
async def test_settlement_amounts(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, settings: Settings, won: bool, available: Decimal, pnl: Decimal) -> None:
    async with sessions() as session:
        account = await lock_bankroll(session, opened, settings)
        entry = await reserve(session, account, ticket(opened, stake="200.00", odds="2.50"))
        assert settle(session, account, entry, won) == pnl
        ledger.mark_peak(account)
        await session.commit()
    peak = max(D("10000.00"), available)
    assert await balances(sessions, opened) == (available, D("0.00"), peak)  # WON: stake + profit back, exposure cleared
    async with sessions() as session:
        derived = await verify_account(session, opened)
    assert derived["PNL"] == -pnl  # income is a credit to PNL


@pytest.mark.asyncio
async def test_the_audit_log_and_journal_are_append_only(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID) -> None:
    async with sessions() as session:
        session.add(ledger.audit_row(AuditEvent.BLOCKED, "BLOCKED_BY_DRAWDOWN", user_id=opened))
        await session.commit()
        row = await session.scalar(select(AuditLog))
        assert row is not None
        row.reason = "TAMPERED"
        with pytest.raises(ImmutableRecordError):
            await session.commit()
        await session.rollback()
        entry = await session.scalar(select(LedgerEntry))
        assert entry is not None
        await session.delete(entry)
        with pytest.raises(ImmutableRecordError):
            await session.commit()
        await session.rollback()


# ================================================================ two-phase execution
@pytest.mark.asyncio
async def test_bookmaker_500_rolls_back_and_returns_every_rupee(
    sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings
, wire: Wire) -> None:
    """The brief's proof: the bookmaker fails after the stake was reserved under the lock. The
    rollback must put the money back in AVAILABLE and leave no ghost bet in EXPOSURE."""
    book = Bookmaker(status(500))
    executor = TradeExecutor(sessions, redis, settings, book.gateway(wire))
    with pytest.raises(CfoError) as caught:
        await executor.execute(opened, order(stake="250.00"))
    assert caught.value.reason == "BOOKMAKER_HTTP_500" and caught.value.status_code == 502
    assert len(book.calls) == 1  # the order really went out with the stake reserved
    assert await balances(sessions, opened) == (D("10000.00"), D("0.00"), D("10000.00"))
    assert await count(sessions, PhantomLedger) == 0  # no ghost bet
    assert await count(sessions, LedgerEntry, LedgerEntry.kind != "OPEN") == 0  # no stray journal legs
    async with sessions() as session:
        await verify_account(session, opened)
        events = (await session.execute(select(AuditLog.event, AuditLog.reason).order_by(AuditLog.created_at))).all()
    assert [(str(e), r) for e, r in events] == [("EXECUTION_ATTEMPT", "GUARDS_PASSED"), ("BOOKMAKER_REJECTED", "BOOKMAKER_HTTP_500")]


@pytest.mark.parametrize(("failure", "reason"), [(httpx.ConnectError, "BOOKMAKER_UNREACHABLE"), (httpx.ConnectTimeout, "BOOKMAKER_UNREACHABLE")])
@pytest.mark.asyncio
async def test_an_order_that_never_left_is_rolled_back(
    sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, failure: type[Exception], reason: str
, wire: Wire) -> None:
    executor = TradeExecutor(sessions, redis, settings, Bookmaker(fail(failure)).gateway(wire))
    with pytest.raises(CfoError) as caught:
        await executor.execute(opened, order())
    assert caught.value.reason == reason
    assert await balances(sessions, opened) == (D("10000.00"), D("0.00"), D("10000.00"))
    assert await count(sessions, PhantomLedger) == 0


@pytest.mark.asyncio
async def test_an_accepted_order_commits(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, wire: Wire) -> None:
    book = Bookmaker(status(200, {"bet_id": "BK-77"}))
    request = order(stake="250.00", odds="2.50")
    receipt = await TradeExecutor(sessions, redis, settings, book.gateway(wire)).execute(opened, request)
    assert receipt.status == "EXECUTED" and receipt.remote_bet_id == "BK-77"
    # 250 * 1.50 = 375 gross; smarkets keeps 2% of the net win, so the ledger books 367.50
    assert receipt.available_balance == D("9750.00") and receipt.exposure_balance == D("250.00") and receipt.potential_pnl == D("367.50")
    assert book.calls[0]["stake"] == "250.00" and book.calls[0]["client_ref"] == str(request.idempotency_key)
    assert await balances(sessions, opened) == (D("9750.00"), D("250.00"), D("10000.00"))
    async with sessions() as session:
        entry = await session.get(PhantomLedger, receipt.ledger_id)
        assert entry is not None and entry.remote_bet_id == "BK-77" and not entry.reconcile_required


@pytest.mark.parametrize(
    ("respond", "reason"),
    [(fail(httpx.ReadTimeout), "BOOKMAKER_TIMEOUT"), (fail(httpx.RemoteProtocolError), "BOOKMAKER_TRANSPORT_ERROR"), (status(200, {"ok": True}), "BOOKMAKER_BAD_RESPONSE")],
)
@pytest.mark.asyncio
async def test_an_unconfirmed_order_keeps_its_stake_in_exposure(
    sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, respond: Any, reason: str
, wire: Wire) -> None:
    """The order went out and no trustworthy answer came back: the bet may be live, so the money
    must not return to AVAILABLE until reconciliation says so."""
    receipt = await TradeExecutor(sessions, redis, settings, Bookmaker(respond).gateway(wire)).execute(opened, order(stake="250.00"))
    assert receipt.status == "UNKNOWN"
    assert await balances(sessions, opened) == (D("9750.00"), D("250.00"), D("10000.00"))
    async with sessions() as session:
        entry = await session.get(PhantomLedger, receipt.ledger_id)
        assert entry is not None and entry.reconcile_required and entry.status is LedgerStatus.PENDING
        assert await session.scalar(select(AuditLog.reason).where(AuditLog.event == AuditEvent.EXECUTION_UNKNOWN)) == reason


@pytest.mark.parametrize(("placed", "available", "exposure", "final"), [(False, "10000.00", "0.00", LedgerStatus.REJECTED), (True, "9750.00", "250.00", LedgerStatus.PENDING)])
@pytest.mark.asyncio
async def test_reconciliation_resolves_an_unconfirmed_order(
    sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, placed: bool, available: str, exposure: str, final: LedgerStatus
, wire: Wire) -> None:
    receipt = await TradeExecutor(sessions, redis, settings, Bookmaker(fail(httpx.ReadTimeout)).gateway(wire)).execute(opened, order(stake="250.00"))
    async with sessions() as session:
        entry = await reconcile(session, settings, receipt.ledger_id, placed=placed, remote_bet_id="BK-9" if placed else None, actor=None)
        await session.commit()
        assert entry.status is final and not entry.reconcile_required
        await verify_account(session, opened)
    assert await balances(sessions, opened) == (D(available), D(exposure), D("10000.00"))


@pytest.mark.asyncio
async def test_a_double_click_executes_once(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, wire: Wire) -> None:
    book = Bookmaker(status(200, {"bet_id": "BK-1"}))
    executor = TradeExecutor(sessions, redis, settings, book.gateway(wire))
    request = order()
    results = await asyncio.gather(executor.execute(opened, request), executor.execute(opened, request), return_exceptions=True)
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 1
    refused = [r for r in results if isinstance(r, CfoError)]
    assert len(refused) == 1 and refused[0].reason == "DUPLICATE_REQUEST" and refused[0].status_code == 409
    assert len(book.calls) == 1
    assert await balances(sessions, opened) == (D("9750.00"), D("250.00"), D("10000.00"))
    assert 0 < await redis.ttl(f"{settings.CFO_IDEMPOTENCY_KEY_PREFIX}:{request.idempotency_key}") <= 60


@pytest.mark.asyncio
async def test_stake_caps_from_the_control_panel_bind_executions(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings) -> None:
    executor = TradeExecutor(sessions, redis, settings, PaperBookmaker())
    with pytest.raises(RiskGuardViolation) as caught:
        await executor.execute(opened, order(stake="500.01"))  # 5% of ₹10,000 is the ceiling
    assert caught.value.reason == "STAKE_ABOVE_CAP"
    assert (await executor.execute(opened, order(stake="500.00"))).status == "EXECUTED"


# ---------------------------------------------------------------- the signal's TTL
@pytest.mark.asyncio
async def test_expired_or_moved_signals_never_execute(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings) -> None:
    executor = TradeExecutor(sessions, redis, settings, PaperBookmaker())
    now = datetime.now(UTC)
    with pytest.raises(CfoError) as expired:
        await executor.execute(opened, order(signal_expires_at=now - timedelta(seconds=1)))
    assert expired.value.reason == "SIGNAL_EXPIRED" and expired.value.status_code == 410

    with pytest.raises(CfoError) as closed:
        await executor.execute(opened, order(signal_id=uuid.uuid4()))  # no live edge for it
    assert closed.value.reason == "SIGNAL_EXPIRED"

    edge = {
        "signal_id": str(uuid.uuid4()), "fixture_id": "fx-ars-lee", "market_id": str(uuid.uuid4()), "market_type": "Match Odds",
        "selection": "HOME", "home_team": "Arsenal", "away_team": "Leeds United", "bookmaker_id": "smarkets", "source": "odds_api",
        "odds": "2.40", "true_prob": "0.43", "ev": "0.032", "ev_percent": "3.2", "full_kelly": "0.02", "devig_method": "shin",
        "overround": "0.03", "books": 5, "timestamp": now.isoformat(), "expires_at": (now + SIGNAL_TTL).isoformat(),
    }
    await redis.hset("test_arya:active", "fx-ars-lee|HOME", json.dumps(edge))
    with pytest.raises(CfoError) as moved:
        await executor.execute(opened, order(odds="2.50", signal_id=uuid.uuid4()))
    assert moved.value.reason == "PRICE_MOVED" and moved.value.detail["current_odds"] == "2.40"
    assert (await executor.execute(opened, order(odds="2.40", signal_id=uuid.uuid4()))).status == "EXECUTED"


# ================================================================ the five pillars
@pytest.mark.asyncio
async def test_kill_switch_blocks_before_anything_else(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, wire: Wire) -> None:
    book = Bookmaker(status(200, {"bet_id": "x"}))
    executor = TradeExecutor(sessions, redis, settings, book.gateway(wire))
    await redis.set(settings.CFO_KILL_SWITCH_KEY, "true")
    with pytest.raises(RiskGuardViolation) as caught:
        await executor.execute(opened, order())
    assert caught.value.reason == "BLOCKED_BY_KILL_SWITCH" and caught.value.status_code == 403
    assert book.calls == [] and await count(sessions, PhantomLedger) == 0
    assert await count(sessions, AuditLog, AuditLog.reason == "BLOCKED_BY_KILL_SWITCH") == 1

    await redis.delete(settings.CFO_KILL_SWITCH_KEY)
    async with sessions() as session:  # the Control Panel emergency stop counts too
        session.add(SystemSettingsModel(id=SETTINGS_SINGLETON_ID, max_daily_exposure=0.0, max_stake_pct=D("5")))
        await session.commit()
    with pytest.raises(RiskGuardViolation) as halted:
        await executor.execute(opened, order())
    assert halted.value.reason == "BLOCKED_BY_KILL_SWITCH"


@pytest.mark.asyncio
async def test_daily_drawdown_counts_only_the_last_24_hours(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings) -> None:
    now = datetime.now(UTC)
    async with sessions() as session:
        session.add(ledger.audit_row(AuditEvent.SETTLED, "SETTLED_LOST", user_id=opened, pnl_inr=D("-2500.00"), created_at=now - timedelta(hours=25)))
        session.add(ledger.audit_row(AuditEvent.SETTLED, "SETTLED_LOST", user_id=opened, pnl_inr=D("-1000.00"), created_at=now - timedelta(hours=2)))
        await session.commit()
    executor = TradeExecutor(sessions, redis, settings, PaperBookmaker())
    assert (await executor.execute(opened, order(stake="10.00"))).status == "EXECUTED"  # -1000 is exactly 10% of peak: not over it

    async with sessions() as session:
        session.add(ledger.audit_row(AuditEvent.SETTLED, "SETTLED_LOST", user_id=opened, pnl_inr=D("-0.01"), created_at=now - timedelta(minutes=5)))
        await session.commit()
    with pytest.raises(RiskGuardViolation) as caught:
        await executor.execute(opened, order(stake="10.00"))
    assert caught.value.reason == "BLOCKED_BY_DRAWDOWN"


@pytest.mark.asyncio
async def test_loss_streak_counter_and_its_ledger_fallback(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings) -> None:
    executor = TradeExecutor(sessions, redis, settings, PaperBookmaker())
    key = f"{settings.CFO_STREAK_KEY_PREFIX}:{opened}"
    await redis.set(key, 5)
    with pytest.raises(RiskGuardViolation) as caught:
        await executor.execute(opened, order())
    assert caught.value.reason == "BLOCKED_BY_LOSS_STREAK"

    await redis.delete(key)  # counter lost (Redis flush): the ledger says 5 losses in a row
    async with sessions() as session:
        account = await lock_bankroll(session, opened, settings)
        for i in range(5):
            settle(session, account, await reserve(session, account, ticket(opened, stake="10.00", fixture=f"fx-{i}")), won=False)
        await session.commit()
    with pytest.raises(RiskGuardViolation) as recomputed:
        await executor.execute(opened, order())
    assert recomputed.value.reason == "BLOCKED_BY_LOSS_STREAK"
    assert await redis.get(key) == "5"


@pytest.mark.asyncio
async def test_market_exposure_counts_open_stakes_on_the_fixture(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings) -> None:
    executor = TradeExecutor(sessions, redis, settings, PaperBookmaker())
    for _ in range(2):
        await executor.execute(opened, order(stake="450.00"))  # 900 open on fx-ars-lee; the cap is 10% of 10,000
    with pytest.raises(RiskGuardViolation) as caught:
        await executor.execute(opened, order(stake="100.01"))
    assert caught.value.reason == "BLOCKED_BY_MARKET_EXPOSURE" and caught.value.status_code == 409
    assert (await executor.execute(opened, order(stake="100.00"))).status == "EXECUTED"  # exactly at the cap
    assert (await executor.execute(opened, order(stake="400.00", fixture="fx-other"))).status == "EXECUTED"  # other fixtures are separate


@pytest.mark.asyncio
async def test_exposure_is_rechecked_under_the_lock(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings) -> None:
    """A pre-lock check can be stale; the locked re-check sees what committed in between."""
    guard = RiskGuard(redis, settings)
    t = ticket(opened, stake="200.00")
    async with sessions() as session:
        report = await guard.check(session, t, await ledger.read_account(session, opened))
    async with sessions() as session:  # another execution lands between the check and the lock
        account = await lock_bankroll(session, opened, settings)
        await reserve(session, account, ticket(opened, stake="900.00"))
        await session.commit()
    async with sessions() as session:
        account = await lock_bankroll(session, opened, settings)
        with pytest.raises(RiskGuardViolation) as caught:
            await guard.recheck_locked(session, t, account, report)
    assert caught.value.reason == "BLOCKED_BY_MARKET_EXPOSURE"


@pytest.mark.asyncio
async def test_velocity_lock_on_a_swinging_price(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings) -> None:
    from app.core.live_odds import tick_history_key

    executor = TradeExecutor(sessions, redis, settings, PaperBookmaker())
    key = tick_history_key(settings, "fx-ars-lee|Match Odds|HOME")
    now = datetime.now(UTC).timestamp()
    await redis.zadd(key, {f"{now - 50:.3f}|2.50|0.4": now - 50, f"{now - 30:.3f}|2.52|0.4": now - 30, f"{now - 10:.3f}|2.49|0.4": now - 10})
    await redis.zadd(key, {f"{now - 300:.3f}|9.00|0.1": now - 300})  # outside the 60s window
    assert (await executor.execute(opened, order(stake="10.00"))).status == "EXECUTED"  # ~0.6% std/mean

    await redis.zadd(key, {f"{now - 5:.3f}|2.90|0.34": now - 5})
    with pytest.raises(RiskGuardViolation) as caught:
        await executor.execute(opened, order(stake="10.00"))
    assert caught.value.reason == "BLOCKED_BY_VELOCITY"


def test_coefficient_of_variation_is_exact() -> None:
    # mean 2.5, sample variance ((-0.1)^2 + 0.1^2) / 1 = 0.02, std = 0.141421356..., CV = 5.65685...%
    cv = coefficient_of_variation([D("2.4"), D("2.6")])
    assert cv is not None and abs(cv - D("5.656854249492380195206754897")) < D("1e-20")
    assert coefficient_of_variation([D("2.5")]) is None


@pytest.mark.asyncio
async def test_without_redis_nothing_trades(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, settings: Settings) -> None:
    with pytest.raises(RiskGuardViolation) as caught:
        await TradeExecutor(sessions, None, settings, PaperBookmaker()).execute(opened, order())
    assert caught.value.reason == "RISK_SERVICES_UNAVAILABLE" and caught.value.status_code == 503
    assert await count(sessions, PhantomLedger) == 0


# ================================================================ settlement engine
async def _bet(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID, settings: Settings, **kwargs: Any) -> uuid.UUID:
    async with sessions() as session:
        account = await lock_bankroll(session, user_id, settings)
        entry = await reserve(session, account, ticket(user_id, **kwargs))
        await session.commit()
        return entry.id


@pytest.mark.asyncio
async def test_settle_markets_pays_winners_clears_exposure_and_counts_the_streak(
    sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings
) -> None:
    await _bet(sessions, opened, settings, stake="200.00", odds="2.50", selection="HOME")
    await _bet(sessions, opened, settings, stake="100.00", odds="4.00", selection="AWAY")
    async with sessions() as session:
        session.add(MarketResult(fixture_id="fx-ars-lee", market="Match Odds", winning_selection="HOME", source="test"))
        await session.commit()

    summary = await settle_markets(sessions, redis, settings)
    assert (summary.won, summary.lost, summary.pnl) == (1, 1, D("200.00"))  # +300 - 100
    # 10,000 - 300 reserved; HOME returns 200 + 300; AWAY's 100 is gone
    assert await balances(sessions, opened) == (D("10200.00"), D("0.00"), D("10200.00"))
    assert await redis.get(f"{settings.CFO_STREAK_KEY_PREFIX}:{opened}") == "1"  # win reset it, then one loss
    async with sessions() as session:
        await verify_account(session, opened)
        pnl = await session.scalar(select(func.sum(AuditLog.pnl_inr)).where(AuditLog.event == AuditEvent.SETTLED))
    assert pnl == D("200.00")

    again = await settle_markets(sessions, redis, settings)  # idempotent: nothing pending is left
    assert (again.won, again.lost, again.users) == (0, 0, 0)
    assert await balances(sessions, opened) == (D("10200.00"), D("0.00"), D("10200.00"))


@pytest.mark.asyncio
async def test_void_markets_return_stakes_and_unconfirmed_bets_wait(
    sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings
) -> None:
    await _bet(sessions, opened, settings, stake="200.00", fixture="fx-void")
    unconfirmed = await _bet(sessions, opened, settings, stake="50.00", fixture="fx-held")
    async with sessions() as session:
        entry = await session.get(PhantomLedger, unconfirmed)
        assert entry is not None
        entry.reconcile_required = True
        session.add_all(
            [
                MarketResult(fixture_id="fx-void", market="Match Odds", is_void=True, source="test"),
                MarketResult(fixture_id="fx-held", market="Match Odds", winning_selection="HOME", source="test"),
            ]
        )
        await session.commit()
    summary = await settle_markets(sessions, redis, settings)
    assert (summary.void, summary.won) == (1, 0)
    assert await balances(sessions, opened) == (D("9950.00"), D("50.00"), D("10000.00"))  # the unconfirmed 50 is never paid blind


# ================================================================ PostgreSQL row locks
@pytest.mark.skipif(not TEST_POSTGRES_URL, reason="set TEST_POSTGRES_URL to a disposable PostgreSQL database")
@pytest.mark.asyncio
async def test_for_update_nowait_refuses_a_second_locker_on_postgres(settings: Settings) -> None:
    engine = create_async_engine(TEST_POSTGRES_URL or "", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=TABLES))
    pg = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with pg() as session:
            user = User(username=f"pg_{uuid.uuid4().hex[:6]}", hashed_password="x")
            session.add(user)
            await session.commit()
        async with pg() as holder, pg() as contender:
            await lock_bankroll(holder, user.id, settings)  # opens and locks the row
            await holder.commit()
            await lock_bankroll(holder, user.id, settings)  # held: an execution mid-bookmaker-call
            with pytest.raises(BankrollLockedError) as caught:
                await lock_bankroll(contender, user.id, settings)
            assert caught.value.reason == "BANKROLL_LOCKED" and caught.value.status_code == 409
            await holder.rollback()
            await contender.rollback()
            assert await lock_bankroll(contender, user.id, settings) is not None  # released: it locks now
            await contender.rollback()
        async with pg() as session:  # the journal refuses edits at the database level too
            entry = await session.scalar(select(LedgerEntry).where(LedgerEntry.user_id == user.id))
            assert entry is not None
            with pytest.raises(Exception, match="append-only"):
                await session.execute(LedgerEntry.__table__.update().where(LedgerEntry.id == entry.id).values(amount=D("1")))
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(TABLES))))
        await engine.dispose()


# ================================================================ steam moves (EMA momentum)
T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)


def test_ema_alpha_is_two_over_n_plus_one() -> None:
    with localcontext() as ctx:
        ctx.prec = 34  # the engine's precision
        assert ema_alpha(12) == D(2) / D(13)


def test_ema_math_by_hand() -> None:
    with localcontext() as ctx:
        ctx.prec = 34
        alpha = D(2) / D(13)
        expected = alpha * D("0.46") + (1 - alpha) * D("0.40")  # 0.4092307692...
    state, ref = update_ema(None, D("0.40"), T0)
    assert ref is None and state.base is None  # no history yet: nothing to judge against
    state, ref = update_ema(state, D("0.46"), T0 + timedelta(seconds=5))
    assert ref == D("0.40") and state.base == D("0.40")
    state, ref = update_ema(state, D("0.52"), T0 + timedelta(seconds=10))
    assert ref is not None and abs(ref - expected) < D("1e-30")
    assert is_steam_move(D("0.52"), ref)  # +27% over its EMA


def test_ema_does_not_compound_within_a_period_and_holds_through_gaps() -> None:
    state, _ = update_ema(None, D("0.40"), T0)
    state, _ = update_ema(state, D("0.40"), T0 + timedelta(seconds=5))
    same, ref_a = update_ema(state, D("0.45"), T0 + timedelta(seconds=6))
    same, ref_b = update_ema(same, D("0.47"), T0 + timedelta(seconds=9))
    assert ref_a == ref_b == D("0.40") and same.value == D("0.47")  # two ticks, one period: one base
    later, ref = update_ema(state, D("0.40"), T0 + timedelta(minutes=10))  # silent periods hold 0.40
    assert ref == D("0.40")
    stale, ref = update_ema(later, D("0.99"), T0)  # an out-of-order tick never rewrites history
    assert stale == later and ref is None


@pytest.mark.parametrize(("value", "steam"), [(D("0.43"), True), (D("0.42"), False), (D("0.38"), False)])
def test_steam_threshold_is_five_percent_upward(value: Decimal, steam: bool) -> None:
    assert is_steam_move(value, D("0.40")) is steam  # 0.42 is exactly +5%: not more than it


def test_ema_state_round_trips_through_redis_text() -> None:
    state = EmaState(361_000_123, D("0.4092307692307692307692307692"), D("0.52"))
    assert EmaState.decode(state.encode()) == state
    assert EmaState.decode(EmaState(1, None, D("0.5")).encode()) == EmaState(1, None, D("0.5"))
    assert EmaState.decode("garbage") is None


def test_edges_on_a_steam_move_are_flagged() -> None:
    now = T0 + timedelta(minutes=5)
    books = (
        BookLine("odds_api", "pinnacle", {"HOME": D("2.00"), "AWAY": D("2.00")}, now),
        BookLine("odds_api", "betfair", {"HOME": D("1.98"), "AWAY": D("2.02")}, now),
        BookLine("odds_api", "softbook", {"HOME": D("2.12"), "AWAY": D("1.85")}, now),
    )
    state = MarketState("fx-1", "Match Odds", "Arsenal", "Leeds United", books, "soccer_epl", now + timedelta(hours=2))
    previous = int(now.timestamp() // 5) - 1
    calm = {"HOME": EmaState(previous, D("0.50"), D("0.50")), "AWAY": EmaState(previous, D("0.50"), D("0.50"))}
    shifted = {"HOME": EmaState(previous, D("0.44"), D("0.44")), "AWAY": EmaState(previous, D("0.56"), D("0.56"))}
    for ema, steam in ((calm, False), (shifted, True)):
        result = evaluate_market(state, now=now, line_max_age=timedelta(seconds=90), book_max_age=timedelta(seconds=300), ema=ema)
        home = [e for e in result.edges if e.selection == "HOME"]
        assert home and home[0].is_steam_move is steam
        assert result.ema is not None and result.ema["HOME"].bucket == previous + 1


# ================================================================ the API
@pytest.mark.asyncio
async def test_execute_trade_endpoint(sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, wire: Wire) -> None:
    from fastapi import FastAPI

    from app.api.deps import get_current_user
    from app.api.v1 import cfo_execution

    app = FastAPI()
    app.include_router(cfo_execution.router, prefix="/api/v1")
    app.state.redis = redis
    app.state.bookmaker = Bookmaker(status(500)).gateway(wire)
    async with sessions() as session:
        user = await session.get(User, opened)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[cfo_execution.get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings

    body = {"idempotency_key": str(uuid.uuid4()), "fixture_id": "fx-ars-lee", "selection": "HOME", "bookmaker_id": "smarkets", "odds": "2.50", "stake_inr": "250.00"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        failed = await client.post("/api/v1/omni/execute-trade", json=body)
        assert failed.status_code == 502 and failed.json()["detail"]["reason"] == "BOOKMAKER_HTTP_500"
        again = await client.post("/api/v1/omni/execute-trade", json=body)  # same key within 60s: a duplicate click
        assert again.status_code == 409 and again.json()["detail"]["reason"] == "DUPLICATE_REQUEST"

        app.state.bookmaker = PaperBookmaker()
        ok = await client.post("/api/v1/omni/execute-trade", json={**body, "idempotency_key": str(uuid.uuid4())})
        assert ok.status_code == 200 and ok.json()["status"] == "EXECUTED" and ok.json()["exposure_balance"] == 250.0

        not_v4 = await client.post("/api/v1/omni/execute-trade", json={**body, "idempotency_key": str(uuid.uuid1())})
        assert not_v4.status_code == 422
        sub_paisa = await client.post("/api/v1/omni/execute-trade", json={**body, "idempotency_key": str(uuid.uuid4()), "stake_inr": "10.001"})
        assert sub_paisa.status_code == 422

        bank = (await client.get("/api/v1/omni/bankroll")).json()
        assert bank["available_balance"] == 9750.0 and bank["exposure_balance"] == 250.0 and len(bank["open_positions"]) == 1
        assert bank["kill_switch"] is False and bank["loss_streak"] == 0

        saved = await client.put("/api/v1/omni/risk-settings", json={"daily_drawdown_pct": "7.5", "max_loss_streak": 3})
        assert saved.status_code == 200 and saved.json()["daily_drawdown_pct"] == 7.5 and saved.json()["max_loss_streak"] == 3
        assert (await client.put("/api/v1/omni/risk-settings", json={"max_market_exposure_pct": "60"})).status_code == 422


@pytest.mark.asyncio
async def test_simultaneous_orders_queue_behind_the_bankroll_lock(
    sessions: async_sessionmaker[AsyncSession], opened: uuid.UUID, redis: Redis, settings: Settings, wire: Wire, request: pytest.FixtureRequest
) -> None:
    """Group 63: three edges hit at once for one user. FOR UPDATE NOWAIT refuses the losers at the
    database, and the executor queues them micro-sequentially instead of failing them."""
    if request.node.callspec.params.get("sessions") != "postgres":
        pytest.skip("row locks need PostgreSQL (TEST_POSTGRES_URL)")

    async def slow(request_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.25)  # the bookmaker takes a moment: the bankroll row is held meanwhile
        return httpx.Response(200, json={"remote_bet_id": f"BK-{uuid.uuid4().hex[:6]}", "matched_odds": "2.50"})

    gateway = SniperGateway(wire.sessions, wire.redis, wire.settings, wire.vault, httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    executor = TradeExecutor(sessions, redis, settings, gateway)
    receipts = await asyncio.gather(
        executor.execute(opened, order(stake="100.00")),
        executor.execute(opened, order(stake="100.00", fixture="fx-other")),
        executor.execute(opened, order(stake="100.00")),
    )
    assert [r.status for r in receipts] == ["EXECUTED"] * 3
    assert await balances(sessions, opened) == (D("9700.00"), D("300.00"), D("10000.00"))
