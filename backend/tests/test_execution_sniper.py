"""Group 63: the Omni-Sniper. Sessions that refresh themselves, ids that must map, outbound limits,
slippage floors, receipts, the order resolver and its dead-letter queue, and the sandbox venue.

The headline proof (the brief's): a ``401 Unauthorized`` mid-shot pauses the order, the session
manager calls the refresh endpoint, and the same order re-fires and is struck, exactly once.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings, get_settings
from app.core.live_odds import publish_board_ticks
from app.core.security_vault import VaultCrypto
from app.models import BetLedger, ExchangeAccount, RiskMandate, User
from app.models.cfo_vault import (
    AuditEvent,
    AuditLog,
    BankrollAccount,
    LedgerEntry,
    LedgerStatus,
    MarketResult,
    PhantomLedger,
    RiskGuardSettings,
)
from app.models.hive_bots import TradingBot
from app.models.control_panel import SystemSettingsModel
from app.models.execution import EntityMapping, ExecutionVenue
from app.sandbox.bookmaker import SANDBOX_BASE_URL, build_sandbox_app, sandbox_credentials
from app.schemas.cfo_vault import ExecuteTradeRequest
from app.schemas.market import MarketTick
from app.services.bookmaker_gateway import BookmakerOrder, BookmakerOutcome, BookmakerResult
from app.services.cfo_execution import TradeExecutor, slippage_floor
from app.services.cfo_ledger import CfoError, lock_bankroll, resolve_manually, verify_account
from app.services.id_mapper import RemoteIds, VenueEvent
from app.services.order_resolver import OrderResolver, backoff_seconds
from app.services.session_manager import SessionToken
from app.services.sniper import SniperGateway

D = Decimal
TABLES = [
    User.__table__, ExchangeAccount.__table__, RiskMandate.__table__, BetLedger.__table__, SystemSettingsModel.__table__,
    TradingBot.__table__, BankrollAccount.__table__, PhantomLedger.__table__, LedgerEntry.__table__, AuditLog.__table__, RiskGuardSettings.__table__,
    MarketResult.__table__, ExecutionVenue.__table__, EntityMapping.__table__,
]
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
_SENTINEL = "betdoc:test-sentinel"
FIXTURE = "fx-ars-lee"
KICKOFF = datetime.now(UTC) + timedelta(days=2)


# ================================================================ a mock bookmaker
@dataclass
class MockVenue:
    """A partner order API with real auth state: tokens expire, refresh tokens rotate."""

    token_ttl: int = 3600
    place: Callable[[dict[str, Any], str], httpx.Response] | None = None
    statuses: dict[str, str] = field(default_factory=dict)  # remote id / client_ref -> status
    status_code: int = 200  # GET /bets answer code (503 = unreachable)
    events: list[dict[str, Any]] = field(default_factory=list)
    refresh_ok: bool = True
    calls: list[tuple[str, str, Any]] = field(default_factory=list)  # (method path, token or grant, body)
    valid: set[str] = field(default_factory=set)
    refresh_tokens: set[str] = field(default_factory=set)
    issued: int = 0
    placed: dict[str, str] = field(default_factory=dict)  # client_ref -> remote id
    fire_times: list[float] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if path == "/oauth/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.calls.append(("POST /oauth/token", form["grant_type"], None))
            if form["grant_type"] == "refresh_token":
                if not self.refresh_ok or form.get("refresh_token") not in self.refresh_tokens:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refresh_tokens.discard(form["refresh_token"])
            elif form.get("client_id") != "sniper-client" or form.get("client_secret") != "sniper-secret-123":
                return httpx.Response(401, json={"error": "invalid_client"})
            self.issued += 1
            access, refresh = f"tok-{self.issued}", f"ref-{self.issued}"
            self.valid.add(access)
            self.refresh_tokens.add(refresh)
            return httpx.Response(200, json={"access_token": access, "expires_in": self.token_ttl, "refresh_token": refresh})
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.method == "POST" and path == "/bets":
            body = json.loads(request.content)
            self.calls.append(("POST /bets", token, body))
            self.fire_times.append(time.perf_counter())
            if token not in self.valid:
                return httpx.Response(401, json={"error": "invalid_token"})
            if self.place is not None:
                return self.place(body, token)
            remote = self.placed.setdefault(body["client_ref"], f"BK-{len(self.placed) + 1}")
            return httpx.Response(200, json={"remote_bet_id": remote, "status": "OPEN", "matched_odds": body["odds"]})
        if request.method == "GET" and path == "/bets":
            self.calls.append(("GET /bets", token, dict(request.url.params)))
            if token not in self.valid:
                return httpx.Response(401, json={"error": "invalid_token"})
            if self.status_code != 200:
                return httpx.Response(self.status_code, json={"error": "unavailable"})
            rows = []
            for remote in filter(None, request.url.params.get("ids", "").split(",")):
                rows.append({"remote_bet_id": remote, "status": self.statuses.get(remote, "NOT_FOUND")})
            for ref in filter(None, request.url.params.get("client_refs", "").split(",")):
                status = self.statuses.get(ref, "NOT_FOUND")
                rows.append({"remote_bet_id": self.placed.get(ref) if status != "NOT_FOUND" else None, "client_ref": ref, "status": status})
            return httpx.Response(200, json={"bets": rows})
        if request.method == "GET" and path == "/events":
            return httpx.Response(200, json={"events": self.events})
        return httpx.Response(404)

    def revoke(self) -> None:
        """Server-side expiry: every access token the venue issued stops working."""
        self.valid.clear()


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and "~" in str(c.sqltext)]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(_sqlite_metadata().create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
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


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "starting_bankroll": 10_000.0,
            "CFO_EXECUTION_MODE": "live",
            "CFO_KILL_SWITCH_KEY": "test:kill_switch",
            "CFO_STREAK_KEY_PREFIX": "test:risk:streak",
            "CFO_IDEMPOTENCY_KEY_PREFIX": "test:idempotency",
            "ARYABHATA_PREFIX": "test_arya",
            "SNIPER_PREFIX": "test_sniper",
            "SNIPER_RATE_MAX_WAIT_SECONDS": 3.0,
            "SNIPER_SANDBOX_TOKEN_TTL_SECONDS": 600,
        }
    )


@pytest.fixture
def vault() -> VaultCrypto:
    return VaultCrypto(Fernet.generate_key().decode())


async def add_venue(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, *, rate: str = "50", burst: int = 50, venue_id: str = "smarkets") -> None:
    async with sessions() as session:
        session.add(
            ExecutionVenue(
                id=venue_id, display_name="Partner book", adapter="generic_json", base_url="https://partner.example/api",
                auth_type="oauth2_client_credentials", token_path="/oauth/token", refresh_path="/oauth/token",
                place_path="/bets", status_path="/bets", events_path="/events", bets_per_second=D(rate), burst=burst,
                routes=[], selection_codes={"HOME": "1", "DRAW": "X", "AWAY": "2"},
                encrypted_credentials=vault.encrypt_key(json.dumps({"client_id": "sniper-client", "client_secret": "sniper-secret-123"})),
                is_enabled=True, is_sandbox=False, currency="INR",  # a rupee account (unset, smarkets would default to GBP)
            )
        )
        await session.flush()  # the venue row first: its mappings reference it
        session.add(EntityMapping(venue_id=venue_id, kind="fixture", canonical_key=FIXTURE, remote_id="EV-9001", source="manual", detail={}))
        await session.commit()


def gateway(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, vault: VaultCrypto, venue: MockVenue, clock: Callable[[], datetime] | None = None) -> SniperGateway:
    http = httpx.AsyncClient(transport=httpx.MockTransport(venue.handler))
    return SniperGateway(sessions, redis, settings, vault, http, clock=clock or (lambda: datetime.now(UTC)))


@pytest_asyncio.fixture
async def user_id(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> uuid.UUID:
    async with sessions() as session:
        user = User(username=f"sniper_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(user)
        await session.commit()
        await lock_bankroll(session, user.id, settings)  # the ₹10,000 account
        await session.commit()
        return user.id


def order(**overrides: Any) -> BookmakerOrder:
    base = dict(client_ref=str(uuid.uuid4()), bookmaker_id="smarkets", fixture_id=FIXTURE, market="Match Odds", selection="HOME",
                odds=D("2.50"), stake_inr=D("100.00"), min_acceptable_odds=D("2.4875"))
    return BookmakerOrder(**{**base, **overrides})  # type: ignore[arg-type]


def request(**overrides: Any) -> ExecuteTradeRequest:
    base = dict(idempotency_key=uuid.uuid4(), fixture_id=FIXTURE, selection="HOME", bookmaker_id="smarkets", odds=D("2.50"),
                stake_inr=D("100.00"), true_prob=D("0.43"), commence_time=KICKOFF)
    return ExecuteTradeRequest(**{**base, **overrides})  # type: ignore[arg-type]


async def balances(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    async with sessions() as session:
        account = await session.scalar(select(BankrollAccount).where(BankrollAccount.user_id == user_id))
        assert account is not None
        return account.available_balance, account.exposure_balance


# ================================================================ the brief's proof: 401 -> refresh -> re-fire
@pytest.mark.asyncio
async def test_a_401_pauses_refreshes_the_session_and_refires_the_same_order(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    config = (await sniper.venues(fresh=True))[0]
    await sniper.sessions.bearer(config)  # a session is open: tok-1 (with refresh token ref-1)
    venue.revoke()  # the bookmaker expires it early, as live books do

    shot = order()
    result = await sniper.place(shot)

    assert result.outcome is BookmakerOutcome.ACCEPTED and result.reference == "BK-1"
    assert [(c[0], c[1]) for c in venue.calls] == [
        ("POST /oauth/token", "client_credentials"),  # the original login
        ("POST /bets", "tok-1"),  # fired with the expired token: 401
        ("POST /oauth/token", "refresh_token"),  # the refresh endpoint, not a fresh login
        ("POST /bets", "tok-2"),  # re-fired with the new session
    ]
    first, second = venue.calls[1][2], venue.calls[3][2]
    assert first == second and first["client_ref"] == shot.client_ref  # the same order, never a second one
    assert first["min_acceptable_odds"] == "2.4875"
    assert len(venue.placed) == 1


@pytest.mark.asyncio
async def test_the_refire_through_the_ledger_records_the_receipt(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    await sniper.sessions.bearer((await sniper.venues(fresh=True))[0])
    venue.revoke()
    receipt = await TradeExecutor(sessions, redis, settings, sniper).execute(user_id, request())
    assert receipt.status == "EXECUTED" and receipt.remote_bet_id == "BK-1"
    async with sessions() as session:
        entry = await session.get(PhantomLedger, receipt.ledger_id)
        assert entry is not None and entry.remote_bet_id == "BK-1" and entry.commence_time is not None and entry.next_resolve_at is not None
        audit = await session.scalar(select(AuditLog).where(AuditLog.event == AuditEvent.EXECUTED))
        assert audit is not None and audit.detail["request_payload"]["event_id"] == "EV-9001" and audit.detail["request_payload"]["selection_id"] == "1"
        assert "Authorization" not in json.dumps(audit.detail) and "tok-" not in json.dumps(audit.detail)  # credentials never recorded
        assert audit.detail["response_payload"]["remote_bet_id"] == "BK-1"
    assert await balances(sessions, user_id) == (D("9900.00"), D("100.00"))


@pytest.mark.asyncio
async def test_a_refused_refresh_token_falls_back_to_a_full_login(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue(refresh_ok=False)
    sniper = gateway(sessions, redis, settings, vault, venue)
    await sniper.sessions.bearer((await sniper.venues(fresh=True))[0])
    venue.revoke()
    assert (await sniper.place(order())).outcome is BookmakerOutcome.ACCEPTED
    grants = [c[1] for c in venue.calls if c[0] == "POST /oauth/token"]
    assert grants == ["client_credentials", "refresh_token", "client_credentials"]


@pytest.mark.asyncio
async def test_a_second_401_rejects_and_the_ledger_rolls_back(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue(place=lambda body, token: httpx.Response(401, json={"error": "account_suspended"}))
    sniper = gateway(sessions, redis, settings, vault, venue)
    with pytest.raises(CfoError) as caught:
        await TradeExecutor(sessions, redis, settings, sniper).execute(user_id, request())
    assert caught.value.reason == "AUTH_FAILED" and caught.value.status_code == 502
    assert await balances(sessions, user_id) == (D("10000.00"), D("0.00"))  # funds restored
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PhantomLedger)) == 0
        await verify_account(session, user_id)


# ================================================================ the session manager
@pytest.mark.asyncio
async def test_tokens_refresh_five_minutes_before_expiry_and_are_encrypted_at_rest(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault)
    clock = Clock()
    venue = MockVenue(token_ttl=600)
    sniper = gateway(sessions, redis, settings, vault, venue, clock)
    config = (await sniper.venues(fresh=True))[0]
    assert await sniper.sessions.bearer(config) == "tok-1"
    raw = await redis.get(sniper.sessions.key(config.id))
    assert raw and "tok-1" not in raw and "ref-1" not in raw  # Fernet ciphertext in Redis
    assert 595 <= await redis.ttl(sniper.sessions.key(config.id)) <= 600

    clock.now += timedelta(seconds=299)  # 301s left: still outside the 5-minute margin
    assert await sniper.sessions.bearer(config) == "tok-1"
    clock.now += timedelta(seconds=2)  # 299s left: inside it -> refreshed before use, via the refresh token
    assert await sniper.sessions.bearer(config) == "tok-2"
    assert [c[1] for c in venue.calls] == ["client_credentials", "refresh_token"]

    clock.now += timedelta(seconds=301)  # proactive pass (beat): tok-2 has 299s left
    assert await sniper.sessions.refresh_due([config]) == {config.id: "refreshed"}
    assert await sniper.sessions.refresh_due([config]) == {config.id: "fresh"}


@pytest.mark.asyncio
async def test_concurrent_shots_share_one_refresh(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    config = (await sniper.venues(fresh=True))[0]
    tokens = await asyncio.gather(*(sniper.sessions.bearer(config) for _ in range(8)))
    assert set(tokens) == {"tok-1"} and venue.issued == 1


@pytest.mark.asyncio
async def test_decrypted_credentials_are_wiped_after_use(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_venue(sessions, vault)
    buffers: list[bytearray] = []
    original = VaultCrypto.decrypt_into

    def spy(self: VaultCrypto, cipher: str) -> bytearray:
        buffer = original(self, cipher)
        buffers.append(buffer)
        return buffer

    monkeypatch.setattr(VaultCrypto, "decrypt_into", spy)
    sniper = gateway(sessions, redis, settings, vault, MockVenue())
    await sniper.sessions.bearer((await sniper.venues(fresh=True))[0])
    assert buffers and all(not any(b) for b in buffers)  # every decrypted buffer is zeroed


def test_session_token_round_trip() -> None:
    token = SessionToken("a", datetime(2026, 10, 9, tzinfo=UTC), "r")
    assert SessionToken.decode(token.encode()) == token
    assert SessionToken.decode("{bad") is None


# ================================================================ id mapping
@pytest.mark.asyncio
async def test_an_unmapped_fixture_aborts_before_any_money_moves(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    with pytest.raises(CfoError) as caught:
        await TradeExecutor(sessions, redis, settings, sniper).execute(user_id, request(fixture_id="fx-nowhere"))
    assert caught.value.reason == "UNMAPPED_FIXTURE"
    assert [c for c in venue.calls if c[0] == "POST /bets"] == []
    assert await balances(sessions, user_id) == (D("10000.00"), D("0.00"))
    # Placing without the pre-lock route (inside an open reservation) rejects too: the ledger rolls back
    result = await sniper.place(order(fixture_id="fx-nowhere"))
    assert result.outcome is BookmakerOutcome.REJECTED and result.reason == "UNMAPPED_FIXTURE"


@pytest.mark.asyncio
async def test_no_venue_for_the_bookmaker_aborts(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault)
    result = await gateway(sessions, redis, settings, vault, MockVenue()).place(order(bookmaker_id="unknownbook"))
    assert result.outcome is BookmakerOutcome.REJECTED and result.reason == "NO_EXECUTION_VENUE"


@pytest.mark.asyncio
async def test_catalog_sync_maps_venue_events_through_the_alias_dictionary(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault)
    sniper = gateway(sessions, redis, settings, vault, MockVenue())
    config = (await sniper.venues(fresh=True))[0]
    events = [
        VenueEvent("E-1", "soccer_epl", "Arsenal FC", "Leeds", KICKOFF.isoformat(), {"HOME": "E-1-H", "DRAW": "E-1-D", "AWAY": "E-1-A"}),
        VenueEvent("E-2", "soccer_epl", "Real Madrid", "Arsenal", KICKOFF.isoformat(), {"HOME": "E-2-H"}),
    ]
    report = await sniper.mapper.sync_catalog(config, events)
    assert (report.events, report.mapped) == (2, 1) and report.unresolved == ["E-2: Real Madrid v Arsenal"]
    aliases = sniper.mapper.aliases
    canonical = aliases.match_id("soccer_epl", aliases.lookup("soccer_epl", "Arsenal").id, aliases.lookup("soccer_epl", "Leeds United").id, KICKOFF)  # type: ignore[union-attr]
    assert await sniper.mapper.resolve(config, canonical, "Match Odds", "DRAW") == RemoteIds("E-1", "E-1-D")


# ================================================================ outbound rate limit
@pytest.mark.asyncio
async def test_simultaneous_edges_fire_micro_sequentially(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault, rate="2", burst=2)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    await sniper.sessions.bearer((await sniper.venues(fresh=True))[0])
    results = await asyncio.gather(*(sniper.place(order()) for _ in range(4)))
    assert all(r.outcome is BookmakerOutcome.ACCEPTED for r in results)
    gaps = sorted(venue.fire_times)
    assert gaps[3] - gaps[0] >= 0.9  # 2 at once (the burst), then one per 0.5s: never a 429-earning flood


@pytest.mark.asyncio
async def test_an_order_that_cannot_get_a_token_in_time_is_never_sent(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto) -> None:
    await add_venue(sessions, vault, rate="0.5", burst=1)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings.model_copy(update={"SNIPER_RATE_MAX_WAIT_SECONDS": 0.2}), vault, venue)
    first, second = await sniper.place(order()), await sniper.place(order())
    assert first.outcome is BookmakerOutcome.ACCEPTED
    assert second.outcome is BookmakerOutcome.REJECTED and second.reason == "OUTBOUND_THROTTLED"
    assert len([c for c in venue.calls if c[0] == "POST /bets"]) == 1


# ================================================================ slippage
@pytest.mark.parametrize(
    ("odds", "p", "tolerance", "floor"),
    [
        ("2.50", "0.42", "0.5", "2.4875"),  # tolerance binds: 2.50 * 0.995
        ("2.50", "0.405", "0.5", "2.4875"),  # +EV floor 1.005/0.405 = 2.4815 is lower
        ("2.50", "0.403", "0.5", "2.4938"),  # +EV floor 2.49379... binds (rounded up, never down)
        ("2.50", "0.40", "0.5", "2.50"),  # the request itself is under the floor: no slippage at all
        ("2.50", "0.42", "0", "2.50"),
        ("3.10", None, "1.0", "3.0690"),  # no fair probability: tolerance only
    ],
)
def test_slippage_floor(odds: str, p: str | None, tolerance: str, floor: str) -> None:
    assert slippage_floor(D(odds), D(p) if p else None, D(tolerance)) == D(floor)


@pytest.mark.asyncio
async def test_a_price_that_dropped_in_flight_is_refused_and_rolled_back(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue(place=lambda body, token: httpx.Response(409, json={"error": "PRICE_BELOW_MINIMUM", "current_odds": "2.40"}))
    with pytest.raises(CfoError) as caught:
        await TradeExecutor(sessions, redis, settings, gateway(sessions, redis, settings, vault, venue)).execute(user_id, request())
    assert caught.value.reason == "SLIPPAGE_REJECTED"
    assert await balances(sessions, user_id) == (D("10000.00"), D("0.00"))


@pytest.mark.asyncio
async def test_a_fill_inside_the_tolerance_books_the_struck_price(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue(place=lambda body, token: httpx.Response(200, json={"remote_bet_id": "BK-S", "matched_odds": "2.49"}))
    receipt = await TradeExecutor(sessions, redis, settings, gateway(sessions, redis, settings, vault, venue)).execute(user_id, request())
    assert receipt.odds == D("2.49") and receipt.potential_pnl == D("149.00")
    async with sessions() as session:
        entry = await session.get(PhantomLedger, receipt.ledger_id)
        assert entry is not None and entry.odds == D("2.49") and entry.potential_pnl == D("149.00")


# ================================================================ the order resolver and its DLQ
async def placed(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, sniper: SniperGateway, user_id: uuid.UUID, **kwargs: Any) -> uuid.UUID:
    receipt = await TradeExecutor(sessions, redis, settings, sniper).execute(user_id, request(**kwargs))
    return receipt.ledger_id


async def entry_of(sessions: async_sessionmaker[AsyncSession], ledger_id: uuid.UUID) -> PhantomLedger:
    async with sessions() as session:
        entry = await session.get(PhantomLedger, ledger_id)
        assert entry is not None
        return entry


def test_backoff_doubles_and_caps() -> None:
    settings = get_settings()
    assert [backoff_seconds(n, settings, jitter=0.0) for n in (1, 2, 3, 4)] == [30.0, 60.0, 120.0, 240.0]
    assert backoff_seconds(20, settings, jitter=0.0) == 3600.0


@pytest.mark.asyncio
async def test_a_won_statement_settles_through_the_ledger_engine(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    ledger_id = await placed(sessions, redis, settings, sniper, user_id)
    clock = Clock()
    clock.now += timedelta(seconds=settings.SNIPER_OPEN_POLL_SECONDS + 1)
    venue.statuses["BK-1"] = "OPEN"
    summary = await OrderResolver(sniper, sessions, settings, clock).run()
    assert summary.open == 1 and (await entry_of(sessions, ledger_id)).status is LedgerStatus.PENDING

    clock.now += timedelta(seconds=settings.SNIPER_OPEN_POLL_SECONDS + 1)
    venue.statuses["BK-1"] = "WON"
    summary = await OrderResolver(sniper, sessions, settings, clock).run()
    assert summary.graded == 1
    entry = await entry_of(sessions, ledger_id)
    assert entry.status is LedgerStatus.WON and entry.realized_pnl == D("150.00")
    assert await balances(sessions, user_id) == (D("10150.00"), D("0.00"))
    async with sessions() as session:
        settled = await session.scalar(select(AuditLog).where(AuditLog.event == AuditEvent.SETTLED))
        assert settled is not None and settled.detail["source"] == "bookmaker:smarkets"
        await verify_account(session, user_id)


@pytest.mark.asyncio
async def test_an_unreachable_venue_backs_off_then_dead_letters_after_ten_failures(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    ledger_id = await placed(sessions, redis, settings, sniper, user_id)
    venue.status_code = 503
    clock = Clock()
    for attempt in range(1, 11):
        clock.now = (await entry_of(sessions, ledger_id)).next_resolve_at or clock.now  # type: ignore[assignment]
        clock.now = clock.now.replace(tzinfo=UTC) if clock.now.tzinfo is None else clock.now
        await OrderResolver(sniper, sessions, settings, clock, jitter=0.0).run()
        entry = await entry_of(sessions, ledger_id)
        if attempt < 10:
            assert entry.status is LedgerStatus.PENDING and entry.resolve_attempts == attempt
            gap = (entry.next_resolve_at.replace(tzinfo=UTC) - clock.now).total_seconds()  # type: ignore[union-attr]
            assert gap == pytest.approx(backoff_seconds(attempt, settings, jitter=0.0))
    assert entry.status is LedgerStatus.REQUIRES_MANUAL_INTERVENTION and entry.next_resolve_at is None
    assert await balances(sessions, user_id) == (D("9900.00"), D("100.00"))  # still in exposure: the bet may be live
    async with sessions() as session:
        dlq = await session.scalar(select(AuditLog).where(AuditLog.event == AuditEvent.DEAD_LETTERED))
        assert dlq is not None and "10_FAILURES" in dlq.detail["reason"]
    clock.now += timedelta(days=1)
    assert (await OrderResolver(sniper, sessions, settings, clock).run()).polled == 0  # out of rotation

    async with sessions() as session:  # a person resolves it from the bookmaker's records
        entry, won = await resolve_manually(session, settings, ledger_id, outcome="LOST", remote_bet_id=None, actor=None)
        await session.commit()
    assert entry.status is LedgerStatus.LOST and won is False
    assert await balances(sessions, user_id) == (D("9900.00"), D("0.00"))


@pytest.mark.asyncio
async def test_a_bet_still_open_a_day_after_kickoff_is_dead_lettered(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    venue = MockVenue()
    sniper = gateway(sessions, redis, settings, vault, venue)
    ledger_id = await placed(sessions, redis, settings, sniper, user_id)
    venue.statuses["BK-1"] = "OPEN"
    clock = Clock()
    clock.now = KICKOFF + timedelta(hours=settings.SNIPER_DLQ_AFTER_HOURS, minutes=1)
    summary = await OrderResolver(sniper, sessions, settings, clock).run()
    assert summary.dead_lettered == 1 and (await entry_of(sessions, ledger_id)).status is LedgerStatus.REQUIRES_MANUAL_INTERVENTION


@pytest.mark.parametrize(("venue_says", "status", "available", "exposure"), [("OPEN", LedgerStatus.PENDING, "9900.00", "100.00"), ("REJECTED", LedgerStatus.REJECTED, "10000.00", "0.00")])
@pytest.mark.asyncio
async def test_an_unconfirmed_order_is_found_by_its_client_ref(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID,
    venue_says: str, status: LedgerStatus, available: str, exposure: str,
) -> None:
    await add_venue(sessions, vault)
    struck: dict[str, str] = {}

    def timeout_after_striking(body: dict[str, Any], token: str) -> httpx.Response:
        struck[body["client_ref"]] = "BK-T"
        raise httpx.ReadTimeout("answer lost", request=httpx.Request("POST", "https://partner.example/api/bets"))

    venue = MockVenue(place=timeout_after_striking)
    sniper = gateway(sessions, redis, settings, vault, venue)
    receipt = await TradeExecutor(sessions, redis, settings, sniper).execute(user_id, request())
    assert receipt.status == "UNKNOWN"
    ref = next(iter(struck))
    venue.placed[ref], venue.statuses[ref] = "BK-T", venue_says
    clock = Clock()
    clock.now += timedelta(seconds=settings.SNIPER_RESOLVE_BACKOFF_BASE_SECONDS + 1)
    await OrderResolver(sniper, sessions, settings, clock).run()
    entry = await entry_of(sessions, receipt.ledger_id)
    assert entry.status is status and not entry.reconcile_required
    if status is LedgerStatus.PENDING:
        assert entry.remote_bet_id == "BK-T"
    assert await balances(sessions, user_id) == (D(available), D(exposure))


# ================================================================ the sandbox venue, end to end
@pytest.mark.asyncio
async def test_the_sandbox_venue_end_to_end(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    from app.services.sniper_runtime import ensure_sandbox_venue
    from app.services.omni_normalizer import default_alias_dictionary

    aliases = default_alias_dictionary()
    fixture = aliases.match_id("soccer_epl", aliases.lookup("soccer_epl", "Arsenal").id, aliases.lookup("soccer_epl", "Leeds United").id, KICKOFF)  # type: ignore[union-attr]
    ticks = [  # the board, as the ingestion fleet publishes it: canonical match ids
        MarketTick(match_id=fixture, home_team="Arsenal", away_team="Leeds United", market_type="Match Odds", selection=sel, odds=D(price),
                   true_probability=D("0.4"), is_suspended=False, sport_key="soccer_epl", commence_time=KICKOFF)
        for sel, price in (("HOME", "2.50"), ("DRAW", "3.40"), ("AWAY", "3.10"))
    ]
    assert await publish_board_ticks(redis, ticks)
    assert await ensure_sandbox_venue(sessions, vault, settings)
    sandbox_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_sandbox_app(redis, sessions, settings)), base_url=SANDBOX_BASE_URL)
    sniper = SniperGateway(sessions, redis, settings, vault, httpx.AsyncClient(), sandbox_http)
    venue = (await sniper.venues(fresh=True))[0]
    assert venue.is_sandbox and (await sniper.venue_for("onexbet")) == venue  # stands in for any bookmaker

    report = await sniper.mapper.sync_catalog(venue, await sniper.adapter(venue).fetch_events())
    assert report.mapped == 1

    executor = TradeExecutor(sessions, redis, settings, sniper)
    receipt = await executor.execute(user_id, request(fixture_id=fixture, bookmaker_id="onexbet", odds=D("2.50"), true_prob=D("0.42")))
    assert receipt.status == "EXECUTED" and receipt.remote_bet_id and receipt.remote_bet_id.startswith("SBX-B-")
    with pytest.raises(CfoError) as slipped:  # the sandbox's price is 2.50: asking 2.60 with a 2.5870 floor is refused
        await executor.execute(user_id, request(fixture_id=fixture, bookmaker_id="onexbet", odds=D("2.60"), true_prob=D("0.42")))
    assert slipped.value.reason == "SLIPPAGE_REJECTED"

    await redis.delete(*[k async for k in redis.scan_iter(f"{settings.SNIPER_PREFIX}:sandbox:token:*")])  # every sandbox session expires
    again = await executor.execute(user_id, request(fixture_id=fixture, bookmaker_id="onexbet", selection="AWAY", odds=D("3.10"), true_prob=D("0.34")))
    assert again.status == "EXECUTED"  # 401 -> refresh -> re-fired

    async with sessions() as session:
        session.add(MarketResult(fixture_id=fixture, market="Match Odds", winning_selection="HOME", source="test"))
        await session.commit()
    clock = Clock()
    clock.now += timedelta(seconds=settings.SNIPER_OPEN_POLL_SECONDS + 1)
    summary = await OrderResolver(sniper, sessions, settings, clock).run()
    assert summary.graded == 2
    assert (await entry_of(sessions, receipt.ledger_id)).status is LedgerStatus.WON
    assert (await entry_of(sessions, again.ledger_id)).status is LedgerStatus.LOST
    assert sandbox_credentials(settings)["client_secret"] != ""
    await sandbox_http.aclose()


# ================================================================ the terminal
@pytest.mark.asyncio
async def test_the_terminal_hears_every_step(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto, user_id: uuid.UUID) -> None:
    await add_venue(sessions, vault)
    sniper = gateway(sessions, redis, settings, vault, MockVenue())
    await TradeExecutor(sessions, redis, settings, sniper).execute(user_id, request())
    steps = [line["step"] for line in reversed(await sniper.feed.history(user_id))]
    assert steps == ["route", "map", "map", "auth", "fire", "result"]
    last = (await sniper.feed.history(user_id))[0]
    assert last["message"].startswith("200 OK - remote_id: BK-1") and last["level"] == "success"


def test_venue_config_routes() -> None:
    venue = VenueConfig(id="pinnacle", display_name="P", adapter="generic_json", base_url="https://x", auth_type="static_bearer",
                        place_path="/bets", status_path="/bets", bets_per_second=D("2"), burst=2, routes=("pinnacle_asia",))
    assert venue.handles("pinnacle") and venue.handles("pinnacle_asia") and not venue.handles("bet365")
    assert BookmakerResult(BookmakerOutcome.REJECTED, "X").steps == []
