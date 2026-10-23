"""
Real-Money Betting Gateway Service (Group 95).
Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.

The assisted execution desk for real cash on Parimatch (INR, ``https://parimatchs1.com/en/all-live``) and 1xBet.
No bookmaker is automated: ASHVATTHAMA builds the ticket (deep link, one-click clipboard slip, target odds, the
minimum profitable floor, KUMBHA's Kelly stake), the trader places it by hand and confirms it here, and TODAR MAL
keeps the real cash vault (the Paper Trading Sandbox it once stood beside was retired in Group 97).

* **Sizing (KUMBHA)**: fractional Kelly ``f = (p b - q) / b x fraction`` on liquid cash, the fraction being the
  sport's multiplier clamped to 0.20-0.50, and never more than ``REAL_MAX_STAKE_PCT`` (5%) of liquid cash.
* **Odds floor (ASHVATTHAMA, SHIVAJI)**: ``floor = (1 / p) x (1 + REAL_MIN_EDGE)``, rounded up to the cent. A
  placement under it is refused (400) unless the trader explicitly overrides, and the ticket is flagged.
* **Circuit breakers (SHIVAJI)**: realized losses reaching ``REAL_DAILY_DRAWDOWN_PCT`` (15%) of the day's starting
  bankroll halt real betting until 00:00 IST; ``REAL_LOSS_STREAK_LIMIT`` (3) consecutive losses start a
  ``REAL_COOLDOWN_MINUTES`` (30) cool-down; CHANAKYA's kill switch halts it too.
* **Settlement (TODAR MAL)**: a win credits ``stake x odds`` less Section 115BBJ's 30% on the net winnings, booked
  in the cash ledger as the payout credit and the TDS debit, and in the tax ledger (mode LIVE); a loss forfeits the
  locked stake; a push or void refunds it in full; quarter-ball handicaps settle half won or half lost. A ticket
  with a settleable market settles from the recorded final score on the beat, any other by the trader's report.
* **Ledger**: every movement of liquid cash writes a ``RealCashTransaction`` with its exact balance before and after.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.wagering.legs import translate
from app.domain.wagering.tax import financial_year, section_115bbj
from app.domain.sports.registry import SportAdapterRegistry
from app.domain.sports.twin_bridge import sport_kelly_multiplier
from app.models.tax_and_fx import TaxLedgerEntry
from app.models.real_money import (
    OPEN_STATUSES,
    RealBankroll,
    RealBetTicket,
    RealBookmaker,
    RealCashTransaction,
    RealTicketStatus,
    RealTransactionType,
)
from app.services import ashvatthama
from app.services.twin.vetting import kill_switch_state

logger = logging.getLogger("betdoc.real_money")

IST = ZoneInfo("Asia/Kolkata")
PAISA = Decimal("0.01")
RUPEE = Decimal("1")
ZERO = Decimal("0.00")
ONE = Decimal(1)
TWO = Decimal(2)
MAX_AMOUNT = Decimal("1000000000")  # ₹100 crore: a typo guard, not a business limit
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"

SPORT_ALIASES: dict[str, str] = {
    "football": "soccer",
    "nfl": "american_football",
    "ncaaf": "american_football",
    "nhl": "ice_hockey",
    "hockey": "ice_hockey",
    "mma": "combat",
    "ufc": "combat",
    "boxing": "combat",
    "nba": "basketball",
    "mlb": "baseball",
    "ipl": "cricket",
}

BOOK_LABEL = {RealBookmaker.PARIMATCH.value: "Parimatch", RealBookmaker.ONEXBET.value: "1xBet"}
BOOK_PREFIX = {RealBookmaker.PARIMATCH.value: "PM", RealBookmaker.ONEXBET.value: "1X"}
BOOK_VENUE = {RealBookmaker.PARIMATCH.value: "parimatch", RealBookmaker.ONEXBET.value: "1xbet"}

LOSING = frozenset({RealTicketStatus.LOST.value, RealTicketStatus.HALF_LOST.value})
REFUNDING = frozenset({RealTicketStatus.PUSH.value, RealTicketStatus.VOID.value, RealTicketStatus.CANCELLED.value})
SETTLED_OUTCOMES = frozenset({
    RealTicketStatus.WON.value,
    RealTicketStatus.HALF_WON.value,
    RealTicketStatus.LOST.value,
    RealTicketStatus.HALF_LOST.value,
    RealTicketStatus.PUSH.value,
    RealTicketStatus.VOID.value,
    RealTicketStatus.CANCELLED.value,
})


class RealMoneyError(ValueError):
    """A refused real-money operation: ``code`` is machine-readable, ``status`` the HTTP status the API answers."""

    def __init__(self, code: str, message: str, status: int = 400, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


def _round_paisa(val: Decimal) -> Decimal:
    return val.quantize(PAISA, rounding=ROUND_HALF_UP)


def _round_odds(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _current_ist_day(now: datetime) -> date:
    return now.astimezone(IST).date()


def _next_midnight_ist(now: datetime) -> datetime:
    ist_now = now.astimezone(IST)
    next_day = ist_now.date() + timedelta(days=1)
    return datetime.combine(next_day, time(0, 0), tzinfo=IST).astimezone(UTC)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _canonical_sport(sport: str) -> str:
    s = sport.strip().lower()
    return SPORT_ALIASES.get(s, s)


@dataclass(frozen=True, slots=True)
class TicketRequest:
    sport: str
    league: str
    match_id: str
    event_name: str
    market_type: str
    selection: str
    target_odds: Decimal
    true_probability: float
    model_edge_pct: float | None = None
    vetting_pillar_score: float = 90.0
    bookmaker: str = "parimatch"
    line: float | None = None
    quote_market: str | None = None
    quote_selection: str | None = None
    notes: str | None = None


# ================================================================ BANKROLL LIFECYCLE
async def get_or_create_bankroll(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> RealBankroll:
    """Finds or initializes TODAR MAL's real bankroll for a user."""
    if now is None:
        now = datetime.now(UTC)
    stmt = select(RealBankroll).where(RealBankroll.user_id == user_id)
    res = await db.execute(stmt)
    bankroll = res.scalar_one_or_none()

    today = _current_ist_day(now)
    if bankroll is None:
        bankroll = RealBankroll(
            user_id=user_id,
            currency="INR",
            cash_balance=ZERO,
            locked_exposure=ZERO,
            total_deposited=ZERO,
            total_withdrawn=ZERO,
            realized_pnl=ZERO,
            gross_winnings=ZERO,
            total_tds_deducted=ZERO,
            high_water_mark=ZERO,
            daily_starting_balance=ZERO,
            daily_realized_loss=ZERO,
            consecutive_losses=0,
            stop_loss_triggered=False,
            trading_day=today,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(bankroll)
        await db.flush()
        return bankroll

    # Check for daily trading rollover (00:00 IST)
    if bankroll.trading_day != today:
        bankroll.trading_day = today
        bankroll.daily_starting_balance = bankroll.cash_balance + bankroll.locked_exposure
        bankroll.daily_realized_loss = ZERO
        bankroll.stop_loss_triggered = False
        bankroll.stop_loss_until = None
        bankroll.updated_at = now
        await db.flush()

    return bankroll


async def deposit(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    reference: str | None = "UPI/IMPS Deposit",
    notes: str | None = None,
) -> RealCashTransaction:
    now = datetime.now(UTC)
    if amount <= ZERO:
        raise RealMoneyError("INVALID_AMOUNT", "Deposit amount must be positive", 422)
    if amount > MAX_AMOUNT:
        raise RealMoneyError("AMOUNT_TOO_HIGH", f"Amount exceeds maximum ceiling of ₹{MAX_AMOUNT}", 422)

    bankroll = await get_or_create_bankroll(db, user_id, now)
    before = bankroll.cash_balance
    after = before + _round_paisa(amount)

    bankroll.cash_balance = after
    bankroll.total_deposited += _round_paisa(amount)
    total_cap = after + bankroll.locked_exposure
    if total_cap > bankroll.high_water_mark:
        bankroll.high_water_mark = total_cap
    if bankroll.daily_starting_balance == ZERO:
        bankroll.daily_starting_balance = total_cap
    bankroll.updated_at = now

    tx = RealCashTransaction(
        user_id=user_id,
        transaction_type=RealTransactionType.DEPOSIT.value,
        amount=_round_paisa(amount),
        currency="INR",
        balance_before=before,
        balance_after=after,
        reference_id=reference,
        description=f"Deposit credited via {reference or 'UPI/IMPS'}" + (f": {notes}" if notes else ""),
        created_at=now,
    )
    db.add(tx)
    await db.flush()
    return tx


async def withdraw(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: Decimal,
    notes: str | None = None,
) -> RealCashTransaction:
    now = datetime.now(UTC)
    if amount <= ZERO:
        raise RealMoneyError("INVALID_AMOUNT", "Withdrawal amount must be positive", 422)

    bankroll = await get_or_create_bankroll(db, user_id, now)
    amt = _round_paisa(amount)
    if amt > bankroll.cash_balance:
        raise RealMoneyError(
            "INSUFFICIENT_FUNDS",
            f"Cannot withdraw ₹{amt:,.2f}: liquid cash balance is ₹{bankroll.cash_balance:,.2f}",
            400,
            available=float(bankroll.cash_balance),
        )

    before = bankroll.cash_balance
    after = before - amt

    bankroll.cash_balance = after
    bankroll.total_withdrawn += amt
    bankroll.updated_at = now

    tx = RealCashTransaction(
        user_id=user_id,
        transaction_type=RealTransactionType.WITHDRAWAL.value,
        amount=amt,
        currency="INR",
        balance_before=before,
        balance_after=after,
        reference_id="WITHDRAWAL",
        description="Cash withdrawal to bank account" + (f": {notes}" if notes else ""),
        created_at=now,
    )
    db.add(tx)
    await db.flush()
    return tx


# ================================================================ SIZING & TICKET CREATION
def calculate_odds_floor(true_prob: float, min_edge: float) -> Decimal:
    """Calculates minimum profitable odds: (1 / p) * (1 + min_edge), rounded up."""
    if true_prob <= 0 or true_prob >= 1:
        raise RealMoneyError("INVALID_PROBABILITY", "Probability must be between 0 and 1", 422)
    raw = (Decimal(1) / Decimal(str(true_prob))) * (Decimal(1) + Decimal(str(min_edge)))
    return raw.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)


