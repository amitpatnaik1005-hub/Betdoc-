"""
Integration and Unit Tests for Group 95 Real-Money Live Betting Gateway.
Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

Validates:
1. TODAR MAL's live cash vault (INR, exact Decimal Numeric(18, 2), non-negative constraints).
2. Immutable double-entry cash ledger (DEPOSIT, WITHDRAWAL, BET_STAKE_LOCKED, BET_PAYOUT_CREDIT,
   TDS_WITHHOLDING_DEBIT, BET_REFUND, BET_STAKE_FORFEITED).
3. KUMBHA's fractional Kelly stake sizing (clamped between 0.20-0.50 and 5% max bankroll ceiling).
4. ASHVATTHAMA's Parimatch execution ticket generation, deep links, and 1-click clipboard slips.
5. Minimum profitable odds floor enforcement and trader decay override.
6. Section 115BBJ 30% TDS withholding on net winnings per winning wager.
7. SHIVAJI's circuit breakers: 3 consecutive loss 30-minute cool-down & 15% daily drawdown stop-loss.
8. Group 79 sport adapter scorecard auto-settlement.
9. FastAPI endpoints under /real-money with X-Engineered-For header.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from fastapi import FastAPI

from app.api.deps import get_current_user, get_db
from app.api.v1.real_money import router as real_money_router
from app.core.config import Settings, get_settings
from app.models import User
from app.models.tax_and_fx import TaxLedgerEntry
from app.models.real_money import (
    RealBankroll,
    RealBetTicket,
    RealBookmaker,
    RealCashTransaction,
    RealTicketStatus,
    RealTransactionType,
)
from app.models.consultant import ConsultantVerdictModel
from app.models.user_bets_ledger import FixtureScore, UserPlacedBet
from app.services import real_money_service as rms

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"

TABLES: list[Table] = [
    User.__table__,  # type: ignore[list-item]
    ConsultantVerdictModel.__table__,  # type: ignore[list-item]  - Group 114: a ticket links its verdict
    UserPlacedBet.__table__,  # type: ignore[list-item]  - and its Oracle bet
    RealBankroll.__table__,  # type: ignore[list-item]
    RealBetTicket.__table__,  # type: ignore[list-item]
    RealCashTransaction.__table__,  # type: ignore[list-item]
    TaxLedgerEntry.__table__,  # type: ignore[list-item]
    FixtureScore.__table__,  # type: ignore[list-item]
]


def _build_metadata() -> MetaData:
    md = MetaData()
    for t in TABLES:
        t.to_metadata(md)
    return md


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(_build_metadata().create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


@pytest_asyncio.fixture
async def db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def test_user(db: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        username="trader_amit",
        hashed_password="hashed_pw_test",
        role="QUANT",
    )
    db.add(user)
    await db.commit()
    return user


@pytest.fixture
def settings() -> Settings:
    s = get_settings()
    s.REAL_MAX_STAKE_PCT = 0.05
    s.REAL_MIN_EDGE = 0.015
    s.REAL_DAILY_DRAWDOWN_PCT = 0.15
    s.REAL_LOSS_STREAK_LIMIT = 3
    s.REAL_COOLDOWN_MINUTES = 30
    return s


# =========================================================================
# 1. Bankroll & Double-Entry Ledger Unit Tests
# =========================================================================

@pytest.mark.asyncio
async def test_bankroll_creation_and_defaults(db: AsyncSession, test_user: User, settings: Settings) -> None:
    now = datetime.now(UTC)
    bankroll = await rms.get_or_create_bankroll(db, test_user.id, now)
    await db.commit()

    assert bankroll.user_id == test_user.id
    assert bankroll.currency == "INR"
    assert bankroll.cash_balance == Decimal("0.00")
    assert bankroll.locked_exposure == Decimal("0.00")
    assert bankroll.consecutive_losses == 0
    assert not bankroll.stop_loss_triggered

    view = rms.bankroll_view(bankroll, settings, now)
    assert view["currency"] == "INR"
    assert view["cash_balance"] == 0.0
    assert view["developer_name"] == DEVELOPER_CREDIT


@pytest.mark.asyncio
async def test_deposit_liquid_cash(db: AsyncSession, test_user: User) -> None:
    await rms.deposit(db, test_user.id, Decimal("50000.00"), reference="UPI/IMPS Deposit")
    await db.commit()

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.cash_balance == Decimal("50000.00")
    assert bankroll.total_deposited == Decimal("50000.00")

    txs = await rms.get_cash_transactions(db, test_user.id)
    assert len(txs) == 1
    assert txs[0].transaction_type == RealTransactionType.DEPOSIT.value
    assert txs[0].amount == Decimal("50000.00")
    assert txs[0].balance_before == Decimal("0.00")
    assert txs[0].balance_after == Decimal("50000.00")


@pytest.mark.asyncio
async def test_withdraw_liquid_cash_and_insufficient_funds(db: AsyncSession, test_user: User) -> None:
    await rms.deposit(db, test_user.id, Decimal("20000.00"))
    await db.commit()

    # Valid withdrawal
    await rms.withdraw(db, test_user.id, Decimal("8000.00"))
    await db.commit()

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.cash_balance == Decimal("12000.00")
    assert bankroll.total_withdrawn == Decimal("8000.00")

    # Insufficient funds
    with pytest.raises(rms.RealMoneyError) as exc_info:
        await rms.withdraw(db, test_user.id, Decimal("15000.00"))
    assert exc_info.value.code == "INSUFFICIENT_FUNDS"
    assert exc_info.value.status == 400


# =========================================================================
# 2. Odds Floor & Kelly Sizing Tests
# =========================================================================

def test_calculate_odds_floor() -> None:
    # p = 0.50, min_edge = 0.015 -> (1 / 0.50) * 1.015 = 2.03
    floor = rms.calculate_odds_floor(0.50, Decimal("0.015"))
    assert floor == Decimal("2.0300")

    # p = 0.25 -> (1 / 0.25) * 1.015 = 4.06
    floor2 = rms.calculate_odds_floor(0.25, Decimal("0.015"))
    assert floor2 == Decimal("4.0600")


@pytest.mark.asyncio
async def test_fractional_kelly_sizing(db: AsyncSession, test_user: User, settings: Settings) -> None:
    await rms.deposit(db, test_user.id, Decimal("100000.00"))
    await db.commit()

    # Bankroll ₹1,00,000, target_odds = 2.00, p = 0.55 (b = 1.00, edge = 0.10)
    # Full Kelly: (0.55 * 1 - 0.45) / 1 = 0.10 (10%)
    # Soccer fraction = 0.25 -> 2.5% of liquid cash = ₹2,500 (below 5% ceiling)
    req1 = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_kelly_1",
        event_name="Kelly Match 1",
        market_type="Match Odds",
        selection="Home",
        target_odds=Decimal("2.00"),
        true_probability=0.55,
    )
    t1 = await rms.create_execution_ticket(db, None, settings, test_user.id, req1, datetime.now(UTC))
    assert t1.recommended_stake == Decimal("3500.00")
    assert t1.kelly_fraction == 0.35

    # With huge edge, Kelly would exceed 5%, verify clamped to ₹5,000 (5%)
    req2 = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_kelly_2",
        event_name="Kelly Match 2",
        market_type="Match Odds",
        selection="Home",
        target_odds=Decimal("4.00"),
        true_probability=0.80,
    )
    t2 = await rms.create_execution_ticket(db, None, settings, test_user.id, req2, datetime.now(UTC))
    assert t2.recommended_stake == Decimal("5000.00")


# =========================================================================
# 3. Execution Ticket Creation & Placement Confirmation
# =========================================================================

@pytest.mark.asyncio
async def test_create_execution_ticket_parimatch(db: AsyncSession, test_user: User, settings: Settings) -> None:
    await rms.deposit(db, test_user.id, Decimal("100000.00"))
    await db.commit()

    now = datetime.now(UTC)
    req = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_ars_che",
        event_name="Arsenal vs Chelsea",
        market_type="Match Odds",
        selection="Arsenal",
        target_odds=Decimal("2.20"),
        true_probability=0.52,
        bookmaker="parimatch",
    )
    ticket = await rms.create_execution_ticket(db, None, settings, test_user.id, req, now)
    await db.commit()

    assert ticket.ticket_reference.startswith("PM-REAL-")
    assert ticket.bookmaker == RealBookmaker.PARIMATCH.value
    assert ticket.status == RealTicketStatus.PENDING_PLACEMENT.value
    assert ticket.recommended_stake > Decimal("0.00")
    assert ticket.minimum_profitable_odds == rms.calculate_odds_floor(0.52, Decimal("0.015"))
    assert "parimatchs1.com" in ticket.deep_link_url
    assert "Arsenal" in ticket.clipboard_payload
    assert DEVELOPER_CREDIT in ticket.clipboard_payload


@pytest.mark.asyncio
async def test_confirm_placement_locks_stake(db: AsyncSession, test_user: User, settings: Settings) -> None:
    await rms.deposit(db, test_user.id, Decimal("100000.00"))
    await db.commit()

    req = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_ars_che",
        event_name="Arsenal vs Chelsea",
        market_type="Match Odds",
        selection="Arsenal",
        target_odds=Decimal("2.20"),
        true_probability=0.52,
    )
    ticket = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
    await db.commit()

    # Confirm placement with stake ₹2,000
    confirmed = await rms.confirm_placement(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        placed_odds=Decimal("2.20"),
        placed_stake=Decimal("2000.00"),
        slip_id="PM-12345",
    )
    await db.commit()

    assert confirmed.status == RealTicketStatus.PLACED.value
    assert confirmed.placed_odds == Decimal("2.2000")
    assert confirmed.placed_stake == Decimal("2000.00")

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.cash_balance == Decimal("98000.00")
    assert bankroll.locked_exposure == Decimal("2000.00")

    # Check ledger
    txs = await rms.get_cash_transactions(db, test_user.id)
    placed_tx = next(t for t in txs if t.transaction_type == RealTransactionType.BET_STAKE_LOCKED.value)
    assert placed_tx.amount == Decimal("2000.00")
    assert placed_tx.balance_before == Decimal("100000.00")
    assert placed_tx.balance_after == Decimal("98000.00")


@pytest.mark.asyncio
async def test_confirm_placement_odds_decay_blocked_without_override(
    db: AsyncSession, test_user: User, settings: Settings
) -> None:
    await rms.deposit(db, test_user.id, Decimal("50000.00"))
    await db.commit()

    req = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_ars_che",
        event_name="Arsenal vs Chelsea",
        market_type="Match Odds",
        selection="Arsenal",
        target_odds=Decimal("2.20"),
        true_probability=0.52,  # floor is ~1.9519
    )
    ticket = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
    await db.commit()

    # Attempt to place at 1.80 (below floor) without override
    with pytest.raises(rms.RealMoneyError) as exc_info:
        await rms.confirm_placement(
            db=db,
            settings=settings,
            user_id=test_user.id,
            ticket_ref=ticket.ticket_reference,
            placed_odds=Decimal("1.80"),
            placed_stake=Decimal("1000.00"),
            override_odds_decay=False,
        )
    assert exc_info.value.code == "ODDS_DECAYED"

    # With override, succeeds
    confirmed = await rms.confirm_placement(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        placed_odds=Decimal("1.80"),
        placed_stake=Decimal("1000.00"),
        override_odds_decay=True,
    )
    await db.commit()
    assert confirmed.status == RealTicketStatus.PLACED.value
    assert confirmed.odds_decay_flag is True


# =========================================================================
# 4. Settlement & Section 115BBJ 30% TDS Tests
# =========================================================================

@pytest.mark.asyncio
async def test_settle_ticket_won_with_section_115bbj_tds(
    db: AsyncSession, test_user: User, settings: Settings
) -> None:
    await rms.deposit(db, test_user.id, Decimal("100000.00"))
    await db.commit()

    req = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_ars_che",
        event_name="Arsenal vs Chelsea",
        market_type="Match Odds",
        selection="Arsenal",
        target_odds=Decimal("2.00"),
        true_probability=0.55,
    )
    ticket = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
    await rms.confirm_placement(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        placed_odds=Decimal("2.00"),
        placed_stake=Decimal("5000.00"),
    )
    await db.commit()

    # Pre-settlement state: cash = 95,000, exposure = 5,000
    # On WIN:
    # gross_return = 5,000 * 2.0 = 10,000
    # net_winnings = 10,000 - 5,000 = 5,000
    # 30% TDS = 1,500
    # net_profit = 3,500
    # settlement_payout = 8,500 (stake 5,000 + 3,500)
    settled = await rms.settle_ticket(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        outcome=RealTicketStatus.WON,
    )
    await db.commit()

    assert settled.status == RealTicketStatus.WON.value
    assert settled.gross_return == Decimal("10000.00")
    assert settled.tds_deducted == Decimal("1500.00")
    assert settled.net_profit == Decimal("3500.00")
    assert settled.settlement_payout == Decimal("8500.00")

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.cash_balance == Decimal("103500.00")  # 95,000 + 8,500
    assert bankroll.locked_exposure == Decimal("0.00")
    assert bankroll.realized_pnl == Decimal("3500.00")
    assert bankroll.gross_winnings == Decimal("5000.00")
    assert bankroll.total_tds_deducted == Decimal("1500.00")
    assert bankroll.consecutive_losses == 0

    # Verify TaxLedgerEntry has been booked in mode LIVE
    stmt = select(TaxLedgerEntry).where(TaxLedgerEntry.user_id == test_user.id, TaxLedgerEntry.mode == "LIVE")
    res = await db.execute(stmt)
    tax_entry = res.scalar_one()
    assert tax_entry.gross_payout == Decimal("10000.00")
    assert tax_entry.tds_amount == Decimal("1500.00")
    assert tax_entry.net_winnings == Decimal("5000.00")


@pytest.mark.asyncio
async def test_settle_ticket_lost_forfeits_stake_and_tracks_streak(
    db: AsyncSession, test_user: User, settings: Settings
) -> None:
    await rms.deposit(db, test_user.id, Decimal("50000.00"))
    await db.commit()

    req = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_ars_che",
        event_name="Arsenal vs Chelsea",
        market_type="Match Odds",
        selection="Arsenal",
        target_odds=Decimal("2.00"),
        true_probability=0.55,
    )
    ticket = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
    await rms.confirm_placement(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        placed_odds=Decimal("2.00"),
        placed_stake=Decimal("2000.00"),
    )
    await db.commit()

    settled = await rms.settle_ticket(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        outcome=RealTicketStatus.LOST,
    )
    await db.commit()

    assert settled.status == RealTicketStatus.LOST.value
    assert settled.gross_return == Decimal("0.00")
    assert settled.tds_deducted == Decimal("0.00")
    assert settled.net_profit == Decimal("-2000.00")

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.cash_balance == Decimal("48000.00")  # remained unchanged from placement
    assert bankroll.locked_exposure == Decimal("0.00")
    assert bankroll.realized_pnl == Decimal("-2000.00")
    assert bankroll.daily_realized_loss == Decimal("2000.00")
    assert bankroll.consecutive_losses == 1

    # Check ledger transaction: BET_STAKE_FORFEITED
    txs = await rms.get_cash_transactions(db, test_user.id)
    forfeit_tx = next(t for t in txs if t.transaction_type == RealTransactionType.BET_STAKE_FORFEITED.value)
    assert forfeit_tx.amount == Decimal("2000.00")
    assert forfeit_tx.balance_before == Decimal("48000.00")
    assert forfeit_tx.balance_after == Decimal("48000.00")


@pytest.mark.asyncio
async def test_settle_ticket_push_refunds_full_stake(
    db: AsyncSession, test_user: User, settings: Settings
) -> None:
    await rms.deposit(db, test_user.id, Decimal("50000.00"))
    await db.commit()

    req = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_ars_che",
        event_name="Arsenal vs Chelsea",
        market_type="Draw No Bet",
        selection="Arsenal",
        target_odds=Decimal("1.80"),
        true_probability=0.60,
    )
    ticket = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
    await rms.confirm_placement(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        placed_odds=Decimal("1.80"),
        placed_stake=Decimal("2000.00"),
    )
    await db.commit()

    settled = await rms.settle_ticket(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        outcome=RealTicketStatus.PUSH,
    )
    await db.commit()

    assert settled.status == RealTicketStatus.PUSH.value
    assert settled.net_profit == Decimal("0.00")
    assert settled.settlement_payout == Decimal("2000.00")

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.cash_balance == Decimal("50000.00")  # refunded in full
    assert bankroll.locked_exposure == Decimal("0.00")


# =========================================================================
# 5. SHIVAJI Circuit Breakers: 30m Cool-Down & 15% Daily Drawdown
# =========================================================================

@pytest.mark.asyncio
async def test_3_consecutive_losses_trigger_30m_cooldown(
    db: AsyncSession, test_user: User, settings: Settings
) -> None:
    await rms.deposit(db, test_user.id, Decimal("50000.00"))
    await db.commit()

    # Place and lose 3 bets
    for i in range(3):
        req = rms.TicketRequest(
            sport="soccer",
            league="Premier League",
            match_id=f"pl_match_{i}",
            event_name=f"Match {i}",
            market_type="Match Odds",
            selection="Home",
            target_odds=Decimal("2.00"),
            true_probability=0.55,
        )
        t = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
        await rms.confirm_placement(
            db=db,
            settings=settings,
            user_id=test_user.id,
            ticket_ref=t.ticket_reference,
            placed_odds=Decimal("2.00"),
            placed_stake=Decimal("100.00"),
        )
        await rms.settle_ticket(
            db=db,
            settings=settings,
            user_id=test_user.id,
            ticket_ref=t.ticket_reference,
            outcome=RealTicketStatus.LOST,
        )
        await db.commit()

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.consecutive_losses == 3
    assert bankroll.cooldown_until is not None
    cooldown = bankroll.cooldown_until
    if cooldown.tzinfo is None:
        cooldown = cooldown.replace(tzinfo=UTC)
    assert cooldown > datetime.now(UTC)

    # Attempting to create another ticket is blocked
    req4 = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_match_4",
        event_name="Match 4",
        market_type="Match Odds",
        selection="Home",
        target_odds=Decimal("2.00"),
        true_probability=0.55,
    )
    with pytest.raises(rms.RealMoneyError) as exc_info:
        await rms.create_execution_ticket(db, None, settings, test_user.id, req4, datetime.now(UTC))
    assert exc_info.value.code == "COOLDOWN_ACTIVE"
    assert exc_info.value.status == 423


@pytest.mark.asyncio
async def test_15pct_daily_drawdown_latches_stop_loss(
    db: AsyncSession, test_user: User, settings: Settings
) -> None:
    # Starting bankroll = ₹10,000. 15% stop-loss threshold = ₹1,500.
    await rms.deposit(db, test_user.id, Decimal("10000.00"))
    await db.commit()

    # Place losses accumulating to 1,600 (> 15% of 10,000)
    # Interleave a winning bet so consecutive loss streak stays under 3 (isolating stop-loss from cooldown)
    plan = [
        ("loss_0", Decimal("400.00"), RealTicketStatus.LOST),
        ("loss_1", Decimal("400.00"), RealTicketStatus.LOST),
        ("win_reset", Decimal("50.00"), RealTicketStatus.WON),  # resets streak, daily loss stays 800
        ("loss_2", Decimal("400.00"), RealTicketStatus.LOST),
        ("loss_3", Decimal("400.00"), RealTicketStatus.LOST),   # daily loss reaches 1,600
    ]
    for match_sub, stake, outcome in plan:
        req = rms.TicketRequest(
            sport="soccer",
            league="Premier League",
            match_id=f"pl_dd_{match_sub}",
            event_name=f"Match DD {match_sub}",
            market_type="Match Odds",
            selection="Home",
            target_odds=Decimal("2.00"),
            true_probability=0.55,
        )
        t = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
        await rms.confirm_placement(
            db=db,
            settings=settings,
            user_id=test_user.id,
            ticket_ref=t.ticket_reference,
            placed_odds=Decimal("2.00"),
            placed_stake=stake,
        )
        await rms.settle_ticket(
            db=db,
            settings=settings,
            user_id=test_user.id,
            ticket_ref=t.ticket_reference,
            outcome=outcome,
        )
        await db.commit()

    bankroll = await rms.get_or_create_bankroll(db, test_user.id)
    assert bankroll.stop_loss_triggered is True
    assert bankroll.stop_loss_until is not None

    # Next ticket creation is blocked by daily stop loss
    req2 = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_next",
        event_name="Match Next",
        market_type="Match Odds",
        selection="Home",
        target_odds=Decimal("2.00"),
        true_probability=0.55,
    )
    with pytest.raises(rms.RealMoneyError) as exc_info:
        await rms.create_execution_ticket(db, None, settings, test_user.id, req2, datetime.now(UTC))
    assert exc_info.value.code == "DAILY_STOP_LOSS_LATCHED"
    assert exc_info.value.status == 423


# =========================================================================
# 6. Scorecard Adapter Settlement Test
# =========================================================================

@pytest.mark.asyncio
async def test_settle_ticket_via_scorecard_adapter(
    db: AsyncSession, test_user: User, settings: Settings
) -> None:
    await rms.deposit(db, test_user.id, Decimal("50000.00"))
    await db.commit()

    req = rms.TicketRequest(
        sport="soccer",
        league="Premier League",
        match_id="pl_ars_che",
        event_name="Arsenal vs Chelsea",
        market_type="Match Odds",
        selection="Home",
        target_odds=Decimal("2.10"),
        true_probability=0.52,
    )
    ticket = await rms.create_execution_ticket(db, None, settings, test_user.id, req, datetime.now(UTC))
    await rms.confirm_placement(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        placed_odds=Decimal("2.10"),
        placed_stake=Decimal("1000.00"),
    )
    await db.commit()

    # Pass final match scorecard: Arsenal 2 - 1 Chelsea
    scorecard = {
        "status": "FINISHED",
        "home_score": 2,
        "away_score": 1,
        "score": {"home": 2, "away": 1},
    }
    settled = await rms.settle_ticket(
        db=db,
        settings=settings,
        user_id=test_user.id,
        ticket_ref=ticket.ticket_reference,
        scorecard=scorecard,
    )
    await db.commit()

    assert settled.status == RealTicketStatus.WON.value
    assert settled.gross_return == Decimal("2100.00")
    assert settled.tds_deducted == Decimal("330.00")  # (2,100 - 1,000) * 0.30 = 330
    assert settled.net_profit == Decimal("770.00")


# =========================================================================
# 7. End-to-End FastAPI Router Integration Tests
# =========================================================================

@pytest.mark.asyncio
async def test_e2e_real_money_api_endpoints(
    session_factory: async_sessionmaker[AsyncSession], test_user: User, settings: Settings
) -> None:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return test_user

    test_app = FastAPI()
    test_app.include_router(real_money_router, prefix="/api/v1")
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_current_user
    test_app.dependency_overrides[get_settings] = lambda: settings

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Check Bankroll
        resp = await client.get("/api/v1/real-money/bankroll")
        assert resp.status_code == 200
        assert resp.headers.get("X-Engineered-For") == DEVELOPER_CREDIT
        data = resp.json()
        assert data["currency"] == "INR"
        assert data["cash_balance"] == 0.0

        # 2. Deposit Cash
        resp_dep = await client.post("/api/v1/real-money/deposit", json={"amount": 25000.0, "reference": "UPI"})
        assert resp_dep.status_code == 200
        assert resp_dep.json()["cash_balance"] == 25000.0

        # 3. Create Ticket
        ticket_payload = {
            "sport": "soccer",
            "league": "Premier League",
            "match_id": "pl_api_match",
            "event_name": "Liverpool vs Man City",
            "market_type": "Match Odds",
            "selection": "Liverpool",
            "target_odds": 2.25,
            "true_probability": 0.50,
        }
        resp_t = await client.post("/api/v1/real-money/create-ticket", json=ticket_payload)
        assert resp_t.status_code == 201
        t_data = resp_t.json()
        ticket_ref = t_data["ticket_reference"]
        assert ticket_ref.startswith("PM-REAL-")

        # 4. Confirm Placement
        confirm_payload = {
            "ticket_reference": ticket_ref,
            "placed_odds": 2.25,
            "placed_stake": 1000.0,
            "bookmaker_slip_id": "PM-E2E-999",
        }
        resp_conf = await client.post("/api/v1/real-money/confirm-placement", json=confirm_payload)
        assert resp_conf.status_code == 200
        assert resp_conf.json()["status"] == "PLACED"

        # 5. Settle Ticket
        settle_payload = {
            "ticket_reference": ticket_ref,
            "outcome": "WON",
        }
        resp_settle = await client.post("/api/v1/real-money/settle-ticket", json=settle_payload)
        assert resp_settle.status_code == 200
        settle_data = resp_settle.json()
        assert settle_data["status"] == "WON"
        assert settle_data["gross_return"] == 2250.0
        assert settle_data["tds_deducted"] == 375.0  # (2,250 - 1,000) * 0.30
        assert settle_data["net_profit"] == 875.0

        # 6. Check Tax Certificate
        resp_tax = await client.get("/api/v1/real-money/tax-certificate")
        assert resp_tax.status_code == 200
        tax_cert = resp_tax.json()
        assert tax_cert["gross_winnings"] == 1250.0
        assert tax_cert["total_tds_withheld"] == 375.0
        assert tax_cert["section"] == "115BBJ"
        assert tax_cert["developer_name"] == DEVELOPER_CREDIT

    test_app.dependency_overrides.clear()
