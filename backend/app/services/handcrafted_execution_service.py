"""Handcrafted placement of a Bet-Slip ticket with real capital (Group 114, points a.4 and b.7).
Developed for Amit Ashok Kumar Patnaik.

BetDoc is Krishna, the strategist and the ledger; the user is Arjuna, who alone places the wager at Parimatch or 1xBet.
Nothing here touches a bookmaker: the user places the official ticket by hand, copies the book's booking code back,
and ``confirm_slip_placement`` closes the loop in one transaction:

1. The verdict is the user's own, its HMAC-SHA256 seal still holds, it is an APPROVED_GOLD verdict with a stake
   (``slip_ornament.issued``). A slip is placed once (``uq_real_bet_tickets_verdict_id``): the same booking code again
   answers the placed ticket, nothing moves twice; another code is refused.
2. The real-money desk keeps Parimatch and 1xBet accounts only; every leg must be one the Oracle can settle.
3. CHANCELLOR's gate (``execution_gate``): the kill switch, the book's emergency lock, SHIVAJI's 15% daily stop-loss
   latch and the cool-down after three straight losses.
4. ``confirm_placement`` locks the stake (liquid cash -> locked exposure, a ``BET_STAKE_LOCKED`` cash row): 400
   INSUFFICIENT_FUNDS over the liquid cash, the 5% ceiling, the odds floor under the joint probability.
5. The Oracle bet (``UserPlacedBet``, the booking code on it) tracks the legs: the ticket settles as that bet settles,
   so a parlay settles leg by leg from the final scores (``live_result_settlement_service``).
6. The booking code goes on the Bet-Slip record, and the twin's in-play watch starts on a straight slip with the
   verdict's stop-loss and ``REAL_CASHOUT_TARGET_PROFIT_PCT``.

The caller commits, then calls ``real_money_service.announce``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS
from app.domain.slip_ornament import ticket as tk
from app.models.consultant import ConsultantVerdictModel
from app.models.digital_twin import TwinInPlayMonitor
from app.models.real_money import RealBankroll, RealBetTicket, RealBookmaker, RealTicketStatus
from app.models.user_bets_ledger import PlacedBookmaker, PlacedStructure, UserPlacedBet
from app.schemas.ashoka import SYSTEM_STRUCTURES, PlaceBetRequest
from app.services import ashvatthama, slip_ornament, user_pnl_tracker
from app.services import real_money_service as rms
from app.services.clv_tracker import ticket_clv
from app.services.twin import inplay

logger = logging.getLogger("betdoc.handcrafted_execution")

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
# the slip's book (Ashoka's key) -> the desk's account and the Oracle ledger's book
BOOKS: dict[str, tuple[RealBookmaker, PlacedBookmaker]] = {
    "parimatch": (RealBookmaker.PARIMATCH, PlacedBookmaker.PARIMATCH),
    "1xbet": (RealBookmaker.ONEXBET, PlacedBookmaker.ONEXBET),
}
STRAIGHT_BY_LEGS = {1: PlacedStructure.SINGLE, 2: PlacedStructure.DOUBLE, 3: PlacedStructure.TREBLE}


@dataclass(slots=True)
class Placement:
    ticket: RealBetTicket
    bet: UserPlacedBet
    bankroll: RealBankroll
    monitor: TwinInPlayMonitor | None
    monitor_note: str | None  # why the in-play watch did not start
    stop_loss_pct: float
    replayed: bool = False  # the same slip and booking code again: nothing moved


def _structure(kind: str, legs: int) -> PlacedStructure:
    if kind:
        try:
            return PlacedStructure(kind)
        except ValueError:
            raise rms.RealMoneyError("UNTRACKABLE_SLIP", f"A {kind} slip has no Oracle structure to track it.", 422) from None
    return STRAIGHT_BY_LEGS.get(legs, PlacedStructure.ACCUMULATOR)


def _bet_request(row: ConsultantVerdictModel, ticket: tk.Ticket, book: PlacedBookmaker, structure: PlacedStructure, stake: Decimal,
                 odds: Decimal, code: str, now: datetime) -> PlaceBetRequest:
    """The Oracle bet that tracks the slip's legs, from the signed report (validated before any money moves)."""
    legs: list[dict[str, Any]] = []
    for number, leg in enumerate((row.report.get("slip") or {}).get("legs") or [], 1):
        fixture_id = str(leg.get("leg_id") or "").rsplit("|", 2)[0]
        home, sep, away = str(leg.get("fixture") or "").partition(" vs ")
        if not fixture_id or not sep:
            raise rms.RealMoneyError("UNTRACKABLE_SLIP", f"Leg {number} does not name its fixture: the Oracle could not settle it.", 422)
        if leg.get("price") is None:
            raise rms.RealMoneyError("UNTRACKABLE_SLIP", f"Leg {number} carries no price at {ticket.book_label}.", 422)
        legs.append({"fixture_id": fixture_id, "home": home, "away": away, "sport_key": leg.get("sport_key"), "league": leg.get("league"),
                     "kickoff": leg.get("kickoff"), "market": str(leg.get("market") or ""), "selection": str(leg.get("selection") or ""),
                     "odds": Decimal(str(leg["price"])), "fair_probability": leg.get("fair_probability")})
    try:
        return PlaceBetRequest(slip_id=row.slip_id[:32], source="ASHOKA", bookmaker=book, structure=structure, stake_inr=stake,
                               placed_odds=None if structure in SYSTEM_STRUCTURES else odds, placed_at=now, legs=legs,  # type: ignore[arg-type]
                               notes=f"Bet-Slip {ticket.ticket_no} placed by hand, booking code {code}")
    except ValidationError as exc:
        reason = exc.errors()[0].get("msg", "invalid leg") if exc.errors() else "invalid leg"
        raise rms.RealMoneyError("UNTRACKABLE_SLIP", f"The slip cannot be tracked leg by leg: {reason}", 422) from exc


