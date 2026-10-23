"""The street-smart multi-sport synthesizer and the three-ticket system splitter, on the live board (Group 115, points b.5
and b.16). Developed for Amit Ashok Kumar Patnaik.

BetDoc is Krishna, the strategist; the user is Arjuna, who places every ticket by hand at Parimatch or 1xBet. Nothing
here places a bet or moves money.

* ``anchors``: every leg on Ashoka's live board (``ashoka_market.load_candidates``) kicking off within
  ``SYNTH_HORIZON_HOURS``, priced at the chosen book within the quote age Ashoka vets with, with the consensus giving it
  at least ``SYNTH_MIN_LEG_PROBABILITY`` and the book paying at least ``SYNTH_MIN_LEG_EV`` over fair. Each carries its
  book's own selection code. No sample slate: an empty board answers empty.
* ``synthesize_for``: the anchors through ``street_smart.synthesize`` toward the user's stake -> return target, each leg
  with its recipe in measured words.
* ``split_for``: the legs the user chose (by id), priced again at the book now (a leg gone from the board or stale is
  refused and named), through ``street_smart.split``.
* ``record_split``: the three tickets as ready-to-place real-money tickets (PENDING_PLACEMENT: nothing locked), each
  with its legs, odds floor, coupon in the book's codes and deep link. Placing one is confirmed with the book's booking
  code (``handcrafted_execution_service.confirm_ticket_placement``): the stake is locked then, and its legs become the
  Oracle bet that settles it leg by leg.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain import street_smart as ss
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, market_selection_label, search_code
from app.domain.oracle.parlay_engine import LegCandidate
from app.domain.slip_ornament.ticket import sport_label
from app.models.real_money import RealBetTicket, RealBookmaker, RealTicketStatus
from app.services import ashoka_market, ashvatthama
from app.services import handcrafted_execution_service as hes
from app.services import real_money_service as rms

logger = logging.getLogger("betdoc.street_smart")

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
DESK_BOOKS: dict[str, RealBookmaker] = {"parimatch": RealBookmaker.PARIMATCH, "1xbet": RealBookmaker.ONEXBET}


class SynthesisError(Exception):
    def __init__(self, reason: str, message: str, status_code: int = 409, **detail: Any) -> None:
        super().__init__(message)
        self.reason, self.message, self.status_code, self.detail = reason, message, status_code, detail


def _sport(sport_key: str | None, settings: Settings) -> str:
    return sport_label(sport_key, settings.MANUAL_PARLAY_SPORTS) or (sport_key or "unknown").split("_")[0].title()


def priced(leg: LegCandidate, book: str, settings: Settings, now: datetime) -> ss.PricedLeg | None:
    """The leg at ``book`` now: its fresh price, the consensus probability and that price's EV (None: not priced)."""
    quote = leg.quotes.get(book)
    p = ashoka_market.fair_value_inputs(leg)
    if quote is None or p is None or quote.age(now) > settings.ASHOKA_MAX_QUOTE_AGE_SECONDS or quote.odds <= 1.0:
        return None
    try:
        ev = leg.ev(quote.net_odds)
    except ValueError:
        return None
    return ss.PricedLeg(
        leg_id=leg.leg_id, fixture_id=leg.fixture_id, home=leg.home, away=leg.away, sport_key=leg.sport_key, sport=_sport(leg.sport_key, settings),
        league=leg.league, kickoff=leg.kickoff, market=leg.market.key, selection=leg.selection,
        label=market_selection_label(leg.market.key, leg.selection, leg.home, leg.away), book=book, odds=Decimal(str(quote.odds)), probability=p, ev=ev,
        consensus_books=leg.consensus_books, code=search_code(book, leg.market.key, leg.selection),
    )


def anchors(candidates: Iterable[LegCandidate], book: str, settings: Settings, now: datetime, *, until: datetime | None = None) -> list[ss.PricedLeg]:
    """Every leg at ``book`` the synthesizer may use: within the horizon (or before ``until``), at least the probability
    floor, and the price at least ``SYNTH_MIN_LEG_EV`` over fair."""
    end = until or now + timedelta(hours=settings.SYNTH_HORIZON_HOURS)
    out: list[ss.PricedLeg] = []
    for leg in candidates:
        if leg.kickoff is None or not now < leg.kickoff <= end:
            continue
        row = priced(leg, book, settings, now)
        if row is not None and row.probability >= settings.SYNTH_MIN_LEG_PROBABILITY and row.ev >= settings.SYNTH_MIN_LEG_EV:
            out.append(row)
    return out