def format_clipboard_slip(
    ref: str,
    bookmaker: str,
    event: str,
    league: str,
    market: str,
    selection: str,
    odds: Decimal,
    floor: Decimal,
    stake: Decimal,
    edge_pct: float,
    notes: str | None = None,
) -> str:
    legs_section = ""
    if notes:
        try:
            parsed = json.loads(notes)
            if isinstance(parsed, dict) and "legs" in parsed and parsed["legs"]:
                legs_list = parsed["legs"]
                lines = [
                    "",
                    "PARLAY LEGS TO ADD ON PARIMATCH:",
                    "──────────────────────────────────────────────────",
                ]
                for i, leg in enumerate(legs_list, 1):
                    fix_name = leg.get("fixture") or leg.get("event") or "Match"
                    sel = leg.get("selection") or "Selection"
                    leg_odds = float(leg.get("odds", 0))
                    lines.append(f"  [{i}] {fix_name} — Selection: {sel} @ {leg_odds:.2f}")
                lines.extend([
                    "──────────────────────────────────────────────────",
                    "HOW TO PLACE ON PARIMATCH:",
                    "  1. Go to https://parimatchs1.com/en/all-live",
                    f"  2. Search & select each of the {len(legs_list)} matches above",
                    "  3. Switch betslip type to 'Parlay' / 'Express'",
                    f"  4. Enter Stake: ₹{stake:,.2f} (Total Odds: ≥ {odds:.2f})",
                    "  5. Click Place Bet & enter your Bookmaker Slip ID here!",
                ])
                legs_section = "\n" + "\n".join(lines)
        except Exception:
            pass

    return (
        f"🎯 BETDOC REAL-MONEY EXECUTION TICKET (ASHVATTHAMA)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Ticket Ref:       {ref}\n"
        f"Bookmaker:        {bookmaker.upper()} (INR)\n"
        f"Event:            {event} ({league})\n"
        f"Market:           {market} ({selection})\n"
        f"Recommended Stake: ₹{stake:,.2f} INR\n"
        f"Target Odds:      ≥ {odds:.2f} (Floor: {floor:.2f})\n"
        f"Quantitative Edge: +{edge_pct:.1f}%\n"
        f"Direct URL:       https://parimatchs1.com/en/all-live{legs_section}\n"
        f"Sniper:           ASHVATTHAMA | Vetted by KAUTILYA\n"
        f"Stop-Loss Shield: SHIVAJI ACTIVE\n"
        f"Engineered For:   {DEVELOPER_CREDIT}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def create_execution_ticket(
    db: AsyncSession,
    redis: Redis | None,
    settings: Settings,
    user_id: uuid.UUID,
    req: TicketRequest,
    now: datetime,
) -> RealBetTicket:
    # 1. Check Chanakya kill switch
    is_halted = await kill_switch_state(db, redis, settings)
    if is_halted:
        raise RealMoneyError("KILL_SWITCH_ACTIVE", "Trading is halted by the master kill switch.", 423)

    # 1b. Group 86: a book under an emergency safety lock takes no ticket
    from app.services.emergency_kill_switch_service import bookmaker_locked  # noqa: PLC0415 - the fortress imports the control panel

    if await bookmaker_locked(redis, settings, req.bookmaker):
        raise RealMoneyError("BOOKMAKER_LOCKED", f"{req.bookmaker} is under an emergency safety lock (Control Panel).", 423)

    # 2. Get bankroll & verify Shivaji circuit breakers
    bankroll = await get_or_create_bankroll(db, user_id, now)

    if bankroll.stop_loss_triggered:
        raise RealMoneyError(
            "DAILY_STOP_LOSS_LATCHED",
            "15% daily drawdown reached. Shivaji has locked live execution until 00:00 IST.",
            423,
            stop_loss_until=str(_next_midnight_ist(now)),
        )

    cooldown = _as_utc(bankroll.cooldown_until)
    if cooldown and cooldown > now:
        rem = int((cooldown - now).total_seconds())
        raise RealMoneyError(
            "COOLDOWN_ACTIVE",
            f"Cool-down active after 3 consecutive losses. Resumes in {rem} seconds.",
            423,
            seconds_remaining=rem,
        )

    # 3. Calculate True Edge and Minimum Odds Floor
    p = Decimal(str(req.true_probability))
    odds_floor = calculate_odds_floor(req.true_probability, settings.REAL_MIN_EDGE)

    b = req.target_odds - ONE
    if b <= ZERO:
        raise RealMoneyError("INVALID_ODDS", "Target odds must exceed 1.00", 422)

    q = ONE - p
    kelly_full = (p * b - q) / b
    if kelly_full <= ZERO:
        raise RealMoneyError("NO_POSITIVE_EDGE", "Selection carries negative or zero expected value.", 422)

    # 4. Sizing via KUMBHA: fractional Kelly clamped to 0.20 - 0.50
    canonical_sport = _canonical_sport(req.sport)
    sport_mult = Decimal(str(sport_kelly_multiplier(canonical_sport)))
    fraction = max(Decimal("0.20"), min(Decimal("0.50"), sport_mult))

    stake_kelly = bankroll.cash_balance * kelly_full * fraction
    # Ceiling at REAL_MAX_STAKE_PCT (5% of liquid cash)
    max_allowed = _round_paisa(bankroll.cash_balance * Decimal(str(settings.REAL_MAX_STAKE_PCT)))
    recommended = max(Decimal("10.00"), min(max_allowed, _round_paisa(stake_kelly)))

    if recommended > bankroll.cash_balance:
        recommended = bankroll.cash_balance

    edge_pct = req.model_edge_pct if req.model_edge_pct is not None else float((p * req.target_odds - ONE) * 100)

    # 5. Format Ticket Reference & Deep Link
    book_code = BOOK_PREFIX.get(req.bookmaker.lower(), "PM")
    day_str = now.strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:6].upper()
    ticket_ref = f"{book_code}-REAL-{day_str}-{short_uuid}"

    venue_base = ashvatthama.venue_url(settings, req.bookmaker.lower()) or "https://parimatchs1.com/en/all-live"
    deep_link = f"{venue_base}?event={req.match_id}&market={req.market_type}"

    clipboard = format_clipboard_slip(
        ref=ticket_ref,
        bookmaker=BOOK_LABEL.get(req.bookmaker.lower(), "Parimatch"),
        event=req.event_name,
        league=req.league,
        market=req.market_type,
        selection=req.selection,
        odds=req.target_odds,
        floor=odds_floor,
        stake=recommended,
        edge_pct=edge_pct,
        notes=req.notes,
    )

    ticket = RealBetTicket(
        user_id=user_id,
        ticket_reference=ticket_ref,
        bookmaker=req.bookmaker.lower(),
        sport=canonical_sport,
        league=req.league,
        match_id=req.match_id,
        event_name=req.event_name,
        market_type=req.market_type,
        selection=req.selection,
        recommended_stake=recommended,
        placed_stake=ZERO,
        currency="INR",
        target_odds=_round_odds(req.target_odds),
        placed_odds=None,
        minimum_profitable_odds=odds_floor,
        true_probability=req.true_probability,
        model_edge_pct=round(edge_pct, 2),
        kelly_fraction=float(fraction),
        deep_link_url=deep_link,
        clipboard_payload=clipboard,
        bookmaker_slip_id=None,
        quote_market=req.quote_market or req.market_type,
        quote_selection=req.quote_selection or req.selection,
        settleable_from_score=True,
        status=RealTicketStatus.PENDING_PLACEMENT.value,
        gross_return=ZERO,
        settlement_payout=ZERO,
        net_profit=ZERO,
        tds_deducted=ZERO,
        vetting_pillar_score=req.vetting_pillar_score,
        odds_decay_flag=False,
        notes=req.notes,
        created_at=now,
    )
    db.add(ticket)
    await db.flush()
    return ticket