def _sport(report_legs: list[dict[str, Any]]) -> str:
    families = {rms._canonical_sport(str(leg.get("sport_key") or "").split("_")[0]) for leg in report_legs if leg.get("sport_key")}  # noqa: SLF001
    return families.pop() if len(families) == 1 else ("multi" if families else "unknown")


def _ticket_row(row: ConsultantVerdictModel, ticket: tk.Ticket, book: RealBookmaker, structure: PlacedStructure, odds: Decimal, code: str,
                settings: Settings, now: datetime) -> RealBetTicket:
    report_legs = list((row.report.get("slip") or {}).get("legs") or [])
    legs = list(ticket.legs)
    single = legs[0] if len(legs) == 1 else None
    p = ticket.joint_probability
    if p is None or not 0 < p < 1:
        raise rms.RealMoneyError("NO_PROBABILITY", "The verdict carries no joint probability: the desk cannot set the slip's odds floor.", 422)
    edge = ticket.joint_ev * 100 if ticket.joint_ev is not None else float((Decimal(str(p)) * odds - 1) * 100)
    leagues = sorted({leg.league for leg in legs if leg.league})
    capital = row.report.get("consultant") or {}
    return RealBetTicket(
        id=uuid.uuid4(), user_id=row.user_id, ticket_reference=f"{rms.BOOK_PREFIX[book.value]}-SLIP-{now:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        bookmaker=book.value, sport=_sport(report_legs), league=", ".join(leagues)[:128],
        match_id=(str(report_legs[0]["leg_id"]).rsplit("|", 2)[0] if single else f"slip:{row.slip_id}")[:128],
        event_name=(single.fixture if single else (ticket.slip_title or " + ".join(leg.fixture for leg in legs)))[:255],
        market_type=(single.market if single else f"{structure.value.replace('_', ' ').title()} ({len(legs)} legs)")[:100],
        selection=(str(report_legs[0].get("selection") or "") if single else " / ".join(leg.code or leg.market for leg in legs))[:128],
        recommended_stake=ticket.stake_inr, placed_stake=rms.ZERO, currency="INR", target_odds=rms._round_odds(ticket.odds or odds),  # noqa: SLF001
        placed_odds=None, minimum_profitable_odds=rms.calculate_odds_floor(p, settings.REAL_MIN_EDGE), true_probability=p, model_edge_pct=round(edge, 2),
        kelly_fraction=float(capital.get("kelly_fraction") or 0.0), deep_link_url=ashvatthama.venue_url(settings, rms.BOOK_VENUE[book.value]) or "",
        clipboard_payload=tk.coupon_text(ticket), bookmaker_slip_id=None,
        quote_market=(str(report_legs[0].get("market") or "")[:64] or None) if single else None,
        quote_selection=(str(report_legs[0].get("selection") or "")[:32] or None) if single else None,
        settleable_from_score=False,  # it settles as its Oracle bet settles
        verdict_id=row.id, status=RealTicketStatus.PENDING_PLACEMENT.value, gross_return=rms.ZERO, settlement_payout=rms.ZERO, net_profit=rms.ZERO,
        tds_deducted=rms.ZERO, vetting_pillar_score=max(0.0, min(100.0, ticket.slip_score)), odds_decay_flag=False,
        notes=f"Bet-Slip {ticket.ticket_no} placed by hand at {rms.BOOK_LABEL[book.value]}, booking code {code}", created_at=now,
    )