async def board(redis: Redis | None, settings: Settings, now: datetime) -> list[LegCandidate]:
    if redis is None:
        raise SynthesisError("BOARD_UNAVAILABLE", "The live board (Redis) is not reachable: nothing can be priced.", 503)
    try:
        candidates, _ = await ashoka_market.load_candidates(redis, settings, now)
    except (RedisError, OSError, TimeoutError) as exc:
        raise SynthesisError("BOARD_UNAVAILABLE", "The live board (Redis) did not answer: nothing can be priced.", 503) from exc
    return candidates


def _book(book: str) -> str:
    key = book.strip().casefold()
    if key not in DESK_BOOKS:
        raise SynthesisError("BOOK_NOT_SUPPORTED", "The real-money desk keeps Parimatch and 1xBet accounts only.", 422)
    return key


def _local(moment: datetime | None, settings: Settings) -> str:
    return "time not listed" if moment is None else moment.astimezone(ZoneInfo(settings.ORACLE_TIMEZONE)).strftime("%a %d %b, %H:%M IST")


def recipe(leg: ss.PricedLeg, rank: int, sports_in_slip: dict[str, int], multi: bool, settings: Settings) -> str:
    """Why the leg is on the slip, in measured words."""
    book = ASHOKA_BOOKMAKERS.get(leg.book, leg.book)
    consensus = f"a {leg.consensus_books}-book consensus" if leg.consensus_books else "the books' consensus"
    parts = [f"{leg.label} at {book} {leg.odds}: fair {leg.probability:.1%} from {consensus} against "
             f"{1 / float(leg.odds):.1%} implied, EV {leg.ev:+.1%}"]
    role = f"anchor #{rank}, the {'safest' if rank == 1 else 'next safest'} priced leg left on the board after one leg per fixture"
    if multi and sports_in_slip[leg.sport] == 1:
        role += f"; the slip's only {leg.sport} leg, so a result there cannot spill into the others"
    parts.append(role)
    parts.append(f"kicks off {_local(leg.kickoff, settings)}")
    return "; ".join(parts) + "."


def synthesis_payload(result: ss.Synthesis, book: str, settings: Settings) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for leg in result.legs:
        counts[leg.sport] = counts.get(leg.sport, 0) + 1
    multi = len(counts) > 1
    return {
        "book": book, "book_label": ASHOKA_BOOKMAKERS.get(book, book),
        "legs": [{**leg.as_dict(), "prices": {book: float(leg.odds)}, "recipe_rationale": recipe(leg, i, counts, multi, settings)}
                 for i, leg in enumerate(result.legs, 1)],
        "leg_count": len(result.legs), "total_odds": str(result.total_odds), "target_stake_inr": str(result.stake),
        "target_return_inr": str(result.target_return), "target_odds": str(result.target_odds), "projected_return_inr": str(result.projected_return),
        "joint_probability": round(result.joint_probability, 6), "joint_ev": round(result.joint_ev, 4), "expected_return_inr": str(result.expected_return),
        "reached_target": result.reached_target, "shortfall": result.shortfall, "sports_represented": result.sports, "is_multi_sport": multi,
        "diversification_relaxed": result.diversification_relaxed, "anchors_available": result.anchors_available,
        "rules": {"min_leg_probability": settings.SYNTH_MIN_LEG_PROBABILITY, "min_leg_ev": settings.SYNTH_MIN_LEG_EV, "min_legs": settings.SYNTH_MIN_LEGS,
                  "max_sport_share": settings.SYNTH_MAX_SPORT_SHARE, "horizon_hours": settings.SYNTH_HORIZON_HOURS,
                  "max_quote_age_seconds": settings.ASHOKA_MAX_QUOTE_AGE_SECONDS},
        "independence_note": "The joint probability multiplies the legs' probabilities: they are of different fixtures, assumed independent.",
        "developer_name": DEVELOPER_CREDIT,
    }


async def synthesize_for(redis: Redis | None, settings: Settings, now: datetime, *, stake: Decimal, target_return: Decimal, book: str,
                         max_legs: int | None = None, preferred_sports: Sequence[str] | None = None) -> dict[str, Any]:
    book = _book(book)
    legs = anchors(await board(redis, settings, now), book, settings, now)
    limit = min(max_legs or settings.SYNTH_MAX_LEGS, settings.SYNTH_MAX_LEGS)
    if limit < settings.SYNTH_MIN_LEGS:
        raise SynthesisError("TOO_FEW_LEGS", f"A synthesized slip has at least {settings.SYNTH_MIN_LEGS} legs.", 422)
    try:
        result = ss.synthesize(legs, stake=stake, target_return=target_return, min_legs=settings.SYNTH_MIN_LEGS, max_legs=limit,
                               max_sport_share=settings.SYNTH_MAX_SPORT_SHARE, preferred_sports=preferred_sports)
    except ValueError as exc:
        raise SynthesisError("INVALID_TARGET", str(exc), 422) from exc
    return synthesis_payload(result, book, settings)