# ================================================================ CONFIRM PLACEMENT
async def confirm_placement(
    db: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    ticket_ref: str,
    placed_odds: Decimal,
    placed_stake: Decimal,
    slip_id: str | None = None,
    override_odds_decay: bool = False,
) -> RealBetTicket:
    now = datetime.now(UTC)
    stmt = select(RealBetTicket).where(RealBetTicket.ticket_reference == ticket_ref, RealBetTicket.user_id == user_id)
    res = await db.execute(stmt)
    ticket = res.scalar_one_or_none()

    if ticket is None:
        raise RealMoneyError("TICKET_NOT_FOUND", f"Ticket {ticket_ref} does not exist", 404)
    if ticket.status != RealTicketStatus.PENDING_PLACEMENT.value:
        raise RealMoneyError("INVALID_STATE", f"Ticket is already in status {ticket.status}", 409)

    bankroll = await get_or_create_bankroll(db, user_id, now)

    # Sizing check against liquid cash
    stake = _round_paisa(placed_stake)
    if stake <= ZERO:
        raise RealMoneyError("INVALID_STAKE", "Placed stake must be positive", 422)
    if stake > bankroll.cash_balance:
        raise RealMoneyError(
            "INSUFFICIENT_FUNDS",
            f"Stake of ₹{stake:,.2f} exceeds available liquid cash of ₹{bankroll.cash_balance:,.2f}",
            400,
            available=float(bankroll.cash_balance),
        )

    # 5% stake ceiling validation
    max_stake = _round_paisa(bankroll.cash_balance * Decimal(str(settings.REAL_MAX_STAKE_PCT)))
    if stake > max_stake and not override_odds_decay:
        raise RealMoneyError(
            "STAKE_EXCEEDS_CEILING",
            f"Stake ₹{stake:,.2f} exceeds the 5% max ceiling of ₹{max_stake:,.2f}",
            422,
            max_allowed=float(max_stake),
        )

    # Odds decay validation
    odds = _round_odds(placed_odds)
    decayed = odds < ticket.minimum_profitable_odds
    if decayed and not override_odds_decay:
        raise RealMoneyError(
            "ODDS_DECAYED",
            f"Placed odds ({odds:.2f}) dropped below minimum floor ({ticket.minimum_profitable_odds:.2f}). "
            "Edge is negative. Pass override_odds_decay=True to force placement.",
            400,
            placed_odds=float(odds),
            floor=float(ticket.minimum_profitable_odds),
        )

    # Lock stake: Liquid Cash -> Locked Exposure
    before = bankroll.cash_balance
    after = before - stake
    bankroll.cash_balance = after
    bankroll.locked_exposure += stake
    bankroll.updated_at = now

    ticket.placed_stake = stake
    ticket.placed_odds = odds
    ticket.bookmaker_slip_id = slip_id
    ticket.odds_decay_flag = decayed
    ticket.status = RealTicketStatus.PLACED.value
    ticket.placed_at = now

    tx = RealCashTransaction(
        user_id=user_id,
        ticket_id=ticket.id,
        transaction_type=RealTransactionType.BET_STAKE_LOCKED.value,
        amount=stake,
        currency="INR",
        balance_before=before,
        balance_after=after,
        reference_id=ticket_ref,
        description=f"Locked stake for {ticket.bookmaker.upper()} ticket {ticket_ref} on {ticket.event_name}",
        created_at=now,
    )
    db.add(tx)
    await db.flush()
    return ticket


