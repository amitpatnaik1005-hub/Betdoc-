"""
Unit and Integration Tests for Paper Trading Sandbox, Currency Engine and Tax Ledger (Group 80).
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

The brief's reference assertions (on real users, exact decimals and the Group 79 settlement grammar), then:

* the pure pieces: Section 115BBJ and the Indian financial year, the forward-test metrics (win rate on decided bets,
  ROI, drawdown, profit factor, Sharpe at rf 6.5% over sqrt(365), the one-sided t-test), Ashoka's leg translation;
* ASHVATTHAMA: the paper fill at Parimatch's live price from Aryabhata's book store, PRICE_MOVED, MARKET_SUSPENDED,
  an unverified fill, the exchange refusing a parlay, the live ticket;
* TODAR MAL: rates from Frankfurter and CoinGecko (a mock transport) into the table and ``fx:rates:latest``, cross
  rates through INR, the execution FX hash first, failing closed when no provider answers;
* the sandbox service on SQLite and PostgreSQL (``TEST_POSTGRES_URL``): placement, idempotency, the stake cap,
  CHANAKYA's kill switch, singles and parlays (half results), the TDS ledger, auto-settlement from recorded scores,
  cash-out, reset epochs, performance, FA-2's XP paid once;
* the API through ``httpx.ASGITransport(app=app)``; the Celery beat; commander #17 ASHVATTHAMA.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_user, get_db
from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.domain.paper_trading import metrics as pm
from app.domain.paper_trading.legs import LegTranslationError, translate
from app.domain.paper_trading.tax import financial_year, section_115bbj
from app.main import app
from app.models import User
from app.models.control_panel import SystemSettingsModel
from app.models.never_forget import UserXPProfile, XPAuditLog
from app.models.paper_trading import (
    CurrencyExchangeRate,
    PaperAccount,
    PaperBet,
    PaperBetStatus,
    PaperLedgerEntry,
    TaxLedgerEntry,
)
from app.models.the_hive import LegendaryBot
from app.models.user_bets_ledger import FixtureScore
from app.services import ashvatthama
from app.services import paper_trading_service as pts
from app.services.commander_supervisor import PROBES
from app.services.currency_service import CurrencyService, CurrencyUnavailableError
from app.services.paper_trading_service import PaperTradingService

DEVELOPER = "Amit Ashok Kumar Patnaik"
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-paper-trading"
T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


# ================================================================ fixtures: databases, Redis, the app
TABLES: list[Table] = [User.__table__, PaperAccount.__table__, PaperBet.__table__, PaperLedgerEntry.__table__, TaxLedgerEntry.__table__,  # type: ignore[list-item]
                       CurrencyExchangeRate.__table__, UserXPProfile.__table__, XPAuditLog.__table__, FixtureScore.__table__,  # type: ignore[list-item]
                       SystemSettingsModel.__table__]  # type: ignore[list-item]


def _closure(tables: list[Table]) -> list[Table]:
    seen: dict[str, Table] = {}
    stack = list(tables)
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen[t.name] = t
        stack.extend(fk.column.table for fk in t.foreign_keys)
    return [t for t in User.metadata.sorted_tables if t.name in seen]


ALL_TABLES = _closure(TABLES)


def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in ALL_TABLES:
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
        await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ALL_TABLES))))
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=ALL_TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ALL_TABLES))))
        await engine.dispose()


@pytest_asyncio.fixture
async def async_session(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        yield session


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


async def _new_user(session: AsyncSession, role: str = "QUANT") -> uuid.UUID:
    user = User(id=uuid.uuid4(), username=f"{role.lower()}-{uuid.uuid4().hex[:8]}", hashed_password="x", role=role, is_active=True)
    session.add(user)
    await session.commit()
    return user.id


async def _seed_book(redis: Redis, match_id: str, market: str, prices: dict[str, str], *, bookmaker: str = "parimatch", age_seconds: float = 5.0,
                     suspended: bool = False, now: datetime | None = None) -> None:
    """A book line in Aryabhata's normalised store, the way Parimatch's injected prices land."""
    settings = get_settings()
    seen = (now or datetime.now(UTC)) - timedelta(seconds=age_seconds)
    line = {"s": f"{bookmaker}_feed", "b": bookmaker, "p": prices, "t": seen.timestamp(), "x": suspended}
    await redis.hset(f"{settings.ARYABHATA_PREFIX}:books:{match_id}|{market}", f"{bookmaker}_feed:{bookmaker}", json.dumps(line))


def _leg(match_id: str, selection: str = "home", odds: str = "2.00", market: str = "moneyline", line: float | None = None,
         sport: str = "soccer", quote: tuple[str, str] | None = None) -> pts.LegRequest:
    return pts.LegRequest(sport, match_id, market, selection, Decimal(odds), line, {}, f"{match_id} event",
                          None if quote is None else quote[0], None if quote is None else quote[1])


async def _place(session: AsyncSession, user_id: uuid.UUID, legs: list[pts.LegRequest], stake: str, *, redis: Redis | None = None,
                 now: datetime = T0, **kw: Any) -> PaperBet:
    bet, _ = await pts.place_paper_bet(session, redis, get_settings(), user_id, pts.PlaceRequest(legs, Decimal(stake), **kw), now)
    return bet


async def _account(session: AsyncSession, user_id: uuid.UUID) -> PaperAccount:
    return await pts.get_or_create_account(session, get_settings(), user_id)


# ================================================================ the brief's reference tests (adapted: real users, Decimal money)
async def test_paper_account_creation_default_balance(async_session: AsyncSession) -> None:
    """Verifies default virtual paper trading account initialization with ₹100,000 INR."""
    svc = PaperTradingService()
    user_id = await _new_user(async_session)
    account = await svc.get_or_create_account(async_session, user_id=user_id)
    assert account.user_id == user_id
    assert account.virtual_balance == Decimal("100000.00")
    assert account.initial_balance == Decimal("100000.00")
    assert account.currency == "INR"
    assert account.total_paper_bets == 0
    assert account.won_bets == 0
    assert account.lost_bets == 0
    assert account.total_pnl == 0
    again = await svc.get_or_create_account(async_session, user_id=user_id)
    assert again.id == account.id
    entries = (await async_session.execute(select(PaperLedgerEntry).where(PaperLedgerEntry.account_id == account.id))).scalars().all()
    assert [e.kind for e in entries] == ["OPEN"]


async def test_place_paper_bet_and_deduction(async_session: AsyncSession) -> None:
    """Verifies virtual stake deduction and pending bet creation."""
    svc = PaperTradingService()
    user_id = await _new_user(async_session)
    bet = await svc.place_paper_bet(db=async_session, user_id=user_id, sport_key="soccer", match_id="match_ars_che", event_name="Arsenal vs Chelsea",
                                    market_type="1x2_match_odds", selection="home", odds=2.10, stake_amount=5000.0, target_bookmaker="parimatch")
    assert bet.id is not None
    assert bet.stake_amount == Decimal("5000.00")
    assert bet.potential_payout == Decimal("10500.00")
    assert bet.status == PaperBetStatus.PENDING.value
    assert bet.target_bookmaker == "parimatch"
    assert bet.execution["commander"] == "ASHVATTHAMA" and bet.execution["price_source"] == "requested_unverified"
    account = await svc.get_or_create_account(async_session, user_id)
    assert account.virtual_balance == Decimal("95000.00")
    assert account.total_paper_bets == 1


async def test_insufficient_virtual_funds(async_session: AsyncSession) -> None:
    """Ensures placing a bet larger than virtual balance raises ValueError."""
    svc = PaperTradingService()
    user_id = await _new_user(async_session)
    with pytest.raises(ValueError, match="Insufficient virtual balance"):
        await svc.place_paper_bet(db=async_session, user_id=user_id, sport_key="cricket", match_id="ind_aus", event_name="India vs Australia",
                                  market_type="match_winner", selection="home", odds=1.85, stake_amount=150000.0)


async def test_settle_paper_bet_win_and_tds(async_session: AsyncSession) -> None:
    """Tests winning bet settlement, balance crediting, and Indian Section 115BBJ 30% TDS ledger."""
    svc = PaperTradingService()
    user_id = await _new_user(async_session)
    bet = await svc.place_paper_bet(db=async_session, user_id=user_id, sport_key="soccer", match_id="m1", event_name="Man City vs Liverpool",
                                    market_type="1x2_match_odds", selection="home", odds=2.00, stake_amount=10000.0)
    settled = await svc.settle_paper_bet(db=async_session, bet_id=bet.id, match_result={"home_score": 2, "away_score": 1, "status": "FT"},
                                         settlement_notes="City wins 2-1")
    assert settled.status == PaperBetStatus.WON.value
    assert settled.pnl == Decimal("10000.00")
    assert settled.tds_amount == Decimal("3000.00") and settled.net_post_tax_payout == Decimal("17000.00")
    assert settled.settlement_notes == "City wins 2-1"
    account = await svc.get_or_create_account(async_session, user_id)
    assert account.virtual_balance == Decimal("110000.00")
    assert account.won_bets == 1
    assert account.total_pnl == Decimal("10000.00")
    assert account.roi_pct == 10.0
    assert account.total_tds == Decimal("3000.00")
    entry = (await async_session.execute(select(TaxLedgerEntry).where(TaxLedgerEntry.paper_bet_id == bet.id))).scalar_one()
    assert (entry.net_winnings, entry.tds_amount, entry.section, entry.jurisdiction, entry.mode) == (
        Decimal("10000.00"), Decimal("3000.00"), "115BBJ", "IN", "PAPER")
    # settling twice changes nothing
    again = await svc.settle_paper_bet(db=async_session, bet_id=bet.id, match_result={"home_score": 0, "away_score": 1})
    assert again.status == PaperBetStatus.WON.value
    assert (await svc.get_or_create_account(async_session, user_id)).virtual_balance == Decimal("110000.00")


async def test_settle_paper_bet_loss(async_session: AsyncSession) -> None:
    """Tests losing bet settlement and P&L update."""
    svc = PaperTradingService()
    user_id = await _new_user(async_session)
    bet = await svc.place_paper_bet(db=async_session, user_id=user_id, sport_key="soccer", match_id="m2", event_name="Real Madrid vs Barcelona",
                                    market_type="1x2_match_odds", selection="away", odds=3.20, stake_amount=5000.0)
    settled = await svc.settle_paper_bet(db=async_session, bet_id=bet.id, match_result={"home_score": 3, "away_score": 1, "status": "FT"})
    assert settled.status == PaperBetStatus.LOST.value
    assert settled.pnl == Decimal("-5000.00") and settled.tds_amount == 0
    account = await svc.get_or_create_account(async_session, user_id)
    assert account.virtual_balance == Decimal("95000.00")
    assert account.lost_bets == 1
    assert account.total_pnl == Decimal("-5000.00")
    assert account.roi_pct == -5.0
    assert (await async_session.execute(select(func.count()).select_from(TaxLedgerEntry))).scalar_one() == 0


async def test_paper_account_reset(async_session: AsyncSession) -> None:
    """Verifies resetting virtual balance restores clean starting slate."""
    svc = PaperTradingService()
    user_id = await _new_user(async_session)
    bet = await svc.place_paper_bet(db=async_session, user_id=user_id, sport_key="tennis", match_id="t1", event_name="Alcaraz vs Sinner",
                                    market_type="match_winner", selection="home", odds=1.90, stake_amount=20000.0)
    reset_acc = await svc.reset_account(async_session, user_id)
    assert reset_acc.virtual_balance == Decimal("100000.00")
    assert reset_acc.total_paper_bets == 0
    assert reset_acc.total_pnl == 0
    assert reset_acc.resets == 1 and reset_acc.last_reset_at is not None
    voided = (await async_session.execute(select(PaperBet).where(PaperBet.id == bet.id))).scalar_one()
    assert voided.status == PaperBetStatus.VOID.value and voided.payout == 0
    custom = await svc.reset_account(async_session, user_id, initial_balance=250000)
    assert custom.virtual_balance == Decimal("250000.00") and custom.initial_balance == Decimal("250000.00") and custom.resets == 2


async def test_currency_conversion_and_tds() -> None:
    """Tests Section 115BBJ 30% TDS (the brief's figures)."""
    svc = CurrencyService()
    tds = svc.calculate_indian_tds(gross_win=25000.0, stake=10000.0)
    assert tds["gross_win"] == 25000.0
    assert tds["stake"] == 10000.0
    assert tds["net_profit"] == 15000.0
    assert tds["tds_deducted"] == 4500.0
    assert tds["net_take_home"] == 20500.0
    assert tds["tds_rate_pct"] == 30.0
    tds_loss = svc.calculate_indian_tds(gross_win=0.0, stake=5000.0)
    assert tds_loss["net_profit"] == 0.0
    assert tds_loss["tds_deducted"] == 0.0
    assert tds_loss["net_take_home"] == 0.0


# ================================================================ the pure pieces
def test_section_115bbj_and_financial_year() -> None:
    b = section_115bbj(Decimal("30000"), Decimal("10000"))
    assert (b.net_winnings, b.tds, b.net_post_tax_payout) == (Decimal("20000.00"), Decimal("6000.00"), Decimal("24000.00"))
    assert b.cess == Decimal("240.00")  # 4% health and education cess on the tax
    assert math.isclose(b.effective_rate_pct, 31.2)  # 30% TDS plus the 4% cess on it
    push = section_115bbj(Decimal("10000"), Decimal("10000"))
    assert push.tds == 0 and push.net_post_tax_payout == Decimal("10000.00")
    odd = section_115bbj(Decimal("1234.57"), Decimal("1000"))
    assert odd.tds == Decimal("70.37")  # 0.30 x 234.57 = 70.371, to the paisa
    assert financial_year(datetime(2026, 3, 31, 18, 29, tzinfo=UTC)) == "2025-26"  # 23:59 IST on 31 March
    assert financial_year(datetime(2026, 3, 31, 18, 31, tzinfo=UTC)) == "2026-27"  # 00:01 IST on 1 April
    assert financial_year(datetime(2027, 1, 15, tzinfo=UTC)) == "2026-27"


def test_forward_test_metrics() -> None:
    assert pm.win_rate_pct(6, 4) == 60.0 and pm.win_rate_pct(0, 0) == 0.0
    assert pm.roi_pct(10000, 100000) == 10.0
    assert math.isclose(pm.max_drawdown_pct([100.0, 120.0, 90.0, 130.0, 117.0]), 25.0)
    assert pm.max_drawdown_pct([]) == 0.0
    assert pm.profit_factor(3000.0, 1500.0) == 2.0 and pm.profit_factor(0.0, 0.0) is None
    assert pm.daily_closes([(date(2026, 1, 1), 100.0), (date(2026, 1, 1), 101.0), (date(2026, 1, 2), 103.0)]) == [101.0, 103.0]
    flat = pm.sharpe_ratio([100.0, 100.0, 100.0])
    assert flat is None  # no variance, no ratio
    rising = pm.sharpe_ratio([100.0, 101.0, 101.5, 103.0, 103.2, 104.5])
    assert rising is not None and rising > 0
    assert pm.sharpe_ratio([100.0]) is None
    few = pm.forward_test_significance([0.9, -1.0, 0.8])
    assert not few.proven and pm.status_summary(500.0, 3, few) == "BUILDING STATISTICAL SAMPLE"
    edge = pm.forward_test_significance([0.95, -1.0, 0.95, 0.95, -1.0, 0.95, 0.95, 0.95, -1.0, 0.95] * 4, min_sample=30)
    assert edge.sample == 40 and edge.p_value is not None and edge.proven
    assert pm.status_summary(9000.0, 40, edge) == "PROFITABLE FORWARD-TEST"
    noise = pm.forward_test_significance([1.0, -1.0] * 20, min_sample=30)
    assert not noise.proven and pm.status_summary(-10.0, 40, noise) == "CALIBRATING ADVICE"
    assert pm.brier_score([(0.6, 1.0), (0.4, 0.0)]) == 0.16 and pm.brier_score([]) is None


def test_ashoka_leg_translation() -> None:
    home = translate("soccer_epl", "Match Odds", "home")
    assert (home.sport_key, home.market_type, home.selection, home.line, home.quote_market, home.quote_selection) == (
        "soccer", "moneyline", "home", None, "Match Odds", "HOME")
    over = translate(None, "Totals 2.5", "OVER")
    assert (over.market_type, over.selection, over.line) == ("total", "over", 2.5)
    away_ah = translate("soccer_epl", "Asian Handicap -0.75", "AWAY")
    assert (away_ah.market_type, away_ah.selection, away_ah.line) == ("asian_handicap", "away", 0.75)  # the away side's own line
    assert translate(None, "Asian Handicap -0.75", "HOME").line == -0.75
    assert translate(None, "Double Chance", "1X").market_type == "double_chance"
    assert translate(None, "BTTS", "YES").market_type == "btts"
    assert translate(None, "Draw No Bet", "AWAY").market_type == "draw_no_bet"
    with pytest.raises(LegTranslationError):
        translate(None, "Corners 9.5", "OVER")
    with pytest.raises(LegTranslationError):
        translate(None, "Match Odds", "OVER")


# ================================================================ ASHVATTHAMA
async def test_ashvatthama_fills_at_the_parimatch_live_price(redis: Redis) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    await _seed_book(redis, "fx-1", "Match Odds", {"HOME": "2.06", "DRAW": "3.40", "AWAY": "3.60"}, now=now)
    await _seed_book(redis, "fx-1", "Match Odds", {"HOME": "2.20"}, bookmaker="pinnacle", now=now)  # another book: not the target
    leg = ashvatthama.ExecLeg("fx-1", Decimal("2.10"), "Match Odds", "HOME")
    receipt = await ashvatthama.execute_paper(redis, settings, target="parimatch", legs=[leg], stake_inr=Decimal(1000), now=now, multiple=False)
    assert receipt.status == "FILLED" and receipt.venue == "parimatch"
    assert receipt.executed_odds == Decimal("2.06") and receipt.price_source == "parimatch_live"
    assert receipt.venue_url == settings.PARIMATCH_TARGET_URL
    assert math.isclose(receipt.slippage_pct or 0.0, 1.9048, abs_tol=1e-3)  # (2.10 - 2.06) / 2.10
    assert receipt.quote_age_seconds is not None and receipt.quote_age_seconds >= 4
    out = receipt.as_dict()
    assert out["commander"] == "ASHVATTHAMA" and out["total_latency_ms"] >= settings.PAPER_SIM_LATENCY_MS

    await _seed_book(redis, "fx-1", "Match Odds", {"HOME": "1.95"}, now=now)  # moved more than 2% under 2.10
    with pytest.raises(ashvatthama.ExecutionRefused) as moved:
        await ashvatthama.execute_paper(redis, settings, target="parimatch", legs=[leg], stake_inr=Decimal(1000), now=now, multiple=False)
    assert moved.value.receipt.reason is not None and moved.value.receipt.reason.startswith("PRICE_MOVED")

    await _seed_book(redis, "fx-1", "Match Odds", {"HOME": "2.10"}, suspended=True, now=now)
    with pytest.raises(ashvatthama.ExecutionRefused, match="MARKET_SUSPENDED"):
        await ashvatthama.execute_paper(redis, settings, target="parimatch", legs=[leg], stake_inr=Decimal(1000), now=now, multiple=False)

    await _seed_book(redis, "fx-1", "Match Odds", {"HOME": "2.30"}, age_seconds=settings.PAPER_QUOTE_MAX_AGE_SECONDS + 60, now=now)
    stale = await ashvatthama.execute_paper(redis, settings, target="parimatch", legs=[leg], stake_inr=Decimal(1000), now=now, multiple=False)
    assert stale.executed_odds == Decimal("2.10") and stale.price_source == "requested_unverified"  # a stale quote proves nothing

    two = [leg, ashvatthama.ExecLeg("fx-2", Decimal("1.50"))]
    with pytest.raises(ashvatthama.ExecutionRefused, match="singles only"):
        await ashvatthama.execute_paper(redis, settings, target="betfair", legs=two, stake_inr=Decimal(1000), now=now, multiple=True)
    with pytest.raises(ValueError, match="not an OmniRoute venue"):
        ashvatthama.resolve_venue("unknown-book")


def test_ashvatthama_live_ticket_places_nothing() -> None:
    settings = get_settings()
    ticket = ashvatthama.live_ticket(settings, target=None, odds=Decimal("2.05"), stake_inr=Decimal("2500"), description="Arsenal to win")
    assert ticket.mode == "LIVE" and ticket.status == "TICKET" and ticket.venue == "parimatch"
    assert ticket.venue_url == "https://parimatchs1.com/en/all-live" == settings.PARIMATCH_TARGET_URL
    assert ticket.reason is not None and "₹2,500.00" in ticket.reason and ticket.slip_ref.startswith("LIVE-PARIMATCH-")


# ================================================================ TODAR MAL
def _fx_transport(calls: list[str], *, fiat: bool = True, crypto: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "api.frankfurter.app":
            if not fiat:
                return httpx.Response(503)
            assert request.url.params["from"] == "INR"
            return httpx.Response(200, json={"base": "INR", "rates": {"USD": 0.012, "EUR": 0.0110, "GBP": 0.0095}})
        if not crypto:
            return httpx.Response(429)
        return httpx.Response(200, json={"tether": {"inr": 83.9}, "bitcoin": {"inr": 5600000}})
    return httpx.MockTransport(handler)


async def test_currency_service_refresh_cache_and_cross_rates(async_session: AsyncSession, redis: Redis) -> None:
    settings = get_settings()
    calls: list[str] = []
    svc = CurrencyService(settings, redis, transport=_fx_transport(calls))
    now = datetime.now(UTC)
    usd = await svc.get_exchange_rate(async_session, "USD", "INR", now)  # nothing cached: one refresh from the providers
    assert math.isclose(float(usd.rate), 1 / 0.012, rel_tol=1e-9) and usd.provider == "frankfurter:ecb"
    assert sorted(set(calls)) == ["api.coingecko.com", "api.frankfurter.app"]
    cached = json.loads(await redis.hget(settings.CURRENCY_CACHE_KEY, "USD_INR"))
    assert settings.CURRENCY_CACHE_KEY == "fx:rates:latest" and Decimal(cached["rate"]) > 83
    calls.clear()
    eur_usd = await svc.get_exchange_rate(async_session, "EUR", "USD", now)  # a cross rate through INR, from the cache
    assert calls == [] and math.isclose(float(eur_usd.rate), 0.012 / 0.0110, rel_tol=1e-9)
    btc, quote = await svc.convert(async_session, Decimal("1120000"), "INR", "BTC", now)
    assert btc == Decimal("0.20000000") and quote.provider == "coingecko"
    rows = (await async_session.execute(select(func.count()).select_from(CurrencyExchangeRate))).scalar_one()
    assert rows == 10  # five currencies, both directions
    await redis.delete(settings.CURRENCY_CACHE_KEY)
    from_table = await svc.get_exchange_rate(async_session, "GBP", "INR", now)
    assert calls == [] and math.isclose(float(from_table.rate), 1 / 0.0095, rel_tol=1e-6)
    board = await svc.board(async_session, now)
    assert set(board["rates"]) == {"INR", "USD", "EUR", "GBP", "USDT", "BTC"} and board["missing"] == {}
    # the execution FX hash wins: the rates real money is converted with
    await redis.hset(settings.FX_RATES_KEY, "USD", json.dumps({"inr_per_unit": "84.25", "as_of": now.timestamp(), "source": "admin"}))
    execution = await svc.get_exchange_rate(async_session, "USD", "INR", now)
    assert execution.rate == Decimal("84.25") and execution.provider.startswith("execution:")
    with pytest.raises(ValueError, match="not a supported currency"):
        await svc.get_exchange_rate(async_session, "JPY", "INR", now)


async def test_currency_service_fails_closed(async_session: AsyncSession, redis: Redis) -> None:
    calls: list[str] = []
    svc = CurrencyService(get_settings(), redis, transport=_fx_transport(calls, fiat=False, crypto=False))
    with pytest.raises(CurrencyUnavailableError):
        await svc.get_exchange_rate(async_session, "USD", "INR")  # never a guessed constant
    assert calls and (await async_session.execute(select(func.count()).select_from(CurrencyExchangeRate))).scalar_one() == 0
    stale_at = datetime.now(UTC) - timedelta(seconds=get_settings().CURRENCY_MAX_AGE_SECONDS + 60)
    await svc.store(async_session, {"USD": (Decimal("83.10"), "frankfurter:ecb")}, stale_at)
    with pytest.raises(CurrencyUnavailableError):
        await svc.get_exchange_rate(async_session, "USD", "INR")  # a stale rate is no rate
    board = await svc.board(async_session)
    assert "USD" in board["missing"] and board["rates"]["INR"]["inr_per_unit"] == 1.0


# ================================================================ the sandbox service
async def test_place_snaps_live_price_and_is_idempotent(async_session: AsyncSession, redis: Redis) -> None:
    user_id = await _new_user(async_session)
    now = datetime.now(UTC)
    await _seed_book(redis, "fx-ars", "Match Odds", {"HOME": "2.08"}, now=now)
    leg = _leg("fx-ars", odds="2.10", quote=("Match Odds", "HOME"))
    bet = await _place(async_session, user_id, [leg], "4000", redis=redis, now=now, client_ref="slip-1")
    assert bet.executed_odds == Decimal("2.0800") and bet.odds == Decimal("2.1000")
    assert bet.potential_payout == Decimal("8320.00") and bet.legs[0]["executed_odds"] == "2.08"
    again, created = await pts.place_paper_bet(async_session, redis, get_settings(), user_id, pts.PlaceRequest([leg], Decimal("4000"), client_ref="slip-1"), now)
    assert again.id == bet.id and created is False
    account = await _account(async_session, user_id)
    assert account.virtual_balance == Decimal("96000.00") and account.total_paper_bets == 1
    await _seed_book(redis, "fx-ars", "Match Odds", {"HOME": "1.80"}, now=now)
    with pytest.raises(pts.PaperTradingError) as moved:
        await _place(async_session, user_id, [leg], "1000", redis=redis, now=now)
    assert moved.value.code == "PRICE_MOVED" and moved.value.receipt is not None
    await async_session.rollback()
    assert (await _account(async_session, user_id)).virtual_balance == Decimal("96000.00")  # a refused slip costs nothing


async def test_placement_guards(async_session: AsyncSession, redis: Redis) -> None:
    settings = get_settings()
    user_id = await _new_user(async_session)
    cases = [
        ([_leg("a", odds="1.00")], "100", "INVALID_ODDS"),
        ([_leg("a", sport="curling")], "100", "UNKNOWN_SPORT"),
        ([_leg("a", market="corner_kick_trivia")], "100", "UNKNOWN_MARKET"),
        ([_leg("a"), _leg("a", selection="away")], "100", "RELATED_LEGS"),
        ([_leg(f"m{i}") for i in range(13)], "100", "TOO_MANY_LEGS"),
        ([_leg("a")], "0", "INVALID_STAKE"),
        ([_leg("a")], "30000", "STAKE_OVER_CAP"),  # over 25% of ₹1,00,000
        ([], "100", "NO_LEGS"),
    ]
    for legs, stake, code in cases:
        with pytest.raises(pts.PaperTradingError) as err:
            await _place(async_session, user_id, legs, stake, redis=redis)
        assert err.value.code == code, (code, err.value.message)
        await async_session.rollback()
    with pytest.raises(pts.PaperTradingError) as venue:
        await _place(async_session, user_id, [_leg("a")], "100", redis=redis, target_bookmaker="nowhere-bet")
    assert venue.value.code == "UNKNOWN_VENUE"
    await async_session.rollback()
    await redis.set(settings.CFO_KILL_SWITCH_KEY, "1")  # CHANAKYA halts paper trading too
    with pytest.raises(pts.PaperTradingError) as halted:
        await _place(async_session, user_id, [_leg("a")], "100", redis=redis)
    assert halted.value.code == "TRADING_HALTED"
    await async_session.rollback()
    await redis.delete(settings.CFO_KILL_SWITCH_KEY)
    assert (await _place(async_session, user_id, [_leg("a")], "100", redis=redis)).status == "PENDING"


async def test_parlays_half_results_and_early_loss(async_session: AsyncSession) -> None:
    user_id = await _new_user(async_session)
    legs = [_leg("p1", odds="2.00"), _leg("p2", selection="home", odds="1.90", market="asian_handicap", line=-0.25), _leg("p3", selection="over", odds="1.80",
                                                                                                                       market="total", line=2.5)]
    parlay = await _place(async_session, user_id, legs, "1000")
    assert parlay.structure == "PARLAY" and parlay.executed_odds == Decimal("6.8400") and parlay.market_type == "parlay"
    partial = await pts.settle_with_results(async_session, get_settings(), parlay.id, {"p1": {"home_score": 1, "away_score": 0}}, now=T0)
    assert partial.status == "PENDING" and partial.legs[0]["status"] == "WON"
    done = await pts.settle_with_results(async_session, get_settings(), parlay.id, {"p2": {"home_score": 1, "away_score": 1},
                                                                                    "p3": {"home_score": 2, "away_score": 1}}, now=T0)
    # 2.00 x 0.5 (AH -0.25 on a draw: half lost) x 1.80 = 1.80
    assert [leg["status"] for leg in done.legs] == ["WON", "HALF_LOST", "WON"]
    assert done.status == "HALF_WON" and done.payout == Decimal("1800.00") and done.pnl == Decimal("800.00") and done.tds_amount == Decimal("240.00")

    lost = await _place(async_session, user_id, [_leg("q1"), _leg("q2")], "500")
    settled = await pts.settle_with_results(async_session, get_settings(), lost.id, {"q1": {"home_score": 0, "away_score": 2}}, now=T0)
    assert settled.status == "LOST" and settled.payout == 0  # one losing leg settles the parlay at once

    push = await _place(async_session, user_id, [_leg("r1", market="draw_no_bet")], "700")
    pushed = await pts.settle_paper_bet(async_session, get_settings(), push.id, {"home_score": 1, "away_score": 1})
    assert pushed.status == "PUSH" and pushed.payout == Decimal("700.00") and pushed.pnl == 0
    unchanged = await pts.settle_with_results(async_session, get_settings(), push.id, {}, now=T0)  # settled already: a no-op
    assert unchanged.status == "PUSH" and unchanged.payout == Decimal("700.00")
    waiting = await _place(async_session, user_id, [_leg("s1")], "100")
    with pytest.raises(pts.PaperTradingError) as missing:
        await pts.settle_with_results(async_session, get_settings(), waiting.id, {"other": {"home_score": 1, "away_score": 0}}, now=T0)
    assert missing.value.code == "RESULT_MISSING"
    await async_session.rollback()
    account = await _account(async_session, user_id)
    assert account.won_bets == 1 and account.lost_bets == 1 and account.push_bets == 1
    assert account.virtual_balance == Decimal("100000.00") - 1000 - 500 - 700 + 1800 + 700 - 100


async def test_auto_settlement_from_recorded_scores(sessions: async_sessionmaker[AsyncSession]) -> None:
    settings = get_settings()
    async with sessions() as s:
        user_id = await _new_user(s)
        win = await _place(s, user_id, [_leg("fx-win", selection="home", odds="1.50")], "2000")
        void = await _place(s, user_id, [_leg("fx-void")], "1000")
        prop = await _place(s, user_id, [_leg("fx-prop", market="player_goals", selection="over", line=0.5)], "300")
        waiting = await _place(s, user_id, [_leg("fx-later")], "100")
        s.add_all([FixtureScore(fixture_id="fx-win", home="A", away="B", sport_key="soccer_epl", home_goals=3, away_goals=0, status="FINAL", source="admin"),
                   FixtureScore(fixture_id="fx-void", home="C", away="D", sport_key="soccer_epl", status="ABANDONED", source="admin"),
                   FixtureScore(fixture_id="fx-prop", home="E", away="F", sport_key="soccer_epl", home_goals=1, away_goals=1, status="FINAL", source="admin")])
        await s.commit()
    report = await pts.auto_settle(sessions, settings, T0 + timedelta(hours=3))
    assert report["pending"] == 4 and report["settled"] == 2 and report["waiting"] == 1
    assert [row["bet_id"] for row in report["needs_manual"]] == [str(prop.id)]
    async with sessions() as s:
        rows = {b.id: b for b in (await s.execute(select(PaperBet))).scalars()}
        assert rows[win.id].status == "WON" and rows[win.id].payout == Decimal("3000.00")
        assert rows[void.id].status == "VOID" and rows[void.id].payout == Decimal("1000.00")  # abandoned: the stake comes back
        assert rows[prop.id].status == "PENDING" and rows[waiting.id].status == "PENDING"
        assert rows[win.id].settlement_notes == "Auto-settled from the recorded final score"
    assert (await pts.auto_settle(sessions, settings, T0 + timedelta(hours=4)))["settled"] == 0  # nothing settles twice


async def test_cash_out_reset_epochs_and_performance(async_session: AsyncSession) -> None:
    settings = get_settings()
    user_id = await _new_user(async_session)
    other = await _new_user(async_session)
    a_id = (await _place(async_session, user_id, [_leg("c1", odds="3.00")], "10000", now=T0)).id  # ids survive the rollbacks below
    with pytest.raises(pts.PaperTradingError) as too_much:
        await pts.cash_out(async_session, settings, user_id, a_id, Decimal("30001"))
    assert too_much.value.code == "INVALID_CASHOUT"
    await async_session.rollback()
    with pytest.raises(pts.PaperTradingError) as not_yours:
        await pts.cash_out(async_session, settings, other, a_id, Decimal("100"))
    assert not_yours.value.code == "NOT_FOUND"
    await async_session.rollback()
    cashed = await pts.cash_out(async_session, settings, user_id, a_id, Decimal("14000"), now=T0 + timedelta(hours=1))
    assert cashed.status == "CASHED_OUT" and cashed.pnl == Decimal("4000.00") and cashed.tds_amount == Decimal("1200.00")
    with pytest.raises(pts.PaperTradingError) as twice:
        await pts.cash_out(async_session, settings, user_id, a_id, Decimal("1"))
    assert twice.value.code == "ALREADY_SETTLED"
    await async_session.rollback()
    b = await _place(async_session, user_id, [_leg("c2", odds="2.00")], "20000", now=T0 + timedelta(days=1), fair_probability=0.55)
    await pts.settle_paper_bet(async_session, settings, b.id, {"home_score": 0, "away_score": 1}, now=T0 + timedelta(days=1, hours=2))
    perf = await pts.performance(async_session, settings, user_id)
    assert perf["total_bets"] == 2 and perf["won_bets"] == 0 and perf["lost_bets"] == 1 and perf["cashed_out_bets"] == 1
    assert perf["win_rate_pct"] == 0.0 and perf["total_pnl"] == -16000.0 and perf["roi_pct"] == -16.0
    assert perf["total_tds"] == 1200.0 and perf["net_pnl_after_tds"] == -17200.0
    # equity: 100000 -> 104000 after the cash-out -> 84000 after the loss: the drawdown is measured from the 104000 peak
    assert math.isclose(perf["max_drawdown_pct"], 20000 / 104000 * 100, rel_tol=1e-4)
    assert perf["profit_factor"] == 0.2 and perf["brier_score"] == 0.3025
    assert perf["status_summary"] == "BUILDING STATISTICAL SAMPLE" and perf["live_ready"] is False
    assert perf["by_sport"]["soccer"]["bets"] == 2 and perf["developer_credit"] == DEVELOPER
    assert [p["equity"] for p in perf["equity_curve"]] == [100000.0, 100000.0, 104000.0, 104000.0, 84000.0]
    pending = await _place(async_session, user_id, [_leg("c3")], "1000", now=T0 + timedelta(days=2))
    account = await pts.reset_account(async_session, settings, user_id, now=T0 + timedelta(days=3))
    assert account.resets == 1 and account.virtual_balance == Decimal("100000.00") and account.max_drawdown_pct == 0
    assert (await pts.get_bet(async_session, pending.id, user_id)).status == "VOID"
    fresh = await pts.performance(async_session, settings, user_id)
    assert fresh["total_bets"] == 0 and fresh["epoch"] == 1 and fresh["max_drawdown_pct"] == 0.0
    assert fresh["equity_curve"] == [{"at": fresh["equity_curve"][0]["at"], "equity": 100000.0}]
    assert await pts.list_bets(async_session, account) == []
    ledger = await pts.tax_ledger(async_session, user_id, financial_year(T0))
    assert len(ledger["entries"]) == 1 and ledger["by_financial_year"]["2026-27"]["tds"] == 1200.0


async def test_disciplined_paper_bet_pays_xp_once(async_session: AsyncSession) -> None:
    user_id = await _new_user(async_session)
    bet = await _place(async_session, user_id, [_leg("x1", odds="2.10")], "1000", fair_probability=0.58, client_ref="xp-1")
    assert bet.kelly_stake is not None and bet.kelly_stake > Decimal("1000")
    assert bet.discipline["disciplined"] is True and bet.discipline["xp_awarded"] > 0
    await _place(async_session, user_id, [_leg("x1", odds="2.10")], "1000", fair_probability=0.58, client_ref="xp-1")  # idempotent: no second award
    paid = (await async_session.execute(select(func.count()).select_from(XPAuditLog).where(XPAuditLog.action_type == "DISCIPLINED_BET"))).scalar_one()
    assert paid == 1
    reckless = await _place(async_session, user_id, [_leg("x2", odds="1.50")], "5000", fair_probability=0.50)
    assert reckless.discipline["disciplined"] is False and "NEGATIVE_EV" in reckless.discipline["flags"]


# ================================================================ the API (SQLite and PostgreSQL)
@pytest_asyncio.fixture
async def api(sessions: async_sessionmaker[AsyncSession], redis: Redis) -> AsyncIterator[dict[str, Any]]:
    async with sessions() as s:
        users = {}
        for role in ("QUANT", "ADMIN", "OTHER"):
            uid = await _new_user(s, "ADMIN" if role == "ADMIN" else "QUANT")
            users[role] = await s.get(User, uid)
    caller = {"user": users["QUANT"]}

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    previous_redis = getattr(app.state, "redis", None)
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
    app.state.redis = redis
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def as_role(role: str) -> None:
        caller["user"] = users[role]

    try:
        yield {"client": client, "as_role": as_role, "users": users, "sessions": sessions, "redis": redis}
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.state.redis = previous_redis


async def test_paper_trading_endpoints(api: dict[str, Any]) -> None:
    """Tests FastAPI router endpoints via httpx ASGITransport (the brief's three, then the rest)."""
    client: httpx.AsyncClient = api["client"]
    settings = get_settings()
    resp = await client.get("/api/v1/paper-trading/account")
    assert resp.status_code == 200
    data = resp.json()
    assert data["currency"] == "INR"
    assert data["virtual_balance"] == 100000.0
    assert data["developer_credit"] == DEVELOPER

    now = datetime.now(UTC).isoformat()
    await api["redis"].hset(settings.CURRENCY_CACHE_KEY, "USD_INR", json.dumps({"rate": "84.10", "as_of": now, "provider": "frankfurter:ecb"}))
    resp_fx = await client.get("/api/v1/paper-trading/currency/rate?base=USD&quote=INR")
    assert resp_fx.status_code == 200
    fx_data = resp_fx.json()
    assert fx_data["rate"] == 84.1
    assert fx_data["developer_credit"] == DEVELOPER
    inverse = (await client.get("/api/v1/paper-trading/currency/convert?amount=8410&from=INR&to=USD")).json()
    assert inverse["converted"] == 100.0 and inverse["to_currency"] == "USD"
    assert (await client.get("/api/v1/paper-trading/currency/rate?base=JPY&quote=INR")).status_code == 422

    resp_tax = await client.get("/api/v1/paper-trading/tax/estimate?gross_win=30000&stake=10000")
    assert resp_tax.status_code == 200
    tax_data = resp_tax.json()
    assert tax_data["gross_profit"] == 20000.0
    assert tax_data["tds_deducted"] == 6000.0
    assert tax_data["net_take_home"] == 24000.0
    assert tax_data["jurisdiction"] == "IN (Section 115BBJ)" and tax_data["cess"] == 240.0
    assert tax_data["developer_credit"] == DEVELOPER


async def test_place_settle_and_read_through_the_api(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    await _seed_book(api["redis"], "fx-api", "Match Odds", {"HOME": "2.02"})
    single = {"sport_key": "soccer", "match_id": "fx-api", "event_name": "Arsenal vs Chelsea", "market_type": "moneyline", "selection": "home",
              "odds": 2.0, "stake_amount": 5000, "quote_market": "Match Odds", "quote_selection": "HOME", "client_ref": "api-1"}
    placed = await client.post("/api/v1/paper-trading/place-bet", json=single)
    assert placed.status_code == 201, placed.text
    bet = placed.json()
    assert bet["executed_odds"] == 2.02 and bet["execution"]["commander"] == "ASHVATTHAMA" and bet["created"] is True
    assert bet["developer_credit"] == DEVELOPER and bet["legs"][0]["quote_selection"] == "HOME"
    replay = (await client.post("/api/v1/paper-trading/place-bet", json=single)).json()
    assert replay["id"] == bet["id"] and replay["created"] is False
    bad = await client.post("/api/v1/paper-trading/place-bet", json={**single, "legs": [single | {"stake_amount": None}]})
    assert bad.status_code == 422
    broke = await client.post("/api/v1/paper-trading/place-bet", json={**single, "client_ref": "api-2", "stake_amount": 500000})
    assert broke.status_code == 400 and broke.json()["detail"]["code"] == "INSUFFICIENT_FUNDS"

    listed = (await client.get("/api/v1/paper-trading/bets?status=PENDING")).json()
    assert [b["id"] for b in listed] == [bet["id"]]
    api["as_role"]("OTHER")
    assert (await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/settle", json={"match_result": {"home_score": 1, "away_score": 0}})).status_code == 404
    api["as_role"]("QUANT")
    assert (await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/settle", json={})).status_code == 422
    settled = await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/settle", json={"match_result": {"home_score": 1, "away_score": 0},
                                                                                         "settlement_notes": "1-0"})
    assert settled.status_code == 200 and settled.json()["status"] == "WON" and settled.json()["tds_amount"] == 1530.0
    perf = (await client.get("/api/v1/paper-trading/performance")).json()
    assert perf["won_bets"] == 1 and perf["virtual_balance"] == 105100.0 and perf["significance"]["sample"] == 1
    ledger = (await client.get("/api/v1/paper-trading/tax/ledger")).json()
    assert ledger["entries"][0]["tds_amount"] == 1530.0 and ledger["developer_credit"] == DEVELOPER
    assert (await client.get("/api/v1/paper-trading/tax/ledger?financial_year=FY26")).status_code == 422
    reset = (await client.post("/api/v1/paper-trading/reset", json={"initial_balance": 50000})).json()
    assert reset["virtual_balance"] == 50000.0 and reset["resets"] == 1
    assert (await client.post("/api/v1/paper-trading/reset")).json()["virtual_balance"] == 100000.0


async def test_oracle_slip_live_check_cashout_and_ticket(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    await _seed_book(api["redis"], "fx-or", "Match Odds", {"HOME": "1.95"})
    slip = {"legs": [{"sport_key": "soccer_epl", "match_id": "fx-or", "market": "Match Odds", "selection": "HOME", "odds": 1.96,
                      "event_name": "Liverpool vs Everton"}], "stake_amount": 2000, "fair_probability": 0.56, "slip_id": "ashoka-77"}
    placed = await client.post("/api/v1/paper-trading/place-oracle-slip", json=slip)
    assert placed.status_code == 201, placed.text
    bet = placed.json()
    assert bet["source"] == "ASHOKA" and bet["executed_odds"] == 1.95 and bet["market_type"] == "moneyline" and bet["selection"] == "home"
    assert bet["client_ref"] == "ashoka:ashoka-77" and bet["strategy"] == "ashoka"
    assert (await client.post("/api/v1/paper-trading/place-oracle-slip", json=slip)).json()["id"] == bet["id"]
    unknown = await client.post("/api/v1/paper-trading/place-oracle-slip", json={**slip, "slip_id": "x", "legs": [{**slip["legs"][0], "market": "Corners 9.5"}]})
    assert unknown.status_code == 422 and unknown.json()["detail"]["code"] == "UNKNOWN_MARKET"

    live = {"home_stats": {"attack_rating": 1.4, "defense_rating": 1.0}, "away_stats": {"attack_rating": 1.2, "defense_rating": 1.1},
            "live_state": {"minute": 80, "home_goals": 0, "away_goals": 2}}
    check = await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/live-check", json=live)
    assert check.status_code == 200, check.text
    body = check.json()
    assert body["side"] == "home" and body["live_probability"] < 0.15 and body["alert"] is True and body["alert_threshold"] == 0.15
    assert body["message"].startswith("Cash out") and body["shield"]["advice"] == "CASH_OUT"
    calm = (await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/live-check", json={**live, "live_state": {"minute": 20, "home_goals": 1, "away_goals": 0}})).json()
    assert calm["alert"] is False and calm["message"].startswith("Hold")
    cashed = await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/cashout", json={"amount": 300})
    assert cashed.status_code == 200 and cashed.json()["status"] == "CASHED_OUT" and cashed.json()["pnl"] == -1700.0
    assert (await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/cashout", json={"amount": 300})).status_code == 409
    assert (await client.post(f"/api/v1/paper-trading/bets/{bet['id']}/live-check", json=live)).status_code == 409

    ticket = await client.post("/api/v1/paper-trading/execution/live-ticket", json={"odds": 1.95, "stake_amount": 2000, "description": "Liverpool to win"})
    assert ticket.status_code == 200 and ticket.json()["venue_url"] == "https://parimatchs1.com/en/all-live" and ticket.json()["status"] == "TICKET"
    assert (await client.post("/api/v1/paper-trading/execution/live-ticket", json={"odds": 1.95, "stake_amount": 1, "description": "x",
                                                                                  "target_bookmaker": "nowhere"})).status_code == 422


async def test_currency_refresh_is_admin_only_and_fails_closed(api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client: httpx.AsyncClient = api["client"]

    async def fake_fetch(self: CurrencyService) -> dict[str, tuple[Decimal, str]]:
        return {"USD": (Decimal("83.50"), "frankfurter:ecb"), "BTC": (Decimal("5600000"), "coingecko")}

    assert (await client.post("/api/v1/paper-trading/currency/refresh")).status_code == 403
    monkeypatch.setattr(CurrencyService, "_fetch", fake_fetch)
    api["as_role"]("ADMIN")
    board = (await client.post("/api/v1/paper-trading/currency/refresh")).json()
    assert board["rates"]["USD"]["inr_per_unit"] == 83.5 and board["rates"]["BTC"]["symbol"] == "₿"
    assert set(board["missing"]) == {"EUR", "GBP", "USDT"} and board["developer_credit"] == DEVELOPER

    async def nothing(self: CurrencyService) -> dict[str, tuple[Decimal, str]]:
        return {}

    monkeypatch.setattr(CurrencyService, "_fetch", nothing)
    down = await client.get("/api/v1/paper-trading/currency/rate?base=EUR&quote=INR")
    assert down.status_code == 503 and "EUR" in down.json()["detail"]


# ================================================================ the fleet: beat, commander #17
def test_celery_beat_and_tasks_registered() -> None:
    import app.workers.paper_trading_tasks  # noqa: F401, PLC0415 - registers the tasks

    settings = get_settings()
    assert "paper_trading.settle_pending_paper_bets" in celery_app.tasks and "currency.refresh_rates" in celery_app.tasks
    beat = celery_app.conf.beat_schedule
    assert beat["paper-trading-auto-settlement"]["task"] == "paper_trading.settle_pending_paper_bets"
    assert beat["paper-trading-auto-settlement"]["schedule"] == 60.0 == float(settings.PAPER_SETTLE_INTERVAL_SECONDS)
    assert beat["currency-refresh-rates"]["schedule"] == float(settings.CURRENCY_REFRESH_SECONDS)
    assert "app.workers.paper_trading_tasks" in celery_app.conf.include


def test_ashvatthama_closes_the_roster() -> None:
    roster = [bot.value for bot in LegendaryBot]
    # 16 since Group 96 retired GARUDA; ASHVATTHAMA is still the last commander
    assert len(roster) == 16 and roster[-1] == "ASHVATTHAMA" and "DEVRAYA" not in roster and "GARUDA" not in roster
    assert LegendaryBot.ASHVATTHAMA in PROBES and set(PROBES) == set(LegendaryBot)
    assert Decimal(get_settings().PAPER_DEFAULT_BALANCE_INR) == Decimal("100000")
