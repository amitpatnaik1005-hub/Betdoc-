"""Group 64: legging risk. Multi-leg arbitrages and hedges through the CFO ledger and the Omni-Sniper.

The brief's proof is the first live-path test: Leg A asks for ₹10,000 at Betfair, the bookmaker's
API answers that only ₹4,000 matched, and Leg B (Pinnacle) goes out at ₹4,000, re-sized *before*
it fires, with the unmatched ₹6,000 back in AVAILABLE. Then: Leg A failing (or matching nothing,
or going unconfirmed) never fires Leg B; pre-flight finds a blocked leg before anything is live;
hedges are only held to the kill switch when they cut risk; a hedge leg's partial fill re-solves
the next one; the 5-per-second publisher reads PostgreSQL only when positions change.

The ledger runs on SQLite, and again on PostgreSQL when ``TEST_POSTGRES_URL`` is set. Redis is a
real server on a dedicated, flushed index (skipped when none is reachable).
"""

from __future__ import annotations

import asyncio
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
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import Settings, get_settings
from app.core.security_vault import VaultCrypto
from app.domain.math.arbitrage_calc import commission_adjusted_odds, raw_odds_for
from app.models import BetLedger, ExchangeAccount, RiskMandate, User
from app.models.cfo_vault import AuditLog, BankrollAccount, LedgerEntry, MarketResult, PhantomLedger, RiskGuardSettings
from app.models.hive_bots import TradingBot
from app.models.control_panel import SystemSettingsModel
from app.models.execution import EntityMapping, ExecutionVenue
from app.schemas.cfo_vault import ExecuteTradeRequest
from app.schemas.portfolio import ArbitrageExecuteRequest, ArbitrageLegRequest, HedgeExecuteRequest
from app.services import portfolio_positions
from app.services.bookmaker_gateway import BookmakerOrder, BookmakerOutcome, BookmakerResult
from app.services.cfo_execution import TradeExecutor
from app.services.cfo_ledger import CfoError, lock_bankroll, verify_account
from app.services.fx_rates import FxRates
from app.services.leg_executor import LegExecutor, leg_key
from app.services.portfolio_manager import PortfolioManager
from app.services.portfolio_positions import mark_positions_dirty
from app.services.portfolio_stream import PortfolioKeys, PortfolioPublisher, watch
from app.services.sniper import SniperGateway

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
FIXTURE = "fx-ars-lee"
BANK = D("1000000.00")  # ₹10 lakh: the 5% stake cap is ₹50,000 and the 10% fixture cap ₹1,00,000


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and "~" in str(c.sqltext)]:
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
            "starting_bankroll": float(BANK),
            "CFO_KILL_SWITCH_KEY": "test:kill_switch",
            "CFO_STREAK_KEY_PREFIX": "test:risk:streak",
            "CFO_IDEMPOTENCY_KEY_PREFIX": "test:idempotency",
            "ARYABHATA_PREFIX": "test_arya",
            "SNIPER_PREFIX": "test_sniper",
            "PORTFOLIO_CHANNEL_PREFIX": "test:live_portfolio",
            "FX_RATES_KEY": "test:fx:rates",
            "CFO_EXECUTION_MODE": "paper",
            "ARB_MIN_MARGIN_PCT": 0.1,
            "BOOKMAKER_CURRENCIES": {"betfair_ex_uk": "INR"},  # this user's Betfair account is in rupees
        }
    )