# ================================================================ SETTLEMENT & 30% TDS
async def settle_ticket(
    db: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    ticket_ref: str,
    outcome: str | None = None,
    scorecard: dict[str, Any] | None = None,
    notes: str | None = None,
) -> RealBetTicket:
    now = datetime.now(UTC)
    stmt = select(RealBetTicket).where(RealBetTicket.ticket_reference == ticket_ref, RealBetTicket.user_id == user_id)
    res = await db.execute(stmt)
    ticket = res.scalar_one_or_none()

    if ticket is None:
        raise RealMoneyError("TICKET_NOT_FOUND", f"Ticket {ticket_ref} not found", 404)
    if ticket.status != RealTicketStatus.PLACED.value:
        raise RealMoneyError("NOT_PLACED", f"Cannot settle ticket in status {ticket.status}", 409)

    # Determine outcome either from argument or score resolution
    final_outcome: str = outcome.upper() if outcome else ""
    if not final_outcome and scorecard:
        final_outcome = _resolve_scorecard_outcome(ticket, scorecard)
    if not final_outcome:
        raise RealMoneyError("OUTCOME_REQUIRED", "A valid outcome or scorecard is required to settle", 422)

    if final_outcome not in SETTLED_OUTCOMES:
        raise RealMoneyError("INVALID_OUTCOME", f"Outcome {final_outcome} is not recognized", 422)

    bankroll = await get_or_create_bankroll(db, user_id, now)
    stake = ticket.placed_stake
    odds = ticket.placed_odds or ticket.target_odds
    before = bankroll.cash_balance

    gross_return = ZERO
    net_profit = ZERO
    tds = ZERO
    payout = ZERO

    # 1. Win Scenario
    if final_outcome == RealTicketStatus.WON.value:
        gross_return = _round_paisa(stake * odds)
        tds_calc = section_115bbj(gross_return, stake)
        tds = tds_calc.tds
        payout = gross_return - tds
        net_profit = payout - stake

        bankroll.locked_exposure -= stake
        bankroll.cash_balance += payout
        bankroll.gross_winnings += (gross_return - stake)
        bankroll.total_tds_deducted += tds
        bankroll.realized_pnl += net_profit
        bankroll.consecutive_losses = 0

        # Ledger entries
        db.add(RealCashTransaction(
            user_id=user_id,
            ticket_id=ticket.id,
            transaction_type=RealTransactionType.BET_PAYOUT_CREDIT.value,
            amount=gross_return,
            currency="INR",
            balance_before=before,
            balance_after=before + gross_return,
            reference_id=ticket_ref,
            description=f"Gross winning payout on {ticket_ref} (odds {odds})",
            created_at=now,
        ))
        if tds > ZERO:
            db.add(RealCashTransaction(
                user_id=user_id,
                ticket_id=ticket.id,
                transaction_type=RealTransactionType.TDS_WITHHOLDING_DEBIT.value,
                amount=tds,
                currency="INR",
                balance_before=before + gross_return,
                balance_after=bankroll.cash_balance,
                reference_id=ticket_ref,
                description=f"Section 115BBJ 30% TDS withheld on {ticket_ref}",
                created_at=now,
            ))
            # Tax ledger
            db.add(TaxLedgerEntry(
                user_id=user_id,
                paper_bet_id=None,
                mode="LIVE",
                gross_payout=gross_return,
                stake=stake,
                net_winnings=gross_return - stake,
                tds_rate=0.30,
                tds_amount=tds,
                net_post_tax_payout=payout,
                currency="INR",
                jurisdiction="IN",
                section="115BBJ",
                financial_year=financial_year(now),
                created_at=now,
            ))

    # 2. Half Won Scenario: half the stake wins at the odds, the other half comes back. Group 113: the winning half is a
    # winning bet like any other, so its TDS is withheld, debited and ledgered exactly as a full win's.
    elif final_outcome == RealTicketStatus.HALF_WON.value:
        half_stake = _round_paisa(stake / TWO)
        winning_half = _round_paisa(half_stake * odds)
        gross_return = winning_half + (stake - half_stake)
        tds_calc = section_115bbj(winning_half, half_stake)
        tds = tds_calc.tds
        payout = gross_return - tds
        net_profit = payout - stake

        bankroll.locked_exposure -= stake
        bankroll.cash_balance += payout
        bankroll.gross_winnings += tds_calc.net_winnings
        bankroll.total_tds_deducted += tds
        bankroll.realized_pnl += net_profit
        bankroll.consecutive_losses = 0

        db.add(RealCashTransaction(
            user_id=user_id,
            ticket_id=ticket.id,
            transaction_type=RealTransactionType.BET_PAYOUT_CREDIT.value,
            amount=gross_return,
            currency="INR",
            balance_before=before,
            balance_after=before + gross_return,
            reference_id=ticket_ref,
            description=f"Half-won payout on {ticket_ref}: the winning half at odds {odds} and the other half returned",
            created_at=now,
        ))
        if tds > ZERO:
            db.add(RealCashTransaction(
                user_id=user_id,
                ticket_id=ticket.id,
                transaction_type=RealTransactionType.TDS_WITHHOLDING_DEBIT.value,
                amount=tds,
                currency="INR",
                balance_before=before + gross_return,
                balance_after=bankroll.cash_balance,
                reference_id=ticket_ref,
                description=f"Section 115BBJ 30% TDS withheld on the winning half of {ticket_ref}",
                created_at=now,
            ))
            db.add(TaxLedgerEntry(
                user_id=user_id,
                paper_bet_id=None,
                mode="LIVE",
                gross_payout=winning_half,
                stake=half_stake,
                net_winnings=tds_calc.net_winnings,
                tds_rate=float(tds_calc.tds_rate),
                tds_amount=tds,
                net_post_tax_payout=tds_calc.net_post_tax_payout,
                currency="INR",
                jurisdiction="IN",
                section="115BBJ",
                financial_year=financial_year(now),
                created_at=now,
            ))

    # 3. Loss Scenario
    elif final_outcome == RealTicketStatus.LOST.value:
        bankroll.locked_exposure -= stake
        bankroll.realized_pnl -= stake
        bankroll.daily_realized_loss += stake
        bankroll.consecutive_losses += 1
        net_profit = -stake

        db.add(RealCashTransaction(
            user_id=user_id,
            ticket_id=ticket.id,
            transaction_type=RealTransactionType.BET_STAKE_FORFEITED.value,
            amount=stake,
            currency="INR",
            balance_before=before,
            balance_after=before,
            reference_id=ticket_ref,
            description=f"Stake forfeited upon loss on {ticket_ref}",
            created_at=now,
        ))

        # Check Shivaji Stop-Loss: 15% daily drawdown
        start_bal = bankroll.daily_starting_balance
        if start_bal > ZERO:
            loss_pct = float(bankroll.daily_realized_loss / start_bal)
            if loss_pct >= settings.REAL_DAILY_DRAWDOWN_PCT:
                bankroll.stop_loss_triggered = True
                bankroll.stop_loss_until = _next_midnight_ist(now)
                logger.warning(
                    f"SHIVAJI STOP-LOSS TRIPPED for user {user_id}: daily loss ₹{bankroll.daily_realized_loss:,.2f} "
                    f"reached {loss_pct*100:.1f}% >= 15%."
                )

        # Check consecutive loss cool-down: 3 losses -> 30m cool-down
        if bankroll.consecutive_losses >= settings.REAL_LOSS_STREAK_LIMIT:
            bankroll.cooldown_until = now + timedelta(minutes=settings.REAL_COOLDOWN_MINUTES)
            logger.warning(
                f"SHIVAJI COOL-DOWN ACTIVATED for user {user_id}: {bankroll.consecutive_losses} consecutive losses. "
                f"Cool-down until {bankroll.cooldown_until}."
            )

    # 4. Half Lost Scenario
    elif final_outcome == RealTicketStatus.HALF_LOST.value:
        half_stake = _round_paisa(stake / TWO)
        payout = stake - half_stake
        net_profit = -half_stake

        bankroll.locked_exposure -= stake
        bankroll.cash_balance += payout
        bankroll.realized_pnl -= half_stake
        bankroll.daily_realized_loss += half_stake
        bankroll.consecutive_losses += 1

        db.add(RealCashTransaction(
            user_id=user_id,
            ticket_id=ticket.id,
            transaction_type=RealTransactionType.BET_REFUND.value,
            amount=payout,
            currency="INR",
            balance_before=before,
            balance_after=bankroll.cash_balance,
            reference_id=ticket_ref,
            description=f"Half-lost refund on Asian handicap ticket {ticket_ref}",
            created_at=now,
        ))

    # 5. Push / Void / Cancelled Scenario
    elif final_outcome in REFUNDING:
        payout = stake
        net_profit = ZERO
        bankroll.locked_exposure -= stake
        bankroll.cash_balance += payout

        db.add(RealCashTransaction(
            user_id=user_id,
            ticket_id=ticket.id,
            transaction_type=RealTransactionType.BET_REFUND.value,
            amount=payout,
            currency="INR",
            balance_before=before,
            balance_after=bankroll.cash_balance,
            reference_id=ticket_ref,
            description=f"Stake refunded in full ({final_outcome}) on {ticket_ref}",
            created_at=now,
        ))

    # High-water mark update
    tot = bankroll.cash_balance + bankroll.locked_exposure
    if tot > bankroll.high_water_mark:
        bankroll.high_water_mark = tot
    bankroll.updated_at = now

    ticket.status = final_outcome
    ticket.gross_return = gross_return
    ticket.settlement_payout = payout
    ticket.net_profit = net_profit
    ticket.tds_deducted = tds
    ticket.settled_at = now
    if notes:
        ticket.notes = (ticket.notes + " | " + notes) if ticket.notes else notes

    await db.flush()
    return ticket