def price_legs(candidates: Iterable[LegCandidate], leg_ids: Sequence[str], book: str, settings: Settings, now: datetime) -> list[ss.PricedLeg]:
    """The chosen legs at ``book`` now, in the order given; refused (and named) when any is gone or stale."""
    if len(set(leg_ids)) != len(leg_ids):
        raise SynthesisError("DUPLICATE_LEGS", "A leg appears twice.", 422)
    by_id = {leg.leg_id: leg for leg in candidates}
    out, missing = [], []
    for leg_id in leg_ids:
        leg = by_id.get(leg_id)
        row = None if leg is None or leg.kickoff is None or leg.kickoff <= now else priced(leg, book, settings, now)
        if row is None:
            missing.append(leg_id)
        else:
            out.append(row)
    if missing:
        raise SynthesisError("STALE_LEGS", f"{len(missing)} leg(s) are no longer priced at {ASHOKA_BOOKMAKERS.get(book, book)} (gone, started or stale): "
                             "synthesize again.", 409, missing=missing)
    return out


def _slip_dict(slip: ss.SplitSlip, *, reference: str | None = None) -> dict[str, Any]:
    return {"tier": slip.tier, "tier_name": slip.name, "tag": slip.tag, "leg_count": len(slip.legs), "stake_inr": str(slip.stake), "odds": str(slip.odds),
            "projected_payout_inr": str(slip.payout), "win_probability": round(slip.probability, 6), "ev": round(slip.ev, 4),
            "covers_session_stake": slip.covers_session, "legs": [leg.as_dict() for leg in slip.legs], "ticket_reference": reference}


def split_payload(result: ss.Split, book: str, references: Sequence[str | None] = (None, None, None)) -> dict[str, Any]:
    a, b, c = result.slips
    shield = (f"Slip A pays ₹{a.payout:,.2f} at {a.odds:.2f}x on its ₹{a.stake:,.2f} stake: it alone returns the ₹{result.session_stake:,.2f} session stake."
              if result.shield_active else
              f"Slip A's {len(a.legs)} safest anchors reach {a.odds:.2f}x; returning the ₹{result.session_stake:,.2f} session stake from its ₹{a.stake:,.2f} "
              f"needs {result.needed_a_odds:.2f}x, so the capital shield is off for these legs.")
    explanation = (f"Session stake ₹{result.session_stake:,.2f}: Fortress ₹{a.stake:,.2f} on {len(a.legs)} legs, Growth ₹{b.stake:,.2f} on {len(b.legs)}, "
                   f"Moonshot ₹{c.stake:,.2f} on all {len(c.legs)}. {shield} The tickets are nested: an anchor that loses is on all three, so the split "
                   "protects against a loss among the riskier legs, not among the anchors.")
    return {"book": book, "book_label": ASHOKA_BOOKMAKERS.get(book, book), "total_session_stake_inr": str(result.session_stake),
            "slip_a_fortress": _slip_dict(a, reference=references[0]), "slip_b_growth": _slip_dict(b, reference=references[1]),
            "slip_c_moonshot": _slip_dict(c, reference=references[2]), "capital_shield_active": result.shield_active,
            "needed_slip_a_odds": str(result.needed_a_odds), "scenarios": result.scenarios, "expected_session_return_inr": str(result.expected_return),
            "recover_stake_probability": round(result.recover_probability, 6), "single_ticket": result.single, "formula_explanation": explanation,
            "developer_name": DEVELOPER_CREDIT}


async def split_for(redis: Redis | None, settings: Settings, now: datetime, *, leg_ids: Sequence[str], session_stake: Decimal, book: str) -> ss.Split:
    book = _book(book)
    legs = price_legs(await board(redis, settings, now), leg_ids, book, settings, now)
    try:
        return ss.split(legs, session_stake, settings.SYNTH_SPLIT_SHARES, min_legs=settings.SYNTH_SPLIT_MIN_LEGS)
    except ValueError as exc:
        raise SynthesisError("CANNOT_SPLIT", str(exc), 422) from exc


# ================================================================ the three tickets, ready to place
def coupon(slip: ss.SplitSlip, reference: str, book_label: str) -> str:
    lines = [f"BETDOC SYSTEM TICKET · {reference}", f"{book_label} · {slip.name} · {len(slip.legs)}-leg accumulator @ {slip.odds:.2f}",
             f"Stake ₹{slip.stake:,.2f} · returns ₹{slip.payout:,.2f} · wins {slip.probability:.1%}"]
    lines += [f"{i}) {leg.fixture} ({leg.league or leg.sport}): {leg.label} [{leg.code}] @ {leg.odds}" for i, leg in enumerate(slip.legs, 1)]
    lines.append(f"Developed for {DEVELOPER_CREDIT}")
    return "\n".join(lines)


