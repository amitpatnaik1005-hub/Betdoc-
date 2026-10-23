"""The in-play cashout advisor and the realised cashout (Group 114, points b.6, a.7 and FA-6).
Developed for Amit Ashok Kumar Patnaik.

* ``evaluate``: the book's live cashout offer, as the user reads it, against the slip's fair hold value
  (``win probability x stake x odds``) and its stop-loss floor (``stake x (1 - stop_loss_pct)``):

  - ``STOP_LOSS_BREACH``: the offer is at or under the floor: cash out now to keep the rest of the stake.
  - ``CASHOUT_ADVISED``: the offer is at least ``ASHOKA_CASHOUT_HOLD_RATIO`` (85%) of fair value, or its profit reaches
    the watch's target (``REAL_CASHOUT_TARGET_PROFIT_PCT`` over the stake).
  - ``HOLD``: fair value is worth more than the offer: let the bet run.

  The win probability is the live one when the market prices every open leg (the twin's own valuation, which also pays
  the legs already won), else the twin watch's last priced tick, else the probability the slip was placed at; the
  answer names which. A GET: nothing is written.
* ``execute_cashout``: the cashout the user took at the book, recorded: Gross = the amount; Net Winnings = max(0,
  Gross - stake); 30% TDS on Net Winnings (Section 115BBJ, withheld under 194BA) with its ``TaxLedgerEntry``; the
  stake leaves locked exposure, the post-tax payout joins liquid cash (``realize_ticket``). The ticket is CASHED_OUT,
  its Oracle bet records the cashout, and that bet's watch closes. The caller commits, then announces.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.backtesting import inplay_stoploss_math as slm
from app.models.digital_twin import PulloutReason, TwinInPlayMonitor
from app.models.real_money import RealBankroll, RealBetTicket, RealTicketStatus
from app.models.user_bets_ledger import PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.services import ashoka_market, user_pnl_tracker
from app.services import real_money_service as rms
from app.services.twin import inplay

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
HOLD, CASHOUT_ADVISED, STOP_LOSS_BREACH = "HOLD", "CASHOUT_ADVISED", "STOP_LOSS_BREACH"
# what the user saw when they took the cashout -> the twin's pullout reason on the closed watch
REASONS: dict[str, PulloutReason] = {STOP_LOSS_BREACH: PulloutReason.STOP_LOSS, CASHOUT_ADVISED: PulloutReason.CASHOUT_ADVISED,
                                     HOLD: PulloutReason.MANUAL_USER_REQUEST, **{r.value: r for r in PulloutReason}}


@dataclass(frozen=True, slots=True)
class Call:
    advice: str
    message: str
    hold_ratio: float | None


def advise(stake: Decimal, offer: Decimal, fair_value: Decimal, *, floor: Decimal, hold_ratio_min: float, target_profit_pct: float, book: str) -> Call:
    """The three-way call on one offer (pure: the figures are the caller's)."""
    ratio = float(offer / fair_value) if fair_value > 0 else None
    if offer <= floor:
        return Call(STOP_LOSS_BREACH, f"EMERGENCY: Offer ₹{offer:,.2f} breached the stop-loss floor ₹{floor:,.2f}. Cash out now on {book} to preserve the "
                                      "remaining capital.", ratio)
    profit = offer - stake
    if ratio is not None and ratio >= hold_ratio_min:
        return Call(CASHOUT_ADVISED, f"Favourable: Offer ₹{offer:,.2f} captures {ratio:.1%} of fair value ₹{fair_value:,.2f}. Banking the profit is advised."
                    if profit > 0 else f"Offer ₹{offer:,.2f} captures {ratio:.1%} of fair value ₹{fair_value:,.2f}: the book pays close to what the bet is "
                    "worth. Cashing out is advised.", ratio)
    if stake > 0 and profit >= stake * Decimal(str(target_profit_pct)):
        return Call(CASHOUT_ADVISED, f"Favourable: Offer ₹{offer:,.2f} is {float(profit / stake):.0%} over the ₹{stake:,.2f} stake (target "
                                     f"{target_profit_pct:.0%}). Banking the profit is advised.", ratio)
    if ratio is None:
        return Call(HOLD, f"Hold: the bet is worth nothing at fair value, and the ₹{offer:,.2f} offer sits above the ₹{floor:,.2f} floor.", ratio)
    return Call(HOLD, f"Hold: Fair value ₹{fair_value:,.2f} exceeds the offer ₹{offer:,.2f} ({ratio:.1%}). The expectation favours letting the bet run.", ratio)


@dataclass(slots=True)
class Evaluation:
    ticket: RealBetTicket
    offer: Decimal
    call: Call
    fair_value: Decimal
    floor: Decimal
    stop_loss_pct: float
    target_profit_pct: float
    win_probability: float
    probability_source: str  # LIVE | LAST_TICK | ENTRY
    twin_call: dict[str, str] | None = None  # the watch's own pullout call on this offer (live prices only)

    def as_dict(self) -> dict[str, Any]:
        return {"ticket_reference": self.ticket.ticket_reference, "advice": self.call.advice, "message": self.call.message,
                "current_offer": float(self.offer), "fair_value": float(self.fair_value), "stop_loss_floor": float(self.floor),
                "stop_loss_pct": self.stop_loss_pct, "hold_ratio": None if self.call.hold_ratio is None else round(self.call.hold_ratio, 4),
                "target_profit_pct": self.target_profit_pct, "win_probability": round(self.win_probability, 4),
                "probability_source": self.probability_source, "placed_stake": float(self.ticket.placed_stake),
                "placed_odds": None if self.ticket.placed_odds is None else float(self.ticket.placed_odds), "twin_call": self.twin_call,
                "developer_name": DEVELOPER_CREDIT}


async def _ticket(db: AsyncSession, user_id: uuid.UUID, key: str) -> RealBetTicket:
    try:
        by_id = RealBetTicket.id == uuid.UUID(key)
    except ValueError:
        by_id = None
    match = RealBetTicket.ticket_reference == key if by_id is None else or_(RealBetTicket.ticket_reference == key, by_id)
    ticket = (await db.execute(select(RealBetTicket).where(match, RealBetTicket.user_id == user_id))).scalar_one_or_none()
    if ticket is None:
        raise rms.RealMoneyError("TICKET_NOT_FOUND", f"Ticket {key} does not exist", 404)
    if ticket.status != RealTicketStatus.PLACED.value:
        raise rms.RealMoneyError("NOT_PLACED", f"Ticket {ticket.ticket_reference} is {ticket.status}: only a placed ticket has a cashout", 409)
    return ticket


async def _linked(db: AsyncSession, ticket: RealBetTicket) -> tuple[UserPlacedBet | None, TwinInPlayMonitor | None]:
    if ticket.user_placed_bet_id is None:
        return None, None
    bet = await db.get(UserPlacedBet, ticket.user_placed_bet_id)
    monitor = None if bet is None else (await db.execute(select(TwinInPlayMonitor).where(TwinInPlayMonitor.bet_id == bet.id))).scalars().first()
    return bet, monitor


async def evaluate(db: AsyncSession, redis: Redis | None, settings: Settings, user_id: uuid.UUID, key: str, offer: Decimal, now: datetime) -> Evaluation:
    ticket = await _ticket(db, user_id, key)
    offer = rms._round_paisa(offer)  # noqa: SLF001
    bet, monitor = await _linked(db, ticket)
    stake, odds = ticket.placed_stake, ticket.placed_odds or ticket.target_odds
    stop = slm.StopLossPolicy.from_settings(settings).clamp(None if monitor is None else monitor.stop_loss_pct)
    target = monitor.target_profit_pct if monitor is not None else settings.REAL_CASHOUT_TARGET_PROFIT_PCT
    probability: float | None = None
    fair: Decimal | None = None
    source, twin_call = "ENTRY", None
    if bet is not None and bet.status == PlacedStatus.PENDING.value:
        legs = list((await db.execute(select(UserPlacedLeg).where(UserPlacedLeg.bet_id == bet.id).order_by(UserPlacedLeg.position))).scalars())
        live = await inplay.live_legs(redis, settings, {leg.fixture_id for leg in legs if leg.result == PlacedStatus.PENDING.value}, now)
        try:
            valuation = inplay.value_bet(bet, legs, live, settings, now, offer=offer)
        except (inplay.NoLivePrice, ValueError):
            valuation = None
        if valuation is not None:
            probability, fair, source = valuation.probability, valuation.advice.fair_value, "LIVE"
            if monitor is not None:
                call = inplay.pullout(monitor, bet, valuation, settings)
                twin_call = None if call is None else {"reason": call[0].value, "action": call[1]}
    elif bet is None and ticket.quote_market and ticket.quote_selection:  # a desk ticket: its one market, priced live
        live = await inplay.live_legs(redis, settings, {ticket.match_id}, now)
        p = ashoka_market.fair_value_inputs(live.get(f"{ticket.match_id}|{ticket.quote_market}|{ticket.quote_selection}"))
        if p is not None:
            probability, source = float(p), "LIVE"
    if probability is None and monitor is not None and monitor.last_tick_at is not None and monitor.fair_value_inr is not None:
        probability, fair, source = monitor.current_win_prob, monitor.fair_value_inr, "LAST_TICK"
    if probability is None:
        probability = ticket.true_probability
    if fair is None:
        fair = rms._round_paisa(stake * odds * Decimal(str(probability)))  # noqa: SLF001
    floor = slm.floor(stake, stop)
    call = advise(stake, offer, fair, floor=floor, hold_ratio_min=settings.ASHOKA_CASHOUT_HOLD_RATIO, target_profit_pct=target,
                  book=rms.BOOK_LABEL.get(ticket.bookmaker, ticket.bookmaker))
    return Evaluation(ticket, offer, call, fair, floor, stop, target, probability, source, twin_call)


@dataclass(slots=True)
class Cashout:
    ticket: RealBetTicket
    bankroll: RealBankroll
    net_winnings: Decimal
    monitor_closed: bool

    def as_dict(self) -> dict[str, Any]:
        t = self.ticket
        return {"ticket_reference": t.ticket_reference, "status": t.status, "gross_cashout": float(t.gross_return), "placed_stake": float(t.placed_stake),
                "net_winnings": float(self.net_winnings), "tds_deducted": float(t.tds_deducted), "net_payout": float(t.settlement_payout),
                "net_profit": float(t.net_profit), "liquid_cash_balance": float(self.bankroll.cash_balance),
                "locked_exposure": float(self.bankroll.locked_exposure), "monitor_closed": self.monitor_closed, "developer_name": DEVELOPER_CREDIT}


async def execute_cashout(db: AsyncSession, settings: Settings, user_id: uuid.UUID, ticket_reference: str, amount: Decimal, reason: str,
                          now: datetime) -> Cashout:
    pullout = REASONS.get(reason.strip().upper())
    if pullout is None:
        raise rms.RealMoneyError("INVALID_REASON", f"Unknown cashout reason {reason!r}: {', '.join(sorted(REASONS))}", 422)
    ticket = await _ticket(db, user_id, ticket_reference)
    bet, monitor = await _linked(db, ticket)
    if bet is not None and bet.status != PlacedStatus.PENDING.value:
        raise rms.RealMoneyError("BET_ALREADY_SETTLED", f"The slip's Oracle bet is already {bet.status}: the ticket settles from it on the next sweep.", 409)
    gross = rms._round_paisa(amount)  # noqa: SLF001
    book = rms.BOOK_LABEL.get(ticket.bookmaker, ticket.bookmaker)
    await rms.realize_ticket(db, settings, ticket, gross, status=RealTicketStatus.CASHED_OUT.value, now=now,
                             notes=f"Cashout of ₹{gross:,.2f} taken at {book} ({pullout.value.replace('_', ' ').lower()})")
    if bet is not None:
        await user_pnl_tracker.record_cashout(db, bet, gross, now)
    closed = False
    if monitor is not None and monitor.is_active:
        monitor.is_active, monitor.pullout_triggered, monitor.pullout_reason, monitor.pullout_at = False, True, pullout.value, now
        monitor.detail = {**(monitor.detail or {}), "closed": f"cashed out at ₹{gross:,.2f}", "at": now.isoformat()}
        closed = True
    await db.flush()
    bankroll = await rms.get_or_create_bankroll(db, user_id, now)
    return Cashout(ticket, bankroll, max(rms.ZERO, gross - ticket.placed_stake), closed)


__all__ = ["CASHOUT_ADVISED", "HOLD", "REASONS", "STOP_LOSS_BREACH", "Call", "Cashout", "Evaluation", "advise", "evaluate", "execute_cashout"]