async def _monitor(session: AsyncSession, bet_id: uuid.UUID) -> TwinInPlayMonitor | None:
    return (await session.execute(select(TwinInPlayMonitor).where(TwinInPlayMonitor.bet_id == bet_id))).scalars().first()


async def confirm_slip_placement(
    db: AsyncSession,
    redis: Redis | None,
    settings: Settings,
    user_id: uuid.UUID,
    report_id: uuid.UUID,
    booking_code: str,
    now: datetime,
    *,
    placed_odds: Decimal | None = None,
    placed_stake: Decimal | None = None,
    override_odds_decay: bool = False,
) -> Placement:
    code = booking_code.strip()
    if not code:
        raise rms.RealMoneyError("NO_BOOKING_CODE", "Copy the booking code the bookmaker issued for the slip.", 422)
    # 1. the user's own verdict, its seal intact, approved gold with a stake
    try:
        row, _, ticket = await slip_ornament.issued(db, settings, user_id, report_id, now)
    except slip_ornament.OrnamentRefusal as exc:
        raise rms.RealMoneyError(exc.reason, exc.message, exc.status_code) from exc
    placed = (await db.execute(select(RealBetTicket).where(RealBetTicket.verdict_id == row.id))).scalar_one_or_none()
    if placed is not None:
        if placed.bookmaker_slip_id != code or placed.user_placed_bet_id is None:
            raise rms.RealMoneyError("ALREADY_PLACED", f"This slip is already placed as {placed.ticket_reference} (booking code {placed.bookmaker_slip_id}).", 409,
                                     ticket_reference=placed.ticket_reference)
        bet = await db.get(UserPlacedBet, placed.user_placed_bet_id)
        monitor = None if bet is None else await _monitor(db, bet.id)
        stop = slm.StopLossPolicy.from_settings(settings).clamp(monitor.stop_loss_pct if monitor else (ticket.stop_pct or None))
        return Placement(placed, bet, await rms.get_or_create_bankroll(db, user_id, now), monitor, None, stop, replayed=True)  # type: ignore[arg-type]

    # 2. a book the desk keeps an account at, legs the Oracle can settle
    books = BOOKS.get(ticket.book or "")
    if books is None:
        where = ASHOKA_BOOKMAKERS.get(ticket.book or "", ticket.book) if ticket.book else "no single book"
        raise rms.RealMoneyError("BOOK_NOT_SUPPORTED", f"The slip was priced at {where}; the real-money desk keeps Parimatch and 1xBet accounts only.", 422)
    real_book, placed_book = books
    structure = _structure(ticket.kind, len(ticket.legs))
    odds = placed_odds if placed_odds is not None else ticket.odds
    if odds is None:
        raise rms.RealMoneyError("NO_ODDS", "The ticket carries no total odds: enter the odds the book gave.", 422)
    stake = rms._round_paisa(placed_stake if placed_stake is not None else ticket.stake_inr)  # noqa: SLF001
    request = _bet_request(row, ticket, placed_book, structure, stake, rms._round_odds(odds), code, now)  # noqa: SLF001

    # 3. the kill switch, the book's lock, SHIVAJI's breakers
    await rms.execution_gate(db, redis, settings, user_id, real_book.value, now)

    # 4. the ticket, then the stake locked (funds, the 5% ceiling, the odds floor)
    row_ticket = _ticket_row(row, ticket, real_book, structure, odds, code, settings, now)
    db.add(row_ticket)
    await db.flush()
    await rms.confirm_placement(db=db, settings=settings, user_id=user_id, ticket_ref=row_ticket.ticket_reference, placed_odds=odds, placed_stake=stake,
                                slip_id=code, override_odds_decay=override_odds_decay)

    # 5. the Oracle bet that settles it, the booking code on it and on the Bet-Slip record
    bet = await user_pnl_tracker.record_bet(db, user_id, request, now)
    bet.booking_code = code
    row_ticket.user_placed_bet_id = bet.id
    await slip_ornament.mark_booked(db, row, code, now)
    await db.flush()

    # 6. the twin's in-play watch, with the verdict's stop-loss
    monitor, note, stop = await _watch(db, redis, settings, bet, now, ticket.stop_pct or None)
    bankroll = await rms.get_or_create_bankroll(db, user_id, now)
    logger.info("handcrafted placement %s: ₹%s at %s on %s (%s)", row_ticket.ticket_reference, stake, odds, real_book.value, code)
    return Placement(row_ticket, bet, bankroll, monitor, note, stop)


