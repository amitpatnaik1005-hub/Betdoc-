"""Group 114: the handcrafted placement of a Bet-Slip, the in-play cashout advisor, the realised cashout with Section
194BA's 30% TDS, and the vault's reconciliation.

* BetDoc never places a bet: the user places the sealed ticket by hand and confirms it with the book's booking code.
  The confirmation checks the seal, the book, the liquid cash (400 INSUFFICIENT_FUNDS) and SHIVAJI's breakers (423), then
  locks the stake (BET_STAKE_LOCKED), records the Oracle bet that settles it leg by leg, puts the booking code on the
  Bet-Slip record and starts the twin's in-play watch with the verdict's stop-loss. A slip is placed once.
* The cashout advisor weighs the book's offer against fair value and the stop-loss floor: STOP_LOSS_BREACH,
  CASHOUT_ADVISED (85% of fair value, or the profit target), HOLD; it names where the win probability came from.
* A cashout taken: Gross = the amount; Net Winnings = max(0, Gross - stake); TDS = 30% of Net Winnings; Payout =
  Gross - TDS; P&L = Payout - stake. ₹2,200 on a ₹1,000 stake: TDS ₹360, payout ₹1,840, P&L ₹840. A loss carries no TDS.
* A placed slip settles as its Oracle bet settles; reconciliation brings locked exposure to the open tickets and checks
  liquid cash against its ledger without ever writing cash.
Every figure is an exact Decimal. Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CheckConstraint, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.deps import get_current_user, get_db
from app.api.v1.real_money import router as real_money_router
from app.core.config import Settings, get_settings
from app.domain.consultant import diagnostics as dx
from app.models import User
from app.models.cfo_vault import MarketResult
from app.models.consultant import ConsultantVerdictModel
from app.models.digital_twin import TwinInPlayMonitor
from app.models.real_money import RealBankroll, RealBetTicket, RealCashTransaction, RealTicketStatus
from app.models.slip_ornament import BetSlipTicketModel
from app.models.tax_and_fx import TaxLedgerEntry
from app.models.user_bets_ledger import FixtureScore, UserPlacedBet, UserPlacedLeg
from app.schemas.ashoka import ScoreIn
from app.services import handcrafted_execution_service as hes
from app.services import live_cashout_service as lcs
from app.services import live_result_settlement_service as settlement
from app.services import real_money_service as rms
from app.services import slip_ornament, user_pnl_tracker
from app.services import vault_reconciliation_service as vrs
from app.services.twin import inplay

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
# the desk's own calls read the clock (deposit, confirm_placement): the tests run on today's IST trading day
NOW = datetime.now(UTC).replace(microsecond=0)
KICKOFF = NOW - timedelta(hours=3)
CODE = "5X7A9B"
DEVELOPER = "Amit Ashok Kumar Patnaik"
MODELS = (User, ConsultantVerdictModel, BetSlipTicketModel, UserPlacedBet, UserPlacedLeg, TwinInPlayMonitor, RealBankroll, RealBetTicket, RealCashTransaction,
          TaxLedgerEntry, FixtureScore, MarketResult)


def _tables() -> list[Table]:
    """The models and every table they reference, in creation order."""
    wanted: dict[str, Table] = {}
    pending = [m.__table__ for m in MODELS]  # type: ignore[attr-defined]
    while pending:
        table = pending.pop()
        if table.name not in wanted:
            wanted[table.name] = table
            pending.extend(fk.column.table for fk in table.foreign_keys)
    return [t for t in User.metadata.sorted_tables if t.name in wanted]


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """SQLite, and PostgreSQL where the real CHECK constraints (CASHED_OUT among them) and the unique verdict apply."""
    tables = _tables()
    if request.param == "postgres":
        if not TEST_POSTGRES_URL:
            pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
        engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=tables))
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(tables))))
            await engine.dispose()
        return
    md = MetaData()
    for table in tables:
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


@pytest.fixture
def settings() -> Settings:
    return get_settings()


# ================================================================ a signed verdict, as the Consultant writes it
def _report(report_id: uuid.UUID, *, book: str = "parimatch", stake: str = "1000.00", **slip_over: Any) -> dict[str, Any]:
    legs = [
        {"leg_id": "fx-ars-che|Match Odds|HOME", "fixture": "Arsenal vs Chelsea", "label": "1X2: Arsenal", "market": "Match Odds", "selection": "HOME",
         "sport_key": "soccer_epl", "league": "EPL", "kickoff": KICKOFF.isoformat(), "price": 2.45, "fair_probability": 0.4339, "ev": 0.0631},
        {"leg_id": "fx-liv-eve|Totals 2.5|OVER", "fixture": "Liverpool vs Everton", "label": "Totals: Over 2.5", "market": "Totals 2.5", "selection": "OVER",
         "sport_key": "soccer_epl", "league": "EPL", "kickoff": KICKOFF.isoformat(), "price": 1.95, "fair_probability": 0.55, "ev": 0.0725},
    ]
    return {
        "report_id": str(report_id), "signed_at": (NOW - timedelta(minutes=10)).isoformat(), "signed_by": "ASHVATTHAMA · BetDoc Consultant",
        "verdict": {"status": "APPROVED_GOLD", "title": "APPROVED — GOLD GRADE", "summary": "Every enforced pillar passed."},
        "slip": {"slip_id": "slip-g114", "kind": "DOUBLE", "title": "ASHOKA Double", "book": book, "odds": "4.7775", "joint_ev": 0.14,
                 "joint_probability": 0.24, "legs": legs, **slip_over},
        "rating": {"score": 96.0, "tier": "DIAMOND"},
        "consultant": {"stake_inr": stake, "stake_usd": None, "fx": {"inr_per_usd": None}, "payout_inr": "4777.50", "kelly_fraction": 0.25,
                       "tax": {"net_winnings": 3777.5, "tds": 1133.25, "net_post_tax_payout": 3644.25, "tds_rate_pct": 30.0},
                       "stop_loss": {"pct": 0.3536, "floor_inr": "646.40"}},
        "developer_credit": DEVELOPER,
    }


async def _seed(sessions: async_sessionmaker[AsyncSession], settings: Settings, *, cash: str = "25000.00", book: str = "parimatch", stake: str = "1000.00",
                seal_holds: bool = True, **slip_over: Any) -> SimpleNamespace:
    """A user with ₹cash deposited in the vault (through the ledger) and one sealed, approved verdict."""
    ids = SimpleNamespace(user=uuid.uuid4(), verdict=uuid.uuid4())
    report = _report(ids.verdict, book=book, stake=stake, **slip_over)
    seal = dx.seal(report, settings.SECRET_KEY.get_secret_value())
    async with sessions() as db:
        db.add(User(id=ids.user, username=f"amit-{ids.user.hex[:6]}", hashed_password="x"))
        db.add(ConsultantVerdictModel(id=ids.verdict, user_id=ids.user, slip_id="slip-g114", leg_ids=[leg["leg_id"] for leg in report["slip"]["legs"]],
                                      bookmaker=book, verdict="APPROVED_GOLD", slip_score=96.0, tier="DIAMOND", report=report,
                                      seal=seal if seal_holds else "0" * 64, signed_at=NOW - timedelta(minutes=10)))
        await db.flush()
        if D(cash) > 0:
            await rms.deposit(db, ids.user, D(cash))
        else:
            await rms.get_or_create_bankroll(db, ids.user, NOW)
        await db.commit()
    return ids


async def _place(sessions: async_sessionmaker[AsyncSession], settings: Settings, ids: SimpleNamespace, *, code: str = CODE, redis: Any = None,
                 **kwargs: Any) -> hes.Placement:
    async with sessions() as db:
        try:
            placement = await hes.confirm_slip_placement(db, redis, settings, ids.user, ids.verdict, code, NOW, **kwargs)
            await db.commit()
        except rms.RealMoneyError:
            await db.rollback()
            raise
        return placement


async def _refusal(sessions: async_sessionmaker[AsyncSession], settings: Settings, ids: SimpleNamespace, **kwargs: Any) -> rms.RealMoneyError:
    with pytest.raises(rms.RealMoneyError) as refused:
        await _place(sessions, settings, ids, **kwargs)
    return refused.value


async def _bankroll(db: AsyncSession, user_id: uuid.UUID) -> RealBankroll:
    return (await db.execute(select(RealBankroll).where(RealBankroll.user_id == user_id))).scalar_one()


async def _count(db: AsyncSession, model: Any, *where: Any) -> int:
    return int((await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


class _KillSwitchRedis:
    """Just enough Redis for the gate: the CFO kill switch engaged, no book locked."""

    def __init__(self, key: str) -> None:
        self.key = key

    async def get(self, key: str) -> str | None:
        return "1" if key == self.key else None

    async def exists(self, *_: str) -> int:
        return 0


# ================================================================ placement
@pytest.mark.asyncio
async def test_a_slip_placed_by_hand_locks_its_stake_and_links_every_ledger(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    ids = await _seed(sessions, settings)
    placement = await _place(sessions, settings, ids)
    assert placement.replayed is False and placement.monitor_note is None
    t = placement.ticket
    assert t.ticket_reference.startswith("PM-SLIP-") and t.bookmaker == "parimatch" and t.status == RealTicketStatus.PLACED.value
    assert (t.placed_stake, t.placed_odds, t.recommended_stake, t.target_odds) == (D("1000.00"), D("4.7775"), D("1000.00"), D("4.7775"))
    assert t.minimum_profitable_odds == D("4.2292")  # (1 / 0.24) x 1.015, rounded up
    assert (t.verdict_id, t.bookmaker_slip_id, t.settleable_from_score, t.sport, t.league) == (ids.verdict, CODE, False, "soccer", "EPL")
    assert t.match_id == "slip:slip-g114" and t.market_type == "Double (2 legs)" and t.clipboard_payload.startswith("BETDOC BET-SLIP")
    async with sessions() as db:
        bankroll = await _bankroll(db, ids.user)
        assert (bankroll.cash_balance, bankroll.locked_exposure) == (D("24000.00"), D("1000.00"))
        lock = (await db.execute(select(RealCashTransaction).where(RealCashTransaction.ticket_id == t.id))).scalar_one()
        assert (lock.transaction_type, lock.amount, lock.balance_before, lock.balance_after) == ("BET_STAKE_LOCKED", D("1000.00"), D("25000.00"), D("24000.00"))
        bet = await db.get(UserPlacedBet, t.user_placed_bet_id)
        assert (bet.structure, bet.stake_inr, bet.placed_odds, bet.bookmaker, bet.booking_code, bet.status) == ("DOUBLE", D("1000.00"), D("4.7775"), "PARIMATCH",
                                                                                                                  CODE, "PENDING")
        legs = list((await db.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id == bet.id).order_by(UserPlacedLeg.position))).scalars())
        assert [(leg.fixture_id, leg.home, leg.away, leg.selection, leg.odds) for leg in legs] == [
            ("fx-ars-che", "Arsenal", "Chelsea", "HOME", D("2.45")), ("fx-liv-eve", "Liverpool", "Everton", "OVER", D("1.95"))]
        record = (await db.execute(select(BetSlipTicketModel).where(BetSlipTicketModel.verdict_id == ids.verdict))).scalar_one()
        assert record.booking_code == CODE and record.booking_recorded_at is not None
        monitor = (await db.execute(select(TwinInPlayMonitor).where(TwinInPlayMonitor.bet_id == bet.id))).scalar_one()
        assert monitor.is_active and monitor.target_profit_pct == settings.REAL_CASHOUT_TARGET_PROFIT_PCT == 0.80
        assert monitor.stop_loss_pct == pytest.approx(0.3536) and monitor.initial_win_prob == pytest.approx(0.4339 * 0.55)  # no live price: the signed legs
        view = await slip_ornament.view(db, settings, ids.user, ids.verdict, NOW)
    assert view["booking_code"] == CODE and view["placement"]["status"] == "PLACED" and view["placement"]["stop_loss_floor_inr"] == "646.40"
    assert view["placement"]["monitor_active"] is True and view["placement"]["ticket_reference"] == t.ticket_reference

    # a slip is placed once: the same code again moves nothing, another code is refused
    again = await _place(sessions, settings, ids)
    assert again.replayed is True and again.ticket.id == t.id and again.bankroll.cash_balance == D("24000.00")
    other = await _refusal(sessions, settings, ids, code="OTHER123")
    assert (other.code, other.status) == ("ALREADY_PLACED", 409)
    async with sessions() as db:
        assert await _count(db, RealBetTicket) == 1 and await _count(db, UserPlacedBet) == 1
        assert await _count(db, RealCashTransaction, RealCashTransaction.transaction_type == "BET_STAKE_LOCKED") == 1


@pytest.mark.asyncio
async def test_insufficient_funds_and_every_breaker_refuse_before_money_moves(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    poor = await _seed(sessions, settings, cash="500.00")
    refused = await _refusal(sessions, settings, poor)
    assert (refused.code, refused.status) == ("INSUFFICIENT_FUNDS", 400)
    assert refused.message == "Stake of ₹1,000.00 exceeds available liquid cash of ₹500.00. Please deposit funds into Todar Mal's Vault."

    latched = await _seed(sessions, settings)
    async with sessions() as db:
        bankroll = await _bankroll(db, latched.user)
        bankroll.stop_loss_triggered = True
        await db.commit()
    assert (await _refusal(sessions, settings, latched)).code == "DAILY_STOP_LOSS_LATCHED"

    cooling = await _seed(sessions, settings)
    async with sessions() as db:
        (await _bankroll(db, cooling.user)).cooldown_until = NOW + timedelta(minutes=20)
        await db.commit()
    cool = await _refusal(sessions, settings, cooling)
    assert (cool.code, cool.status) == ("COOLDOWN_ACTIVE", 423)

    halted = await _seed(sessions, settings)
    kill = await _refusal(sessions, settings, halted, redis=_KillSwitchRedis(settings.CFO_KILL_SWITCH_KEY))
    assert (kill.code, kill.status) == ("KILL_SWITCH_ACTIVE", 423)

    elsewhere = await _seed(sessions, settings, book="pinnacle")
    book = await _refusal(sessions, settings, elsewhere)
    assert (book.code, book.status) == ("BOOK_NOT_SUPPORTED", 422) and "Pinnacle" in book.message

    broken = await _seed(sessions, settings, seal_holds=False)
    assert (await _refusal(sessions, settings, broken)).code == "SEAL_BROKEN"

    stranger = SimpleNamespace(user=poor.user, verdict=halted.verdict)  # someone else's verdict
    assert (await _refusal(sessions, settings, stranger)).status == 404

    decayed = await _seed(sessions, settings)
    low = await _refusal(sessions, settings, decayed, placed_odds=D("4.00"))  # under the 4.2292 floor
    assert (low.code, low.status) == ("ODDS_DECAYED", 400)
    forced = await _place(sessions, settings, decayed, placed_odds=D("4.00"), override_odds_decay=True)
    assert forced.ticket.odds_decay_flag is True and forced.ticket.placed_odds == D("4.0000")

    async with sessions() as db:  # every refusal rolled back: one ticket (the forced one), one lock, nothing else
        assert await _count(db, RealBetTicket) == 1 and await _count(db, UserPlacedBet) == 1 and await _count(db, TwinInPlayMonitor) == 1
        assert await _count(db, RealCashTransaction, RealCashTransaction.transaction_type == "BET_STAKE_LOCKED") == 1


# ================================================================ the cashout advisor
def test_the_advisor_calls_breach_cashout_and_hold_on_the_offer() -> None:
    def call(offer: str, fair: str, stake: str = "1000.00", pct: float = 0.35) -> lcs.Call:
        return lcs.advise(D(stake), D(offer), D(fair), floor=D(stake) * (1 - D(str(pct))), hold_ratio_min=0.85, target_profit_pct=0.80, book="Parimatch")

    breach = call("500.00", "600.00")  # floor ₹650
    assert breach.advice == "STOP_LOSS_BREACH" and breach.message.startswith("EMERGENCY: Offer ₹500.00 breached the stop-loss floor ₹650.00")
    assert "Cash out now on Parimatch" in breach.message
    advised = call("1500.00", "1600.00")
    assert advised.advice == "CASHOUT_ADVISED" and advised.hold_ratio == pytest.approx(0.9375) and "captures 93.8% of fair value ₹1,600.00" in advised.message
    hold = call("1100.00", "1600.00")
    assert hold.advice == "HOLD" and hold.message.startswith("Hold: Fair value ₹1,600.00 exceeds the offer ₹1,100.00 (68.8%)")
    target = call("1850.00", "2400.00")  # 77% of fair value, but 85% over the stake: the target is reached
    assert target.advice == "CASHOUT_ADVISED" and "85% over the ₹1,000.00 stake (target 80%)" in target.message
    assert call("650.00", "600.00").advice == "STOP_LOSS_BREACH"  # at the floor is a breach


@pytest.mark.asyncio
async def test_the_evaluation_names_its_probability_and_writes_nothing(sessions: async_sessionmaker[AsyncSession], settings: Settings,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    ids = await _seed(sessions, settings)
    ref = (await _place(sessions, settings, ids)).ticket.ticket_reference
    async with sessions() as db:
        entry = await lcs.evaluate(db, None, settings, ids.user, ref, D("1000"), NOW)
        # no live price, no tick yet: the probability at placement; fair = 0.24 x ₹1,000 x 4.7775
        assert (entry.probability_source, entry.fair_value, entry.floor, entry.stop_loss_pct) == ("ENTRY", D("1146.60"), D("646.40"), pytest.approx(0.3536))
        assert entry.call.advice == "CASHOUT_ADVISED" and entry.as_dict()["hold_ratio"] == pytest.approx(0.8721, abs=1e-4)
        assert (await lcs.evaluate(db, None, settings, ids.user, ref, D("600"), NOW)).call.advice == "STOP_LOSS_BREACH"
        assert (await lcs.evaluate(db, None, settings, ids.user, ref, D("900"), NOW)).call.advice == "HOLD"
        by_id = await lcs.evaluate(db, None, settings, ids.user, str(entry.ticket.id), D("900"), NOW)
        assert by_id.ticket.ticket_reference == ref

        monitor = (await db.execute(select(TwinInPlayMonitor))).scalar_one()
        monitor.last_tick_at, monitor.current_win_prob, monitor.fair_value_inr = NOW - timedelta(seconds=30), 0.42, D("2000.00")
        await db.commit()
        tick = await lcs.evaluate(db, None, settings, ids.user, ref, D("1500"), NOW)
        assert (tick.probability_source, tick.fair_value, tick.call.advice) == ("LAST_TICK", D("2000.00"), "HOLD")  # 75% of fair value

        live = SimpleNamespace(probability=0.5, advice=SimpleNamespace(fair_value=D("2388.75")))
        monkeypatch.setattr(inplay, "value_bet", lambda *a, **k: live)
        monkeypatch.setattr(inplay, "pullout", lambda *a, **k: None)
        now_live = await lcs.evaluate(db, None, settings, ids.user, ref, D("2100"), NOW)
        assert (now_live.probability_source, now_live.fair_value, now_live.win_probability, now_live.call.advice) == ("LIVE", D("2388.75"), 0.5, "CASHOUT_ADVISED")
        assert monitor.cashout_offer_inr is None  # a GET: nothing written
        with pytest.raises(rms.RealMoneyError) as missing:
            await lcs.evaluate(db, None, settings, ids.user, "PM-SLIP-NOPE", D("100"), NOW)
        assert missing.value.status == 404


# ================================================================ the cashout taken
@pytest.mark.asyncio
async def test_a_profitable_cashout_withholds_30pct_tds_on_the_net_winnings(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    ids = await _seed(sessions, settings)
    ref = (await _place(sessions, settings, ids)).ticket.ticket_reference
    async with sessions() as db:
        done = await lcs.execute_cashout(db, settings, ids.user, ref, D("2200"), "CASHOUT_ADVISED", NOW)
        await db.commit()
    t = done.ticket
    assert (t.status, t.gross_return, t.tds_deducted, t.settlement_payout, t.net_profit) == ("CASHED_OUT", D("2200.00"), D("360.00"), D("1840.00"), D("840.00"))
    assert done.net_winnings == D("1200.00") and done.monitor_closed is True
    assert done.as_dict() == {"ticket_reference": ref, "status": "CASHED_OUT", "gross_cashout": 2200.0, "placed_stake": 1000.0, "net_winnings": 1200.0,
                              "tds_deducted": 360.0, "net_payout": 1840.0, "net_profit": 840.0, "liquid_cash_balance": 25840.0, "locked_exposure": 0.0,
                              "monitor_closed": True, "developer_name": DEVELOPER}
    async with sessions() as db:
        bankroll = await _bankroll(db, ids.user)
        assert (bankroll.cash_balance, bankroll.locked_exposure, bankroll.total_tds_deducted, bankroll.gross_winnings, bankroll.realized_pnl) == (
            D("25840.00"), D("0.00"), D("360.00"), D("1200.00"), D("840.00"))
        tax = (await db.execute(select(TaxLedgerEntry).where(TaxLedgerEntry.user_id == ids.user))).scalar_one()
        assert (tax.mode, tax.section, tax.gross_payout, tax.stake, tax.net_winnings, tax.tds_amount, tax.net_post_tax_payout, tax.tds_rate) == (
            "LIVE", "115BBJ", D("2200.00"), D("1000.00"), D("1200.00"), D("360.00"), D("1840.00"), 0.30)
        rows = list((await db.execute(select(RealCashTransaction).where(RealCashTransaction.ticket_id == t.id, RealCashTransaction.transaction_type != "BET_STAKE_LOCKED")
                                      .order_by(RealCashTransaction.amount.desc()))).scalars())
        assert [(r.transaction_type, r.amount, r.balance_before, r.balance_after) for r in rows] == [
            ("BET_PAYOUT_CREDIT", D("2200.00"), D("24000.00"), D("26200.00")), ("TDS_WITHHOLDING_DEBIT", D("360.00"), D("26200.00"), D("25840.00"))]
        bet = await db.get(UserPlacedBet, t.user_placed_bet_id)
        assert (bet.status, bet.return_inr, bet.pnl_inr, bet.settlement_source) == ("CASHED_OUT", D("2200.00"), D("1200.00"), "CASHOUT")
        monitor = (await db.execute(select(TwinInPlayMonitor).where(TwinInPlayMonitor.bet_id == bet.id))).scalar_one()
        assert (monitor.is_active, monitor.pullout_triggered, monitor.pullout_reason) == (False, True, "CASHOUT_ADVISED")
        with pytest.raises(rms.RealMoneyError) as twice:
            await lcs.execute_cashout(db, settings, ids.user, ref, D("2200"), "CASHOUT_ADVISED", NOW)
        assert (twice.value.code, twice.value.status) == ("NOT_PLACED", 409)


@pytest.mark.asyncio
async def test_a_stop_loss_cashout_books_the_loss_without_tds(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    ids = await _seed(sessions, settings)
    ref = (await _place(sessions, settings, ids)).ticket.ticket_reference
    async with sessions() as db:
        with pytest.raises(rms.RealMoneyError) as unknown:
            await lcs.execute_cashout(db, settings, ids.user, ref, D("550"), "PANIC", NOW)
        assert (unknown.value.code, unknown.value.status) == ("INVALID_REASON", 422)
        done = await lcs.execute_cashout(db, settings, ids.user, ref, D("550"), "STOP_LOSS_BREACH", NOW)
        await db.commit()
    t = done.ticket
    assert (t.status, t.gross_return, t.tds_deducted, t.settlement_payout, t.net_profit, done.net_winnings) == (
        "CASHED_OUT", D("550.00"), D("0.00"), D("550.00"), D("-450.00"), D("0.00"))
    async with sessions() as db:
        bankroll = await _bankroll(db, ids.user)
        assert (bankroll.cash_balance, bankroll.locked_exposure, bankroll.realized_pnl, bankroll.daily_realized_loss, bankroll.consecutive_losses) == (
            D("24550.00"), D("0.00"), D("-450.00"), D("450.00"), 1)
        assert await _count(db, TaxLedgerEntry, TaxLedgerEntry.user_id == ids.user) == 0
        assert await _count(db, RealCashTransaction, RealCashTransaction.transaction_type == "TDS_WITHHOLDING_DEBIT") == 0
        monitor = (await db.execute(select(TwinInPlayMonitor))).scalar_one()
        assert monitor.pullout_reason == "STOP_LOSS" and monitor.is_active is False


# ================================================================ settlement and reconciliation
@pytest.mark.asyncio
@pytest.mark.parametrize(("away_goals", "status", "gross", "tds", "payout", "pnl", "cash"), [
    (1, "WON", "4777.50", "1133.25", "3644.25", "2644.25", "27644.25"),  # Liverpool 3-1: over 2.5; Arsenal 2-1: the double wins
    (0, "LOST", "0.00", "0.00", "0.00", "-1000.00", "24000.00"),  # Liverpool 2-0: under 2.5, the double loses
])
async def test_a_placed_slip_settles_as_its_oracle_bet_settles(sessions: async_sessionmaker[AsyncSession], settings: Settings, away_goals: int, status: str,
                                                                gross: str, tds: str, payout: str, pnl: str, cash: str) -> None:
    ids = await _seed(sessions, settings)
    ref = (await _place(sessions, settings, ids)).ticket.ticket_reference
    async with sessions() as db:
        for fixture, home, away, score in (("fx-ars-che", "Arsenal", "Chelsea", (2, 1)), ("fx-liv-eve", "Liverpool", "Everton", (2 + (away_goals > 0), away_goals))):
            await user_pnl_tracker.record_score(db, ScoreIn(fixture_id=fixture, home=home, away=away, sport_key="soccer_epl", kickoff=KICKOFF,
                                                            home_goals=score[0], away_goals=score[1]), source="test", by=None, now=NOW)
        await db.commit()
        # before the Oracle bet settles, the sweep leaves the ticket placed (it never grades a parlay as one market)
        assert (await settlement.settle_real_tickets(db, settings, NOW)).settled == 0
    await user_pnl_tracker.settle_pending(sessions, NOW)
    async with sessions() as db:
        report = await settlement.settle_real_tickets(db, settings, NOW)
        await db.commit()
    assert report.settled == 1 and report.rows[0]["source"] == "oracle_bet" and report.tds_withheld == D(tds)
    async with sessions() as db:
        t = (await db.execute(select(RealBetTicket).where(RealBetTicket.ticket_reference == ref))).scalar_one()
        assert (t.status, t.gross_return, t.tds_deducted, t.settlement_payout, t.net_profit) == (status, D(gross), D(tds), D(payout), D(pnl))
        bankroll = await _bankroll(db, ids.user)
        assert (bankroll.cash_balance, bankroll.locked_exposure) == (D(cash), D("0.00"))
        kinds = {r.transaction_type for r in (await db.execute(select(RealCashTransaction).where(RealCashTransaction.ticket_id == t.id))).scalars()}
        assert kinds == ({"BET_STAKE_LOCKED", "BET_PAYOUT_CREDIT", "TDS_WITHHOLDING_DEBIT"} if status == "WON" else {"BET_STAKE_LOCKED", "BET_STAKE_FORFEITED"})
        assert (await settlement.settle_real_tickets(db, settings, NOW)).settled == 0  # settled once


@pytest.mark.asyncio
async def test_reconciliation_corrects_exposure_and_only_reports_cash(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    ids = await _seed(sessions, settings)
    await _place(sessions, settings, ids)
    async with sessions() as db:
        clean = await vrs.reconcile(db, ids.user, NOW)
        assert (clean["reconciled"], clean["adjusted"], clean["open_tickets_count"], clean["locked_exposure"], clean["ledger_cash"], clean["cash_drift"]) == (
            True, False, 1, 1000.0, 24000.0, 0.0)
        bankroll = await _bankroll(db, ids.user)
        bankroll.locked_exposure = D("1500.00")  # drifted
        await db.commit()
        fixed = await vrs.reconcile(db, ids.user, NOW)
        await db.commit()
        assert (fixed["adjusted"], fixed["previous_exposure"], fixed["locked_exposure"], fixed["exposure_drift"], fixed["reconciled"]) == (
            True, 1500.0, 1000.0, 500.0, True)
        assert (await _bankroll(db, ids.user)).locked_exposure == D("1000.00")
        bankroll.cash_balance += D("10.00")  # cash that no ledger row explains
        await db.commit()
        off = await vrs.reconcile(db, ids.user, NOW)
        assert (off["reconciled"], off["cash_drift"], off["liquid_cash"], off["ledger_cash"]) == (False, 10.0, 24010.0, 24000.0)
        assert (await _bankroll(db, ids.user)).cash_balance == D("24010.00")  # reported, never written


# ================================================================ the API
@pytest.mark.asyncio
async def test_the_four_endpoints_end_to_end(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    ids = await _seed(sessions, settings)
    async with sessions() as db:
        user = await db.get(User, ids.user)

    async def db_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(real_money_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad = await client.post("/api/v1/real-money/confirm-handcrafted-placement", json={"report_id": str(ids.verdict), "booking_code": CODE, "placed_stake_inr": 99999})
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INSUFFICIENT_FUNDS"
        placed = await client.post("/api/v1/real-money/confirm-handcrafted-placement", json={"report_id": str(ids.verdict), "booking_code": CODE})
        assert placed.status_code == 200 and placed.headers["X-Engineered-For"] == DEVELOPER
        body = placed.json()
        ref = body["ticket"]["ticket_reference"]
        assert body["ticket"]["status"] == "PLACED" and body["ticket"]["verdict_id"] == str(ids.verdict) and body["stop_loss_floor_inr"] == 646.4
        assert body["monitor"]["is_active"] is True and body["replayed"] is False

        quote = await client.get(f"/api/v1/real-money/tickets/{ref}/cashout-evaluation", params={"offer_inr": 600})
        assert quote.status_code == 200 and quote.json()["advice"] == "STOP_LOSS_BREACH" and quote.json()["probability_source"] == "ENTRY"
        assert (await client.get(f"/api/v1/real-money/tickets/{ref}/cashout-evaluation", params={"offer_inr": 0})).status_code == 422

        drift = await client.post("/api/v1/real-money/reconcile")
        assert drift.status_code == 200 and drift.json()["reconciled"] is True and drift.json()["adjusted"] is False

        taken = await client.post("/api/v1/real-money/cashout", json={"ticket_reference": ref, "cashout_amount_inr": 2200, "reason": "CASHOUT_ADVISED"})
        assert taken.status_code == 200
        assert {k: taken.json()[k] for k in ("status", "tds_deducted", "net_payout", "net_profit", "liquid_cash_balance")} == {
            "status": "CASHED_OUT", "tds_deducted": 360.0, "net_payout": 1840.0, "net_profit": 840.0, "liquid_cash_balance": 25840.0}
        again = await client.post("/api/v1/real-money/cashout", json={"ticket_reference": ref, "cashout_amount_inr": 2200})
        assert again.status_code == 409
        history = await client.get("/api/v1/real-money/history")
        assert history.json()[0]["status"] == "CASHED_OUT" and history.json()[0]["user_placed_bet_id"] == body["user_placed_bet_id"]


# ================================================================ the migration and the desk on the page
def test_the_migration_adds_cashed_out_over_every_spelling_of_the_old_check() -> None:
    script = ScriptDirectory.from_config(Config(str(ROOT / "backend" / "alembic.ini")))
    revision = script.get_revision("f114a8b7c2d1")
    assert revision is not None and revision.down_revision == "e106b4d2c9a7" and script.get_heads() == ["f114a8b7c2d1"]
    source = (ROOT / "backend" / "alembic" / "versions" / "f114a8b7c2d1_add_cashed_out_status_to_real_bet_tickets.py").read_text(encoding="utf-8")
    assert "ck_real_bet_tickets_ck_real_bet_tickets_status_valid" in source and "'CASHED_OUT'" in source  # the live, double-prefixed name
    assert "uq_real_bet_tickets_verdict_id" in source and "fk_real_bet_tickets_user_placed_bet_id_user_placed_bets" in source


def test_the_bet_slip_carries_the_desk_and_the_realtime_bus_refreshes_it() -> None:
    src = ROOT / "frontend" / "src"
    desk = (src / "components" / "slip_ornament" / "HandcraftedPlacementDesk.tsx").read_text(encoding="utf-8")
    for text in ("LIVE IN-PLAY · PLACED ON BOOKMAKER", "Lock stake &amp; confirm placement", "Record cashout taken", "Stop-Loss Floor:", "STOP-LOSS BREACH",
                 "CASHOUT ADVISED", "Please deposit funds into Todar Mal's Vault"):
        assert text in desk, text
    assert "<HandcraftedPlacementDesk" in (src / "components" / "slip_ornament" / "LuxuryBetCardOrnament.tsx").read_text(encoding="utf-8")
    lib = (src / "lib" / "real_money.ts").read_text(encoding="utf-8")
    for text in ('| "CASHED_OUT"', "confirmHandcraftedPlacement", "evaluateCashoutOffer", "executeCashout", "reconcileRealVault"):
        assert text in lib, text
    realtime = (src / "services" / "realtime.ts").read_text(encoding="utf-8")
    assert '"real-money": REAL_MONEY_FANOUT' in realtime and 'event.type === "real_money"' in realtime