@pytest_asyncio.fixture
async def user_id(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> uuid.UUID:
    async with sessions() as session:
        user = User(username=f"legs_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(user)
        await session.commit()
        await lock_bankroll(session, user.id, settings)  # opens the ₹10 lakh account
        await session.commit()
        return user.id


async def put_book(redis: Redis, settings: Settings, bookmaker: str, prices: dict[str, str], fixture: str = FIXTURE, age: float = 2.0) -> None:
    """One bookmaker's live prices in the shape Aryabhata keeps them."""
    value = json.dumps({"s": "odds_api", "b": bookmaker, "p": prices, "t": time.time() - age, "x": False})
    await redis.hset(f"{settings.ARYABHATA_PREFIX}:books:{fixture}|Match Odds", f"odds_api|{bookmaker}", value)


async def arb_books(redis: Redis, settings: Settings) -> None:
    """Betfair HOME 2.20 (5%: true 2.14) against Pinnacle AWAY 2.14 (0%): 2 / 2.14 = 0.934579.
    ₹20,000 splits exactly ₹10,000 / ₹10,000."""
    await put_book(redis, settings, "betfair_ex_uk", {"HOME": "2.20", "AWAY": "1.70"})
    await put_book(redis, settings, "pinnacle", {"HOME": "1.95", "AWAY": "2.14"})


def arb_request(key: uuid.UUID | None = None, home: str = "2.20", away: str = "2.14", total: str = "20000.00") -> ArbitrageExecuteRequest:
    return ArbitrageExecuteRequest(
        idempotency_key=key or uuid.uuid4(),
        fixture_id=FIXTURE,
        total_stake_inr=D(total),
        legs=[ArbitrageLegRequest(selection="HOME", bookmaker_id="betfair_ex_uk", odds=D(home)), ArbitrageLegRequest(selection="AWAY", bookmaker_id="pinnacle", odds=D(away))],
    )


class Scripted:
    """A ``BookmakerGateway`` that records every order and answers per selection from a script."""

    def __init__(self, **script: Callable[[BookmakerOrder], BookmakerResult]) -> None:
        self.script = script
        self.orders: list[BookmakerOrder] = []

    async def prepare(self, order: BookmakerOrder) -> None:
        return None

    async def place(self, order: BookmakerOrder, route: Any = None) -> BookmakerResult:  # noqa: ARG002
        self.orders.append(order)
        return self.script.get(order.selection, fills())(order)


def fills(matched: str | None = None) -> Callable[[BookmakerOrder], BookmakerResult]:
    def answer(order: BookmakerOrder) -> BookmakerResult:
        filled = order.venue_stake if matched is None else D(matched)
        reason = "BOOKMAKER_ACCEPTED" if filled == order.venue_stake else "PARTIAL_FILL"
        return BookmakerResult(BookmakerOutcome.ACCEPTED, reason, f"R-{order.selection}-{uuid.uuid4().hex[:6]}", 200, matched_odds=order.odds, filled_stake=filled, venue_id="mock")

    return answer


def rejects(reason: str = "BOOKMAKER_HTTP_500") -> Callable[[BookmakerOrder], BookmakerResult]:
    return lambda _: BookmakerResult(BookmakerOutcome.REJECTED, reason, http_status=500, venue_id="mock")


def unknown() -> Callable[[BookmakerOrder], BookmakerResult]:
    return lambda _: BookmakerResult(BookmakerOutcome.UNKNOWN, "BOOKMAKER_TIMEOUT", venue_id="mock")


def legs_for(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, gateway: Any) -> LegExecutor:
    executor = TradeExecutor(sessions, redis, settings, gateway)
    return LegExecutor(executor, PortfolioManager(redis, sessions, settings, getattr(gateway, "venues", None)), redis, settings)


async def rows(sessions: async_sessionmaker[AsyncSession]) -> list[PhantomLedger]:
    async with sessions() as session:
        return list((await session.execute(select(PhantomLedger).order_by(PhantomLedger.created_at))).scalars())


async def bank(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    async with sessions() as session:
        account = await session.scalar(select(BankrollAccount).where(BankrollAccount.user_id == user_id))
        assert account is not None
        await verify_account(session, user_id)  # the journal still re-derives every balance
        return account.available_balance, account.exposure_balance


# ================================================================ the brief's proof, on the live path
@pytest.mark.asyncio
async def test_partial_fill_response_scales_leg_b_down_before_it_fires(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID
) -> None:
    """Leg A (Betfair, the exchange leg, fires first) asks ₹10,000. The bookmaker's API answers
    ``matched_stake: 4000.00``. Leg B (Pinnacle) must go out at ₹4,000, not ₹10,000."""
    vault = VaultCrypto(Fernet.generate_key().decode())
    async with sessions() as session:
        for venue_id in ("betfair_ex_uk", "pinnacle"):
            session.add(
                ExecutionVenue(
                    id=venue_id, display_name=venue_id, adapter="generic_json", base_url=f"https://{venue_id}.example/api", auth_type="static_bearer",
                    place_path="/bets", status_path="/bets", bets_per_second=D("50"), burst=50, routes=[], selection_codes={"HOME": "1", "AWAY": "2"},
                    encrypted_credentials=vault.encrypt_key(json.dumps({"api_key": f"key-{venue_id}-0001"})), is_enabled=True, is_sandbox=False,
                )
            )
        await session.flush()
        for venue_id in ("betfair_ex_uk", "pinnacle"):
            session.add(EntityMapping(venue_id=venue_id, kind="fixture", canonical_key=FIXTURE, remote_id=f"EV-{venue_id}", source="manual", detail={}))
        await session.commit()

    calls: list[dict[str, Any]] = []

    def bookmaker(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"host": request.url.host, **body})
        if request.url.host.startswith("betfair"):
            return httpx.Response(200, json={"remote_bet_id": "BF-1001", "status": "OPEN", "matched_odds": "2.20", "matched_stake": "4000.00"})
        return httpx.Response(200, json={"remote_bet_id": "PN-2002", "status": "OPEN", "matched_odds": body["odds"]})

    await arb_books(redis, settings)
    live = settings.model_copy(update={"CFO_EXECUTION_MODE": "live"})
    gateway = SniperGateway(sessions, redis, live, vault, httpx.AsyncClient(transport=httpx.MockTransport(bookmaker)))
    receipt = await legs_for(sessions, redis, live, gateway).execute_arbitrage(user_id, arb_request())

    assert [(c["host"], c["stake"]) for c in calls] == [("betfair_ex_uk.example", "10000.00"), ("pinnacle.example", "4000.00")]
    assert calls[0]["time_in_force"] == "IOC"  # the unmatched ₹6,000 lapses at the exchange, it never sits in the book
    assert receipt.status == "COMPLETE"
    a, b = receipt.legs
    assert (a.status, a.planned_stake_inr, a.filled_stake_inr) == ("PARTIAL", D("10000.00"), D("4000.00"))
    assert (b.status, b.planned_stake_inr, b.requested_stake_inr, b.filled_stake_inr) == ("FILLED", D("10000.00"), D("4000.00"), D("4000.00"))
    # Both outcomes still pay the same: 4,000 * 2.14 - 8,000 = ₹560
    assert receipt.outcome_profits == {"HOME": D("560.00"), "AWAY": D("560.00")}

    ledger = {row.selection: row for row in await rows(sessions)}
    assert (ledger["HOME"].stake_inr, ledger["HOME"].requested_stake_inr, ledger["HOME"].remote_bet_id) == (D("4000.00"), D("10000.00"), "BF-1001")
    assert ledger["HOME"].strategy == ledger["AWAY"].strategy == "arbitrage" and ledger["HOME"].group_id == ledger["AWAY"].group_id
    assert ledger["HOME"].potential_pnl == D("4800.00")  # 4,000 * 1.20: the matched stake settles, not the asked one
    assert await bank(sessions, user_id) == (BANK - D("8000.00"), D("8000.00"))  # the unmatched ₹6,000 is back


@pytest.mark.asyncio
async def test_the_price_floors_keep_every_outcome_whole(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """Leg A's floor: its outcome must cover the legs after it once they re-size to its payout:
    1 / (1 - 1/2.14) = 1.877193 true, 1.9234 raw at 5%. Leg B's: (4,000 + 4,000) / 4,000 = 2.00."""
    await arb_books(redis, settings)
    book = Scripted(HOME=fills("4000.00"))
    await legs_for(sessions, redis, settings, book).execute_arbitrage(user_id, arb_request())
    a, b = book.orders
    assert a.min_acceptable_odds == raw_odds_for(1 / (1 - 1 / D("2.14")), D("0.05")) == D("1.9234")
    assert commission_adjusted_odds(a.min_acceptable_odds, D("0.05")) >= 1 / (1 - 1 / D("2.14"))
    assert b.min_acceptable_odds == D("2.0000") and b.stake_inr == D("4000.00")


@pytest.mark.parametrize(
    ("leg_a", "reason"),
    [(rejects(), "BOOKMAKER_HTTP_500"), (rejects("NOT_MATCHED"), "NOT_MATCHED"), (rejects("SLIPPAGE_REJECTED"), "SLIPPAGE_REJECTED")],
)
@pytest.mark.asyncio
async def test_leg_a_failing_never_fires_leg_b(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, leg_a: Any, reason: str
) -> None:
    await arb_books(redis, settings)
    book = Scripted(HOME=leg_a)
    request = arb_request()
    receipt = await legs_for(sessions, redis, settings, book).execute_arbitrage(user_id, request)
    assert [o.selection for o in book.orders] == ["HOME"]  # Leg B never left
    assert receipt.status == "ABORTED" and receipt.legs[0].reason == reason
    assert receipt.legs[1].status == "ABORTED" and receipt.legs[1].reason == "ARBITRAGE_LEG_ABORTED"
    assert await rows(sessions) == [] and await bank(sessions, user_id) == (BANK, D("0.00"))
    async with sessions() as session:
        aborted = await session.scalar(select(AuditLog).where(AuditLog.reason == "ARBITRAGE_LEG_ABORTED"))
    assert aborted is not None and aborted.idempotency_key == leg_key(request.idempotency_key, "AWAY")


@pytest.mark.asyncio
async def test_a_zero_match_response_is_a_rejection(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """``matched_stake: 0`` from an immediate-or-cancel order: nothing exists, so Leg A rolls back
    (adapter level: the generic JSON adapter classifies it before the ledger sees it)."""
    from app.adapters.execution.factory import GenericJsonExecutionAdapter
    from app.adapters.execution.venue import VenueConfig
    from app.services.id_mapper import RemoteIds

    class Sessions:
        async def bearer(self, venue: Any, stale: str | None = None) -> str:  # noqa: ARG002
            return "token"

    class Limiter:
        async def acquire(self, *_: Any) -> float:
            return 0.0

    answers = iter([{"remote_bet_id": "X1", "matched_stake": "0"}, {"remote_bet_id": "X2", "matched_stake": "12000"}, {"remote_bet_id": "X3", "matched_stake": "2500.50"}])
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(answers))))
    venue = VenueConfig(id="v", display_name="V", adapter="generic_json", base_url="https://v.example", auth_type="static_bearer", place_path="/bets", status_path="/bets", bets_per_second=D("5"), burst=5)
    adapter = GenericJsonExecutionAdapter(venue, http=http, sessions=Sessions(), limiter=Limiter(), settings=settings)  # type: ignore[arg-type]
    order = BookmakerOrder("ref", "v", FIXTURE, "Match Odds", "HOME", D("2.2"), D("10000"), D("2.0"))
    remote = RemoteIds("EV", "EV-1")
    lapsed, over, partial = [await adapter.place(order, remote) for _ in range(3)]
    assert (lapsed.outcome, lapsed.reason) == (BookmakerOutcome.REJECTED, "NOT_MATCHED")
    assert (over.outcome, over.reason) == (BookmakerOutcome.UNKNOWN, "FILL_EXCEEDS_REQUEST")  # more matched than reserved: a person checks it
    assert (partial.outcome, partial.reason, partial.filled_stake) == (BookmakerOutcome.ACCEPTED, "PARTIAL_FILL", D("2500.50"))


@pytest.mark.asyncio
async def test_an_unconfirmed_leg_a_stops_the_sequence(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    await arb_books(redis, settings)
    book = Scripted(HOME=unknown())
    receipt = await legs_for(sessions, redis, settings, book).execute_arbitrage(user_id, arb_request())
    assert [o.selection for o in book.orders] == ["HOME"]
    assert receipt.status == "LEGGED" and receipt.legs[0].status == "UNCONFIRMED" and receipt.legs[1].status == "ABORTED"
    (entry,) = await rows(sessions)
    assert entry.reconcile_required and entry.stake_inr == D("10000.00")  # the stake stays in exposure until resolved


@pytest.mark.asyncio
async def test_leg_b_failing_after_leg_a_is_reported_as_legged(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    await arb_books(redis, settings)
    receipt = await legs_for(sessions, redis, settings, Scripted(AWAY=rejects())).execute_arbitrage(user_id, arb_request())
    assert receipt.status == "LEGGED" and [leg.status for leg in receipt.legs] == ["FILLED", "FAILED"]
    # Leg A alone: HOME pays 10,000 * 2.14 - 10,000 = ₹11,400, AWAY loses the ₹10,000
    assert receipt.outcome_profits == {"HOME": D("11400.00"), "AWAY": D("-10000.00")} and receipt.worst_case == D("-10000.00")


@pytest.mark.asyncio
async def test_a_foreign_currency_leg_is_staked_in_its_own_currency(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """unibet_uk settles in GBP (test rate ₹110.00, 0.5% haircut on the way home): HOME 2.30 there is
    2.2885 in rupee terms; AWAY 2.10 at Pinnacle. 1/2.2885 + 1/2.10 = 0.913161. The GBP leg goes
    out in pounds, the ledger reserves its cost in whole paise and keeps the pound stake."""
    await FxRates(redis, settings).publish("GBP", D("110.00"), "test")
    await put_book(redis, settings, "unibet_uk", {"HOME": "2.30", "AWAY": "1.60"})
    await put_book(redis, settings, "pinnacle", {"HOME": "1.95", "AWAY": "2.10"})
    book = Scripted()
    request = ArbitrageExecuteRequest(
        idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, total_stake_inr=D("5000.00"),
        legs=[ArbitrageLegRequest(selection="HOME", bookmaker_id="unibet_uk", odds=D("2.30")), ArbitrageLegRequest(selection="AWAY", bookmaker_id="pinnacle", odds=D("2.10"))],
    )
    receipt = await legs_for(sessions, redis, settings, book).execute_arbitrage(user_id, request)
    assert receipt.status == "COMPLETE" and receipt.worst_case > 0
    uk = next(o for o in book.orders if o.selection == "HOME")
    assert uk.currency == "GBP" and uk.stake is not None and uk.stake == uk.stake.quantize(D("0.01"))
    assert uk.stake_inr == (uk.stake * D("110")).quantize(D("0.01"), rounding="ROUND_UP")
    row = next(r for r in await rows(sessions) if r.selection == "HOME")
    assert (row.currency, row.stake_ccy, row.stake_inr) == ("GBP", uk.stake, uk.stake_inr)
    # The HOME outcome pays from the pounds' exact value, through the haircut
    paid = uk.stake * D("110") * D("2.30") * D("0.995")
    assert receipt.outcome_profits["HOME"] == (paid - receipt.total_staked_inr).quantize(D("0.01"), rounding="ROUND_DOWN")


@pytest.mark.asyncio
async def test_a_betslip_order_to_a_foreign_venue_is_converted_at_the_live_rate(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID
) -> None:
    """unibet_uk's account is in pounds. With no live GBP rate the order is refused before anything
    is reserved. At ₹117.60: ₹1,000 buys £8.50 (8.5034 rounded down), which costs ₹999.60, and that
    is what goes out (in GBP) and what the ledger reserves."""
    vault = VaultCrypto(Fernet.generate_key().decode())
    async with sessions() as session:
        session.add(
            ExecutionVenue(
                id="unibet_uk", display_name="Unibet UK", adapter="generic_json", base_url="https://unibet.example/api", auth_type="static_bearer",
                place_path="/bets", status_path="/bets", bets_per_second=D("50"), burst=50, routes=[], selection_codes={"HOME": "1", "AWAY": "2"},
                encrypted_credentials=vault.encrypt_key(json.dumps({"api_key": "key-unibet-0001"})), is_enabled=True, is_sandbox=False,
            )
        )
        await session.flush()
        session.add(EntityMapping(venue_id="unibet_uk", kind="fixture", canonical_key=FIXTURE, remote_id="EV-UK", source="manual", detail={}))
        await session.commit()
    calls: list[dict[str, Any]] = []

    def bookmaker(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"remote_bet_id": "UK-1", "status": "OPEN", "matched_odds": body["odds"]})

    executor = TradeExecutor(sessions, redis, settings, SniperGateway(sessions, redis, settings, vault, httpx.AsyncClient(transport=httpx.MockTransport(bookmaker))))

    def order() -> ExecuteTradeRequest:
        return ExecuteTradeRequest(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, selection="HOME", bookmaker_id="unibet_uk", odds=D("2.30"), stake_inr=D("1000.00"))

    with pytest.raises(CfoError) as no_rate:
        await executor.execute(user_id, order())
    assert no_rate.value.reason == "FX_UNAVAILABLE" and calls == [] and await rows(sessions) == []

    await FxRates(redis, settings).publish("GBP", D("117.60"), "test")
    receipt = await executor.execute(user_id, order())
    assert (calls[0]["stake"], calls[0]["currency"]) == ("8.50", "GBP")
    assert (receipt.stake_inr, receipt.stake_ccy) == (D("999.60"), D("8.50"))
    (row,) = await rows(sessions)
    assert (row.currency, row.stake_ccy, row.stake_inr) == ("GBP", D("8.50"), D("999.60"))
    assert await bank(sessions, user_id) == (BANK - D("999.60"), D("999.60"))


# ================================================================ pre-flight: nothing fires
@pytest.mark.asyncio
async def test_pre_flight_refusals_fire_nothing(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    await arb_books(redis, settings)
    book = Scripted()
    legs = legs_for(sessions, redis, settings, book)

    with pytest.raises(CfoError) as moved:  # Pinnacle no longer offers 2.20
        await legs.execute_arbitrage(user_id, arb_request(away="2.20"))
    assert moved.value.reason == "PRICE_MOVED" and moved.value.detail["current_odds"] == "2.1400"

    # Each leg alone (₹10,000) fits a 1% fixture cap of ₹10,000; both together do not
    async with sessions() as session:
        session.add(RiskGuardSettings(user_id=user_id, max_market_exposure_pct=D("1.00")))
        await session.commit()
    with pytest.raises(CfoError) as capped:
        await legs.execute_arbitrage(user_id, arb_request())
    assert capped.value.reason == "BLOCKED_BY_MARKET_EXPOSURE"

    await put_book(redis, settings, "pinnacle", {"HOME": "1.95", "DRAW": "3.40", "AWAY": "2.14"})
    with pytest.raises(CfoError) as incomplete:  # the market has a draw now: two legs don't cover it
        await legs.execute_arbitrage(user_id, arb_request())
    assert incomplete.value.reason == "ARB_INCOMPLETE"
    assert book.orders == [] and await rows(sessions) == []


@pytest.mark.asyncio
async def test_commission_can_erase_an_arbitrage_before_it_fires(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """Raw 2.04 + 2.00 is 0.990196; Betfair's 5% makes it 1.003018. Refused, nothing placed."""
    await put_book(redis, settings, "betfair_ex_uk", {"HOME": "2.04", "AWAY": "1.70"})
    await put_book(redis, settings, "pinnacle", {"HOME": "1.95", "AWAY": "2.00"})
    book = Scripted()
    with pytest.raises(CfoError) as caught:
        await legs_for(sessions, redis, settings, book).execute_arbitrage(user_id, arb_request(home="2.04", away="2.00"))
    assert caught.value.reason == "NO_ARBITRAGE" and book.orders == []


@pytest.mark.asyncio
async def test_a_double_click_fires_the_legs_once(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    await arb_books(redis, settings)
    book = Scripted()
    legs = legs_for(sessions, redis, settings, book)
    key = uuid.uuid4()
    first, second = await asyncio.gather(legs.execute_arbitrage(user_id, arb_request(key)), legs.execute_arbitrage(user_id, arb_request(key)), return_exceptions=True)
    outcomes = sorted([type(first).__name__, type(second).__name__])
    assert outcomes == ["DuplicateExecutionError", "MultiLegReceipt"]
    assert len(book.orders) == 2


# ================================================================ hedges
async def back_bet(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, stake: str, odds: str, selection: str = "HOME") -> None:
    request = ExecuteTradeRequest(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, selection=selection, bookmaker_id="pinnacle", odds=D(odds), stake_inr=D(stake))
    await TradeExecutor(sessions, redis, settings, Scripted()).execute(user_id, request)


@pytest.mark.asyncio
async def test_a_balanced_hedge_cuts_risk_so_only_the_kill_switch_binds(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """₹40,000 on HOME @ 3.00; AWAY now 2.50. The hedge is ₹48,000 on AWAY: above the 1% fixture
    cap and the 5% stake cap, and still allowed, because it takes the worst case from -₹40,000 to
    +₹32,000 (40,000 * (3 * 0.6 - 1)). The kill switch still stops it."""
    await back_bet(sessions, redis, settings, user_id, "40000.00", "3.00")
    await put_book(redis, settings, "pinnacle", {"HOME": "2.80", "AWAY": "2.50"})
    async with sessions() as session:
        session.add(RiskGuardSettings(user_id=user_id, max_market_exposure_pct=D("1.00")))
        await session.commit()
    book = Scripted()
    legs = legs_for(sessions, redis, settings, book)

    await redis.set(settings.CFO_KILL_SWITCH_KEY, "1")
    with pytest.raises(CfoError) as halted:
        await legs.execute_hedge(user_id, HedgeExecuteRequest(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, fraction=D("1")))
    assert halted.value.reason == "BLOCKED_BY_KILL_SWITCH" and book.orders == []
    await redis.delete(settings.CFO_KILL_SWITCH_KEY)

    receipt = await legs.execute_hedge(user_id, HedgeExecuteRequest(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, fraction=D("1")))
    assert receipt.status == "COMPLETE" and [(o.selection, o.stake_inr) for o in book.orders] == [("AWAY", D("48000.00"))]
    assert receipt.outcome_profits == {"HOME": D("32000.00"), "AWAY": D("32000.00")}
    hedge_row = next(row for row in await rows(sessions) if row.selection == "AWAY")
    assert hedge_row.strategy == "hedge"


@pytest.mark.asyncio
async def test_a_free_bet_hedge_loses_nothing_if_the_bet_fails(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    await back_bet(sessions, redis, settings, user_id, "10000.00", "3.00")
    await put_book(redis, settings, "pinnacle", {"HOME": "2.80", "AWAY": "2.50"})
    receipt = await legs_for(sessions, redis, settings, Scripted()).execute_hedge(user_id, HedgeExecuteRequest(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, fraction=D("0")))
    assert receipt.status == "COMPLETE"
    assert D("0") <= receipt.outcome_profits["AWAY"] <= D("0.05")  # exactly nothing lost, give or take a rounded-up paisa
    assert receipt.outcome_profits["HOME"] == D("13333.32")  # 30,000 - 10,000 - 6,666.68 (the AWAY stake, rounded up)


@pytest.mark.asyncio
async def test_a_partial_fill_on_a_hedge_leg_resizes_the_next(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    """3-way free bet on ₹5,000 HOME @ 4.00 with DRAW 3.60 and AWAY 2.80: AWAY ₹4,891.31 and DRAW
    ₹3,804.35. AWAY (the bigger leg) fires first and half fills; DRAW is re-solved against the new
    book (₹2,863.73) so the DRAW outcome still loses nothing. AWAY's shortfall is reported: LEGGED."""
    await back_bet(sessions, redis, settings, user_id, "5000.00", "4.00")
    await put_book(redis, settings, "pinnacle", {"HOME": "3.90", "DRAW": "3.60", "AWAY": "2.80"})
    book = Scripted(AWAY=fills("2445.65"))
    receipt = await legs_for(sessions, redis, settings, book).execute_hedge(user_id, HedgeExecuteRequest(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, fraction=D("0")))
    away, draw = book.orders
    b = 1 / D("3.6") + 1 / D("2.8")
    planned_away = (D("5000") + D("5000") * b / (1 - b)) / D("2.8")  # (S + H) / E, H = S * B / (1 - B)
    assert away.selection == "AWAY" and abs(away.stake_inr - planned_away) <= D("0.05")
    # Re-solved with AWAY traded: DRAW must take the book's DRAW outcome (-7,445.65 now) back to zero
    after_away = D("-5000") - D("2445.65")
    resolved_draw = -after_away / (D("3.6") - 1)
    assert draw.selection == "DRAW" and abs(draw.stake_inr - resolved_draw) <= D("0.05") and draw.stake_inr < D("3804.35")
    assert receipt.outcome_profits["DRAW"] >= 0
    assert receipt.status == "LEGGED" and receipt.outcome_profits["AWAY"] < 0


@pytest.mark.asyncio
async def test_a_hedge_that_moved_since_the_modal_is_reconfirmed(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    from app.schemas.portfolio import HedgeLegExpectation

    await back_bet(sessions, redis, settings, user_id, "1000.00", "3.00")
    await put_book(redis, settings, "pinnacle", {"HOME": "2.80", "AWAY": "2.40"})  # the modal showed 2.50
    shown = [HedgeLegExpectation(selection="AWAY", bookmaker_id="pinnacle", odds=D("2.50"), stake_inr=D("1200.00"))]
    book = Scripted()
    with pytest.raises(CfoError) as caught:
        await legs_for(sessions, redis, settings, book).execute_hedge(
            user_id, HedgeExecuteRequest(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, fraction=D("1"), expected_legs=shown)
        )
    assert caught.value.reason == "HEDGE_CHANGED" and caught.value.detail["legs"][0]["odds"] == "2.4000" and book.orders == []


# ================================================================ the live portfolio stream
@pytest.mark.asyncio
async def test_the_publisher_streams_5x_a_second_without_polling_postgres(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    await back_bet(sessions, redis, settings, user_id, "1000.00", "3.00")
    await put_book(redis, settings, "pinnacle", {"HOME": "2.80", "AWAY": "2.50"})
    loads = 0
    real = portfolio_positions.load_open_bets

    async def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal loads
        loads += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(portfolio_positions, "load_open_bets", counting)
    keys = PortfolioKeys(settings)
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(keys.channel(user_id))
    publisher = PortfolioPublisher(redis, sessions, settings)
    assert await publisher.lead() and not await PortfolioPublisher(redis, sessions, settings).lead()  # one leader at a time

    assert await publisher.tick() == 0  # nobody watching: nothing computed
    await watch(redis, settings, user_id)
    for _ in range(5):
        assert await publisher.tick() == 1
    assert loads == 1  # five ticks, one ledger read: the rest came from the Redis snapshot

    messages = []
    deadline = time.monotonic() + 2
    while len(messages) < 5 and time.monotonic() < deadline:
        message = await pubsub.get_message(timeout=0.2)
        if message and message["type"] == "message":
            messages.append(json.loads(message["data"]))
    assert [m["seq"] for m in messages] == [2, 3, 4, 5, 6] or len(messages) == 5
    (market,) = messages[-1]["markets"]
    assert market["profitable"] is True and market["cash_out"] == "800.00"  # 1,000 * (3 * 0.6 - 1)
    assert json.loads(await redis.get(keys.last(user_id)))["markets"][0]["cash_out"] == "800.00"

    await mark_positions_dirty(redis, settings, user_id)  # an execution or settlement happened
    await publisher.tick()
    assert loads == 2
    await pubsub.aclose()


@pytest.mark.asyncio
async def test_portfolio_and_arbitrage_endpoints(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID) -> None:
    from fastapi import FastAPI

    from app.api.deps import get_current_user
    from app.api.v1 import cfo_execution, portfolio

    app = FastAPI()
    app.include_router(portfolio.router, prefix="/api/v1")
    app.state.redis = redis
    app.state.bookmaker = Scripted(HOME=fills("4000.00"))
    async with sessions() as session:
        user = await session.get(User, user_id)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[cfo_execution.get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    await arb_books(redis, settings)
    body = arb_request().model_dump(mode="json")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        fired = await client.post("/api/v1/omni/arbitrage/execute", json=body)
        assert fired.status_code == 200 and fired.json()["status"] == "COMPLETE"
        assert [leg["filled_stake_inr"] for leg in fired.json()["legs"]] == [4000.0, 4000.0]
        again = await client.post("/api/v1/omni/arbitrage/execute", json=body)
        assert again.status_code == 409 and again.json()["detail"]["reason"] == "DUPLICATE_REQUEST"
        view = (await client.get("/api/v1/omni/portfolio")).json()
        assert view["totals"]["open_bets"] == 2 and view["markets"][0]["worst_case"] == "560.00"
        assert (await client.put("/api/v1/omni/fx-rates", json={"currency": "GBP", "inr_per_unit": "110"})).status_code in (401, 403)
        rates = (await client.get("/api/v1/omni/fx-rates")).json()
        assert rates["rates"] == [] and rates["haircut_pct"] == settings.FX_HAIRCUT_PCT
