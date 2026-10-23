"""Group 116, the grand capstone: the closing line value of a ticket, the Monte Carlo stress test under SHIVAJI's rules,
the shareable card's server figures, the real vault on /status, and the Gazette manual's honesty.

* CLV = placed / closing - 1, from the close the Oracle bet already records; none before kickoff.
* The stress test stakes fractional Kelly under the 5% ceiling, latches a day at a 15% loss of the day's opening
  bankroll, sits out after three straight losses, draws from its own seeded generator, and takes its win rate and odds
  from the desk's WON/LOST tickets (or from stated assumptions, labelled so). Inputs without an edge stake nothing.
* Every figure the share card prints comes from the server (Section 115BBJ's 30% on the net winnings).
Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_user, get_db
from app.api.v1.real_money import router as real_money_router
from app.core.config import Settings, get_settings
from app.models import User
from app.models.digital_twin import TwinInPlayMonitor
from app.models.real_money import RealBankroll, RealBetTicket, RealCashTransaction
from app.models.user_bets_ledger import UserPlacedBet
from app.services import handcrafted_execution_service as hes
from app.services import real_money_service as rms
from app.services import street_smart_synthesizer as synth
from app.services.bankroll_stress_test import DEVELOPER_CREDIT as STRESS_CREDIT
from app.services.bankroll_stress_test import measured_inputs, simulate_bankroll_stress_test
from app.services.clv_tracker import DEVELOPER_CREDIT as CLV_CREDIT
from app.services.clv_tracker import calculate_clv_edge, ticket_clv
from app.services.sentinel_commands import CommandCenter, TelegramUpdate

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
DEVELOPER = "Amit Ashok Kumar Patnaik"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


# ================================================================ the brief's own six checks
def test_developer_credits():
    assert CLV_CREDIT == "Amit Ashok Kumar Patnaik"
    assert STRESS_CREDIT == "Amit Ashok Kumar Patnaik"


def test_clv_positive_edge():
    # Placed at 2.10, closed at 1.90 -> +10.53% CLV edge
    res = calculate_clv_edge(placed_odds=2.10, closing_odds=1.90)
    assert res["beat_the_market"] is True
    assert res["clv_edge_pct"] > 10.0
    assert "Beat the market" in res["rationale"]


def test_clv_negative_edge():
    # Placed at 1.80, closed at 2.00 -> -10% CLV edge
    res = calculate_clv_edge(placed_odds=1.80, closing_odds=2.00)
    assert res["beat_the_market"] is False
    assert res["clv_edge_pct"] < 0
    assert "Negative CLV" in res["rationale"]


def test_clv_neutral_edge():
    res = calculate_clv_edge(placed_odds=2.00, closing_odds=2.00)
    assert res["beat_the_market"] is False
    assert res["clv_edge_pct"] == 0.0
    assert "Fair value lock" in res["rationale"]


def test_clv_invalid_odds():
    with pytest.raises(ValueError):
        calculate_clv_edge(placed_odds=0.95, closing_odds=2.00)
    with pytest.raises(ValueError):
        calculate_clv_edge(placed_odds=2.00, closing_odds=1.00)


def test_monte_carlo_bankroll_stress_test():
    res = simulate_bankroll_stress_test(
        starting_bankroll=10000.0,
        win_probability=0.58,
        average_odds=1.90,
        kelly_fraction=0.25,
        num_iterations=1000,
        bets_per_iteration=50,
        seed=42,
    )
    assert res["ruin_probability"] < 0.01
    assert res["ruin_shield_passed"] is True
    assert res["median_final_bankroll"] > 10000.0
    assert res["p95_max_drawdown_pct"] < 50.0
    assert res["architect"] == "Amit Ashok Kumar Patnaik"


# ================================================================ the stress test, as the desk stakes and guards
def test_the_stress_test_stakes_kelly_under_the_ceiling_and_leaves_the_process_rng_alone() -> None:
    random.seed(7)
    expected_next = random.random()
    random.seed(7)
    first = simulate_bankroll_stress_test(starting_bankroll=10_000, win_probability=0.58, average_odds=1.90, num_iterations=300, bets_per_iteration=40, seed=11)
    assert random.random() == expected_next  # its own generator: the process-wide one did not move
    again = simulate_bankroll_stress_test(starting_bankroll=10_000, win_probability=0.58, average_odds=1.90, num_iterations=300, bets_per_iteration=40, seed=11)
    assert first == again  # seeded: reproducible
    full = (0.9 * 0.58 - 0.42) / 0.9
    assert first["full_kelly"] == pytest.approx(full, abs=1e-6) and first["stake_fraction"] == pytest.approx(full * 0.25, abs=1e-6)
    assert first["edge_per_unit"] == pytest.approx(0.58 * 1.90 - 1, abs=1e-6)
    capped = simulate_bankroll_stress_test(starting_bankroll=10_000, win_probability=0.70, average_odds=3.0, kelly_fraction=1.0, num_iterations=10, seed=1)
    assert capped["stake_fraction"] == 0.05  # the 5% single-ticket ceiling


def test_no_edge_stakes_nothing_and_is_not_called_ruin() -> None:
    flat = simulate_bankroll_stress_test(starting_bankroll=5_000, win_probability=0.50, average_odds=1.90, num_iterations=200, seed=3)
    assert (flat["stake_fraction"], flat["ruin_probability"], flat["bets_placed"], flat["median_final_bankroll"]) == (0.0, 0.0, 0, 5000.0)
    assert flat["note"] == "These inputs carry no edge: Kelly stakes nothing, so no path bets."


def test_shivaji_latches_days_and_calls_cooldowns_on_a_losing_streak() -> None:
    harsh = simulate_bankroll_stress_test(starting_bankroll=10_000, win_probability=0.35, average_odds=4.0, kelly_fraction=1.0, num_iterations=500,
                                          bets_per_iteration=60, bets_per_day=10, seed=5)
    assert harsh["stake_fraction"] == 0.05 and harsh["cooldowns"] > 0 and harsh["stop_loss_activations"] > 0
    assert harsh["bets_placed"] < 500 * 60  # sat-out bets are not placed
    for bad in ({"win_probability": 1.0}, {"average_odds": 1.0}, {"starting_bankroll": 0.0}, {"num_iterations": 50_000}):
        with pytest.raises(ValueError):
            simulate_bankroll_stress_test(**{"starting_bankroll": 1000.0, "win_probability": 0.5, "average_odds": 2.0, **bad})


# ================================================================ CLV on a ticket, the share card's figures
def test_ticket_clv_waits_for_a_recorded_close() -> None:
    assert ticket_clv(D("2.10"), None) is None and ticket_clv(None, D("1.90")) is None
    clv = ticket_clv(D("2.10"), D("1.90"))
    assert clv is not None and clv["clv_edge_pct"] == 10.53 and clv["beat_the_market"] is True


def test_the_share_card_prints_the_server_tds() -> None:
    pending = RealBetTicket(recommended_stake=D("400.00"), placed_stake=D("0.00"), target_odds=D("2.7440"), placed_odds=None)
    assert synth.projection(pending) == {"stake_inr": "400.00", "odds": "2.7440", "gross_payout_inr": "1097.60", "net_winnings_inr": "697.60",
                                         "tds_inr": "209.28", "take_home_inr": "888.32"}
    placed = RealBetTicket(recommended_stake=D("400.00"), placed_stake=D("350.00"), target_odds=D("2.7440"), placed_odds=D("2.5000"))
    assert synth.projection(placed)["gross_payout_inr"] == "875.00" and synth.projection(placed)["tds_inr"] == "157.50"


# ================================================================ on a database: measured inputs, the endpoint, CLV, /status
@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = [m.__table__ for m in (User, UserPlacedBet, TwinInPlayMonitor, RealBankroll, RealBetTicket, RealCashTransaction)]  # type: ignore[attr-defined]
    wanted = {t.name: t for t in tables}
    pending = list(tables)
    while pending:
        table = pending.pop()
        for fk in table.foreign_keys:
            if fk.column.table.name not in wanted:
                wanted[fk.column.table.name] = fk.column.table
                pending.append(fk.column.table)
    ordered = [t for t in User.metadata.sorted_tables if t.name in wanted]
    if request.param == "postgres":
        if not TEST_POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
        engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=ordered))
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ordered))))
            await engine.dispose()
        return
    md = MetaData()
    for table in ordered:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(md.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _ticket(user_id: uuid.UUID, ref: str, status: str, odds: str) -> RealBetTicket:
    return RealBetTicket(user_id=user_id, ticket_reference=ref, bookmaker="parimatch", sport="soccer", league="EPL", match_id=f"m-{ref}", event_name="A vs B",
                         market_type="Match Odds", selection="HOME", recommended_stake=D("100.00"), placed_stake=D("100.00"), target_odds=D(odds),
                         placed_odds=D(odds), minimum_profitable_odds=D("1.50"), true_probability=0.6, model_edge_pct=1.0, deep_link_url="", clipboard_payload="",
                         settleable_from_score=False, status=status, created_at=NOW)


async def _desk(sessions: async_sessionmaker[AsyncSession], *, won: int, lost: int, cash: str = "20000.00") -> uuid.UUID:
    uid = uuid.uuid4()
    async with sessions() as db:
        db.add(User(id=uid, username=f"amit-{uid.hex[:6]}", hashed_password="x"))
        await db.flush()
        await rms.deposit(db, uid, D(cash))
        for i in range(won):
            db.add(_ticket(uid, f"W{i}-{uid.hex[:6]}", "WON", "2.00"))
        for i in range(lost):
            db.add(_ticket(uid, f"L{i}-{uid.hex[:6]}", "LOST", "1.80"))
        db.add(_ticket(uid, f"V0-{uid.hex[:6]}", "VOID", "9.00"))  # not a binary outcome: left out
        await db.commit()
    return uid


@pytest.mark.asyncio
async def test_measured_inputs_and_the_endpoint(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    uid = await _desk(sessions, won=6, lost=4)
    async with sessions() as db:
        measured = await measured_inputs(db, uid)
        user = await db.get(User, uid)
    assert (measured["settled"], measured["won"], measured["win_probability"]) == (10, 6, 0.6)
    assert measured["average_odds"] == pytest.approx((6 * 2.00 + 4 * 1.80) / 10)

    async def db_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(real_money_router, prefix="/api/v1")
    app.dependency_overrides.update({get_db: db_override, get_current_user: lambda: user, get_settings: lambda: settings})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        short = await client.post("/api/v1/real-money/stress-test", json={"iterations": 200, "bets": 20})
        assert short.status_code == 422 and short.json()["detail"]["code"] == "INSUFFICIENT_HISTORY" and short.json()["detail"]["settled"] == 10
        half = await client.post("/api/v1/real-money/stress-test", json={"win_probability": 0.55})
        assert half.status_code == 422
        assumed = await client.post("/api/v1/real-money/stress-test", json={"win_probability": 0.58, "average_odds": 1.9, "iterations": 300, "bets": 30})
        body = assumed.json()
        assert assumed.status_code == 200 and body["inputs_source"] == "ASSUMED" and body["starting_bankroll"] == 20000.0 and body["developer_name"] == DEVELOPER
        assert body["max_stake_pct"] == settings.REAL_MAX_STAKE_PCT and body["measured"]["settled"] == 10

    low = settings.model_copy(update={"STRESS_TEST_MIN_SETTLED": 10})
    app.dependency_overrides[get_settings] = lambda: low
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mine = await client.post("/api/v1/real-money/stress-test", json={"iterations": 200, "bets": 20, "starting_bankroll": 5000})
        body = mine.json()
        assert mine.status_code == 200 and body["inputs_source"] == "MEASURED" and body["win_probability"] == 0.6 and body["starting_bankroll"] == 5000.0


@pytest.mark.asyncio
async def test_a_placement_shows_its_clv_once_the_close_is_recorded(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    uid = await _desk(sessions, won=0, lost=0)
    async with sessions() as db:
        bet = UserPlacedBet(id=uuid.uuid4(), user_id=uid, source="ASHOKA", bookmaker="PARIMATCH", structure="SINGLE", stake_inr=D("100.00"), placed_odds=D("2.10"),
                            placed_at=NOW, status="PENDING")
        db.add(bet)
        await db.flush()
        ticket = _ticket(uid, "PM-CLV-1", "PLACED", "2.10")
        ticket.user_placed_bet_id = bet.id
        db.add(ticket)
        await db.commit()
        assert (await hes.ticket_placement(db, settings, ticket))["clv"] is None  # no close recorded yet
        bet.closing_odds = D("1.90")
        await db.commit()
        clv = (await hes.ticket_placement(db, settings, ticket))["clv"]
    assert clv["clv_edge_pct"] == 10.53 and clv["beat_the_market"] is True and clv["closing_odds"] == 1.9


@pytest.mark.asyncio
async def test_status_shows_the_linked_real_vault(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    uid = await _desk(sessions, won=0, lost=0, cash="12500.00")
    center = CommandCenter(sessions, None, settings, clock=lambda: NOW)
    update = TelegramUpdate(update_id=1, chat_id=42, sender_id=7, sender="amit", text="/status")
    linked = await center._real_vault_line(update, {"chat_ids": [42], "links": {"42": {"user_id": str(uid)}}}, NOW)  # noqa: SLF001
    assert linked == "Real vault: ₹12,500.00 liquid · ₹0.00 locked · SHIVAJI ✅ clear"
    unlinked = await center._real_vault_line(update, {"chat_ids": [42]}, NOW)  # noqa: SLF001
    assert unlinked.startswith("Real vault: link this chat")


@pytest.fixture
def settings() -> Settings:
    return get_settings()


# ================================================================ the Gazette and the card, as written
def test_the_gazette_states_only_what_the_product_does() -> None:
    page = (ROOT / "frontend" / "src" / "pages" / "TestingManual.tsx").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "BETDOC_OFFICIAL_GAZETTE_TESTING_MANUAL.md").read_text(encoding="utf-8")
    for source in (page, doc):
        for promise in ("Capital Shield Guarantee", "Mathematical Guarantee", "You lose nothing", "Bumrah", "every second", "CERTIFIED OPERATIONAL", "STATUTORY"):
            assert promise not in source, promise
        for fact in ("Developed for Amit Ashok Kumar Patnaik", "5(d) Street-smart", "all three tickets lose", "Form 16A", "never sees the book's cashout offer"):
            assert fact in source, fact
    assert "defaultChecked" not in page and 'data-print-root=""' in page and "<ComponentGuideBanner" in page and "/health/" in page
    css = (ROOT / "frontend" / "src" / "index.css").read_text(encoding="utf-8")
    assert "[data-print-root] *" in css  # the manual and the card print; the global print sheet hid them before
    card = (ROOT / "frontend" / "src" / "components" / "betslip" / "ShareableBetSlipCard.tsx").read_text(encoding="utf-8")
    assert "0.30" not in card and "alert(" not in card and "tdsInr: string" in card  # the server's TDS, never the browser's
    assert "<ShareableBetSlipCard" in (ROOT / "frontend" / "src" / "components" / "builder" / "StreetSmartSynthesizer.tsx").read_text(encoding="utf-8")
    assert "<BankrollStressTestPanel" in (ROOT / "frontend" / "src" / "pages" / "TheVault.tsx").read_text(encoding="utf-8")