async def _watch(db: AsyncSession, redis: Redis | None, settings: Settings, bet: UserPlacedBet, now: datetime,
                 stop_pct: float | None) -> tuple[TwinInPlayMonitor | None, str | None, float]:
    """The twin's in-play watch on a placed bet (the stop-loss given, else the policy's), or why it did not start."""
    monitor, note = None, None
    try:
        monitor = await inplay.start(db, redis, settings, bet, now, target_profit_pct=settings.REAL_CASHOUT_TARGET_PROFIT_PCT, stop_loss_pct=stop_pct)
    except (inplay.NoLivePrice, ValueError) as exc:  # a system's lines settle separately; a leg without a probability
        note = f"In-play watch not started: {exc}"
    stop = slm.StopLossPolicy.from_settings(settings).clamp(monitor.stop_loss_pct if monitor is not None else stop_pct)
    return monitor, note, stop


async def confirm_ticket_placement(
    db: AsyncSession,
    redis: Redis | None,
    settings: Settings,
    user_id: uuid.UUID,
    ticket_reference: str,
    booking_code: str,
    now: datetime,
    *,
    placed_odds: Decimal | None = None,
    placed_stake: Decimal | None = None,
    override_odds_decay: bool = False,
) -> Placement:
    """Group 115: a multi-leg ticket the desk recorded (the system splitter), placed by hand at the book: the gate, the
    stake locked against its booking code, its legs as the Oracle bet that settles it, and the in-play watch."""
    code = booking_code.strip()
    if not code:
        raise rms.RealMoneyError("NO_BOOKING_CODE", "Copy the booking code the bookmaker issued for the ticket.", 422)
    ticket = (await db.execute(select(RealBetTicket).where(RealBetTicket.ticket_reference == ticket_reference,
                                                          RealBetTicket.user_id == user_id))).scalar_one_or_none()
    if ticket is None:
        raise rms.RealMoneyError("TICKET_NOT_FOUND", f"Ticket {ticket_reference} does not exist", 404)
    if ticket.status == RealTicketStatus.PLACED.value and ticket.bookmaker_slip_id == code and ticket.user_placed_bet_id is not None:
        bet = await db.get(UserPlacedBet, ticket.user_placed_bet_id)
        monitor = None if bet is None else await _monitor(db, bet.id)
        stop = slm.StopLossPolicy.from_settings(settings).clamp(None if monitor is None else monitor.stop_loss_pct)
        return Placement(ticket, bet, await rms.get_or_create_bankroll(db, user_id, now), monitor, None, stop, replayed=True)  # type: ignore[arg-type]
    if not ticket.legs:
        raise rms.RealMoneyError("NOT_A_DESK_TICKET", f"Ticket {ticket_reference} has no recorded legs: confirm it on the desk's own placement.", 409)
    if ticket.status != RealTicketStatus.PENDING_PLACEMENT.value:
        raise rms.RealMoneyError("INVALID_STATE", f"Ticket {ticket_reference} is already {ticket.status}", 409)
    odds = rms._round_odds(placed_odds if placed_odds is not None else ticket.target_odds)  # noqa: SLF001
    stake = rms._round_paisa(placed_stake if placed_stake is not None else ticket.recommended_stake)  # noqa: SLF001
    placed_book = {RealBookmaker.PARIMATCH.value: PlacedBookmaker.PARIMATCH, RealBookmaker.ONEXBET.value: PlacedBookmaker.ONEXBET}[ticket.bookmaker]
    structure = STRAIGHT_BY_LEGS.get(len(ticket.legs), PlacedStructure.ACCUMULATOR)
    try:
        request = PlaceBetRequest(
            slip_id=ticket.ticket_reference[-32:], source="ASHOKA", bookmaker=placed_book, structure=structure, stake_inr=stake, placed_odds=odds, placed_at=now,
            legs=[{"fixture_id": leg["fixture_id"], "home": leg["home"], "away": leg["away"], "sport_key": leg.get("sport_key"), "league": leg.get("league"),
                   "kickoff": leg.get("kickoff"), "market": leg["market"], "selection": leg["selection"], "odds": Decimal(str(leg["odds"])),
                   "fair_probability": leg.get("fair_probability")} for leg in ticket.legs],  # type: ignore[misc]
            notes=f"System ticket {ticket.ticket_reference} placed by hand, booking code {code}",
        )
    except (ValidationError, KeyError) as exc:
        raise rms.RealMoneyError("UNTRACKABLE_SLIP", f"The ticket's legs cannot be tracked leg by leg: {exc}", 422) from exc
    await rms.execution_gate(db, redis, settings, user_id, ticket.bookmaker, now)
    await rms.confirm_placement(db=db, settings=settings, user_id=user_id, ticket_ref=ticket.ticket_reference, placed_odds=odds, placed_stake=stake,
                                slip_id=code, override_odds_decay=override_odds_decay)
    bet = await user_pnl_tracker.record_bet(db, user_id, request, now)
    bet.booking_code = code
    ticket.user_placed_bet_id = bet.id
    await db.flush()
    monitor, note, stop = await _watch(db, redis, settings, bet, now, None)
    logger.info("system ticket placed %s: ₹%s at %s on %s (%s)", ticket.ticket_reference, stake, odds, ticket.bookmaker, code)
    return Placement(ticket, bet, await rms.get_or_create_bankroll(db, user_id, now), monitor, note, stop)