def _resolve_scorecard_outcome(ticket: RealBetTicket, scorecard: dict[str, Any]) -> str:
    """Uses sport adapters to resolve match final score to outcome. Group 113: a scorecard the adapter cannot grade is
    refused, never settled VOID (a refund nobody decided is a fake settlement)."""
    try:
        adapter = SportAdapterRegistry.get_adapter(ticket.sport)
        translated = translate(ticket.sport, ticket.market_type, ticket.selection)
        res = adapter.settle_market(translated.market_type, scorecard, translated.selection, translated.line)
        return res.value.upper()
    except Exception as exc:
        logger.warning(f"Could not grade ticket {ticket.ticket_reference} from its scorecard: {exc}")
        raise RealMoneyError("UNGRADEABLE", f"The scorecard cannot grade {ticket.market_type} / {ticket.selection}: give the outcome instead", 422) from exc


# ================================================================ QUERIES & VIEWS
async def get_active_tickets(db: AsyncSession, user_id: uuid.UUID) -> list[RealBetTicket]:
    stmt = (
        select(RealBetTicket)
        .where(RealBetTicket.user_id == user_id, RealBetTicket.status.in_(list(OPEN_STATUSES)))
        .order_by(RealBetTicket.created_at.desc())
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_ticket_history(db: AsyncSession, user_id: uuid.UUID, limit: int = 50) -> list[RealBetTicket]:
    stmt = (
        select(RealBetTicket)
        .where(RealBetTicket.user_id == user_id)
        .order_by(RealBetTicket.created_at.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_cash_transactions(db: AsyncSession, user_id: uuid.UUID, limit: int = 50) -> list[RealCashTransaction]:
    stmt = (
        select(RealCashTransaction)
        .where(RealCashTransaction.user_id == user_id)
        .order_by(RealCashTransaction.created_at.desc())
        .limit(limit)
    )
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def check_ticket_odds(
    redis: Redis | None,
    settings: Settings,
    ticket: RealBetTicket,
) -> dict[str, Any]:
    """Inspects live book store in Redis to detect line drift / decay."""
    current_odds: float | None = None
    is_suspended = False
    as_of: str | None = None

    if redis is not None and ticket.quote_market and ticket.quote_selection:
        try:
            from app.services.aryabhata_pipeline import read_market_books
            venue = BOOK_VENUE.get(ticket.bookmaker, "parimatch")
            books = await read_market_books(redis, ticket.match_id, ticket.quote_market)
            if books and venue in books:
                quote = books[venue]
                is_suspended = bool(quote.get("suspended", False))
                prices = quote.get("prices", {})
                if ticket.quote_selection in prices:
                    current_odds = float(prices[ticket.quote_selection])
                as_of = str(quote.get("as_of", ""))
        except Exception as exc:
            logger.warning(f"Error checking live odds for ticket {ticket.ticket_reference}: {exc}")

    odds_floor = float(ticket.minimum_profitable_odds)
    decay_detected = current_odds is not None and current_odds < odds_floor

    return {
        "ticket_reference": ticket.ticket_reference,
        "bookmaker": ticket.bookmaker,
        "current_odds": current_odds,
        "target_odds": float(ticket.target_odds),
        "minimum_profitable_odds": odds_floor,
        "odds_decay_detected": decay_detected,
        "is_suspended": is_suspended,
        "as_of": as_of,
        "developer_name": DEVELOPER_CREDIT,
    }


def bankroll_view(bankroll: RealBankroll, settings: Settings, now: datetime) -> dict[str, Any]:
    start_bal = float(bankroll.daily_starting_balance)
    day_loss = float(bankroll.daily_realized_loss)
    drawdown_pct = round((day_loss / start_bal * 100) if start_bal > 0 else 0.0, 2)
    max_stake = round(float(bankroll.cash_balance) * settings.REAL_MAX_STAKE_PCT, 2)

    cooldown = _as_utc(bankroll.cooldown_until)
    in_cooldown = bool(cooldown and cooldown > now)
    rem = int((cooldown - now).total_seconds()) if in_cooldown and cooldown else 0
    can_bet = not bankroll.stop_loss_triggered and not in_cooldown and bankroll.cash_balance > 0

    return {
        "user_id": str(bankroll.user_id),
        "currency": "INR",
        "cash_balance": float(bankroll.cash_balance),
        "locked_exposure": float(bankroll.locked_exposure),
        "total_bankroll": float(bankroll.cash_balance + bankroll.locked_exposure),
        "total_deposited": float(bankroll.total_deposited),
        "total_withdrawn": float(bankroll.total_withdrawn),
        "realized_pnl": float(bankroll.realized_pnl),
        "gross_winnings": float(bankroll.gross_winnings),
        "total_tds_deducted": float(bankroll.total_tds_deducted),
        "high_water_mark": float(bankroll.high_water_mark),
        "daily_starting_balance": start_bal,
        "daily_realized_loss": day_loss,
        "daily_drawdown_pct": drawdown_pct,
        "daily_drawdown_limit_pct": settings.REAL_DAILY_DRAWDOWN_PCT * 100,
        "consecutive_losses": bankroll.consecutive_losses,
        "loss_streak_limit": settings.REAL_LOSS_STREAK_LIMIT,
        "stop_loss_triggered": bankroll.stop_loss_triggered,
        "stop_loss_until": str(bankroll.stop_loss_until) if bankroll.stop_loss_until else None,
        "is_in_cooldown": in_cooldown,
        "cooldown_seconds_remaining": rem,
        "max_single_stake": max_stake,
        "max_stake_pct": settings.REAL_MAX_STAKE_PCT * 100,
        "trading_day": str(bankroll.trading_day) if bankroll.trading_day else None,
        "can_bet": can_bet,
        "developer_name": DEVELOPER_CREDIT,
    }


def ticket_view(ticket: RealBetTicket) -> dict[str, Any]:
    return {
        "id": str(ticket.id),
        "ticket_reference": ticket.ticket_reference,
        "bookmaker": ticket.bookmaker,
        "sport": ticket.sport,
        "league": ticket.league,
        "match_id": ticket.match_id,
        "event_name": ticket.event_name,
        "market_type": ticket.market_type,
        "selection": ticket.selection,
        "recommended_stake": float(ticket.recommended_stake),
        "placed_stake": float(ticket.placed_stake),
        "currency": "INR",
        "target_odds": float(ticket.target_odds),
        "placed_odds": float(ticket.placed_odds) if ticket.placed_odds else None,
        "minimum_profitable_odds": float(ticket.minimum_profitable_odds),
        "true_probability": ticket.true_probability,
        "model_edge_pct": ticket.model_edge_pct,
        "kelly_fraction": ticket.kelly_fraction,
        "deep_link_url": ticket.deep_link_url,
        "clipboard_payload": ticket.clipboard_payload,
        "bookmaker_slip_id": ticket.bookmaker_slip_id,
        "quote_market": ticket.quote_market,
        "quote_selection": ticket.quote_selection,
        "settleable_from_score": ticket.settleable_from_score,
        "status": ticket.status,
        "gross_return": float(ticket.gross_return),
        "settlement_payout": float(ticket.settlement_payout),
        "net_profit": float(ticket.net_profit),
        "tds_deducted": float(ticket.tds_deducted),
        "vetting_pillar_score": ticket.vetting_pillar_score,
        "odds_decay_flag": ticket.odds_decay_flag,
        "notes": ticket.notes,
        "placed_at": ticket.placed_at.isoformat() if ticket.placed_at else None,
        "settled_at": ticket.settled_at.isoformat() if ticket.settled_at else None,
        "created_at": ticket.created_at.isoformat(),
        "developer_name": DEVELOPER_CREDIT,
    }


def transaction_view(tx: RealCashTransaction) -> dict[str, Any]:
    return {
        "id": str(tx.id),
        "ticket_id": str(tx.ticket_id) if tx.ticket_id else None,
        "transaction_type": tx.transaction_type,
        "amount": float(tx.amount),
        "currency": "INR",
        "balance_before": float(tx.balance_before),
        "balance_after": float(tx.balance_after),
        "reference_id": tx.reference_id,
        "description": tx.description,
        "created_at": tx.created_at.isoformat(),
        "developer_name": DEVELOPER_CREDIT,
    }