async def record_split(db: AsyncSession, settings: Settings, user_id: uuid.UUID, result: ss.Split, book: str, now: datetime) -> list[RealBetTicket]:
    """The three tickets, PENDING_PLACEMENT: nothing is locked until each one's placement is confirmed."""
    book = _book(book)
    desk = DESK_BOOKS[book]
    session = uuid.uuid4().hex[:6].upper()
    label = ASHOKA_BOOKMAKERS.get(book, book)
    tickets = []
    for slip, share in zip(result.slips, settings.SYNTH_SPLIT_SHARES, strict=True):
        reference = f"{rms.BOOK_PREFIX[desk.value]}-SPLIT-{slip.tier}-{now:%Y%m%d}-{session}"
        families = {leg.sport for leg in slip.legs}
        leagues = sorted({leg.league for leg in slip.legs if leg.league})
        p = slip.probability
        ticket = RealBetTicket(
            id=uuid.uuid4(), user_id=user_id, ticket_reference=reference, bookmaker=desk.value,
            sport=(families.pop().lower() if len(families) == 1 else "multi")[:64], league=", ".join(leagues)[:128], match_id=f"split:{session}:{slip.tier}",
            event_name=(f"{slip.name}: " + " + ".join(leg.fixture for leg in slip.legs))[:255], market_type=f"System split {slip.tier} ({len(slip.legs)}-leg accumulator)",
            selection=" / ".join(leg.code or leg.label for leg in slip.legs)[:128], recommended_stake=slip.stake, placed_stake=rms.ZERO, currency="INR",
            target_odds=slip.odds, placed_odds=None, minimum_profitable_odds=rms.calculate_odds_floor(p, settings.REAL_MIN_EDGE), true_probability=p,
            model_edge_pct=round(slip.ev * 100, 2), kelly_fraction=0.0, deep_link_url=ashvatthama.venue_url(settings, book) or "",
            clipboard_payload=coupon(slip, reference, label), bookmaker_slip_id=None, quote_market=None, quote_selection=None, settleable_from_score=False,
            legs=[leg.as_dict() for leg in slip.legs], status=RealTicketStatus.PENDING_PLACEMENT.value, gross_return=rms.ZERO, settlement_payout=rms.ZERO,
            net_profit=rms.ZERO, tds_deducted=rms.ZERO, vetting_pillar_score=0.0, odds_decay_flag=False,
            notes=f"System split {session}: slip {slip.tier} of 3, {share:.0%} of a ₹{result.session_stake:,.2f} session at {label}", created_at=now,
        )
        db.add(ticket)
        tickets.append(ticket)
    await db.flush()
    logger.info("system split %s recorded for %s: %s", session, user_id, ", ".join(t.ticket_reference for t in tickets))
    return tickets


async def system_tickets(db: AsyncSession, settings: Settings, user_id: uuid.UUID, limit: int = 30) -> list[dict[str, Any]]:
    """The user's split tickets, newest first, each with its placement (stake locked, stop-loss floor, in-play watch)."""
    rows = (await db.execute(select(RealBetTicket).where(RealBetTicket.user_id == user_id, RealBetTicket.legs.is_not(None))
                             .order_by(RealBetTicket.created_at.desc(), RealBetTicket.ticket_reference).limit(limit))).scalars().all()
    return [{**rms.ticket_view(t), "legs": t.legs, "placement": await hes.ticket_placement(db, settings, t)} for t in rows]


async def cancel_ticket(db: AsyncSession, user_id: uuid.UUID, reference: str, now: datetime) -> RealBetTicket:
    """A recorded ticket the user will not place: CANCELLED (nothing was locked, nothing moves)."""
    ticket = (await db.execute(select(RealBetTicket).where(RealBetTicket.ticket_reference == reference, RealBetTicket.user_id == user_id))).scalar_one_or_none()
    if ticket is None or ticket.legs is None:
        raise SynthesisError("TICKET_NOT_FOUND", f"No system ticket {reference} of yours.", 404)
    if ticket.status != RealTicketStatus.PENDING_PLACEMENT.value:
        raise SynthesisError("NOT_PENDING", f"Ticket {reference} is {ticket.status}: only a ticket not yet placed can be cancelled.", 409)
    ticket.status, ticket.settled_at = RealTicketStatus.CANCELLED.value, now
    ticket.notes = (ticket.notes + " | " if ticket.notes else "") + "Cancelled before placement"
    await db.flush()
    return ticket


__all__ = ["DESK_BOOKS", "SynthesisError", "anchors", "board", "cancel_ticket", "coupon", "price_legs", "priced", "record_split", "split_for", "split_payload",
           "synthesis_payload", "synthesize_for", "system_tickets"]