def placement_dict(placement: Placement) -> dict[str, Any]:
    ticket, monitor = placement.ticket, placement.monitor
    return {
        "ticket": rms.ticket_view(ticket), "booking_code": ticket.bookmaker_slip_id, "user_placed_bet_id": str(placement.bet.id),
        "stop_loss_pct": placement.stop_loss_pct, "stop_loss_floor_inr": float(slm.floor(ticket.placed_stake, placement.stop_loss_pct)),
        "monitor": None if monitor is None else inplay.monitor_view(monitor), "monitor_note": placement.monitor_note, "replayed": placement.replayed,
        "developer_name": DEVELOPER_CREDIT,
    }


async def placement_view(session: AsyncSession, settings: Settings, verdict_id: uuid.UUID) -> dict[str, Any] | None:
    """The real ticket a Bet-Slip was placed as, for its view: status, stake, the stop-loss floor, the watch."""
    ticket = (await session.execute(select(RealBetTicket).where(RealBetTicket.verdict_id == verdict_id))).scalar_one_or_none()
    return None if ticket is None else await ticket_placement(session, settings, ticket)


async def ticket_placement(session: AsyncSession, settings: Settings, ticket: RealBetTicket) -> dict[str, Any]:
    """A real ticket as the placement desks show it (the Bet-Slip, the system tickets): status, stake, the stop-loss floor, the
    watch, and (Group 116) its closing line value once the Oracle bet that tracks it has a recorded close."""
    monitor = None if ticket.user_placed_bet_id is None else await _monitor(session, ticket.user_placed_bet_id)
    bet = None if ticket.user_placed_bet_id is None else await session.get(UserPlacedBet, ticket.user_placed_bet_id)
    stop = slm.StopLossPolicy.from_settings(settings).clamp(None if monitor is None else monitor.stop_loss_pct)
    return {
        "ticket_reference": ticket.ticket_reference, "status": ticket.status, "bookmaker": ticket.bookmaker, "booking_code": ticket.bookmaker_slip_id,
        "placed_stake": str(ticket.placed_stake), "placed_odds": None if ticket.placed_odds is None else str(ticket.placed_odds),
        "placed_at": None if ticket.placed_at is None else ticket.placed_at.isoformat(), "stop_loss_pct": stop,
        "stop_loss_floor_inr": str(slm.floor(ticket.placed_stake, stop)), "monitor_active": bool(monitor and monitor.is_active),
        "current_win_prob": None if monitor is None else round(monitor.current_win_prob, 4),
        "fair_value_inr": None if monitor is None or monitor.fair_value_inr is None else str(monitor.fair_value_inr),
        "gross_return": str(ticket.gross_return), "settlement_payout": str(ticket.settlement_payout), "net_profit": str(ticket.net_profit),
        "tds_deducted": str(ticket.tds_deducted), "settled_at": None if ticket.settled_at is None else ticket.settled_at.isoformat(),
        "clv": None if bet is None else ticket_clv(bet.placed_odds, bet.closing_odds),
    }


__all__ = ["BOOKS", "Placement", "confirm_slip_placement", "confirm_ticket_placement", "placement_dict", "placement_view", "ticket_placement"]
