"""The Bet-Slip ticket (Group 106, ASHVATTHAMA, points b.7 and b.12). Developed for Amit Ashok Kumar Patnaik.

A ticket is the collectible face of one signed Consultant verdict: it is issued only for a verdict the Consultant
APPROVED (gold grade) whose HMAC seal still holds against the stored report, and every figure on it is the report's
own, as signed: the book, each leg's price there, KUMBHA's stake, the payout and its Section 115BBJ / 194BA tax, the
stop-loss floor, the Slip Score. Nothing is recomputed or estimated here, and no figure is invented when the report
lacks one (USD shows only if the verdict had a fresh rate).

Each leg carries the wording a bookmaker's slip uses: the fixture as both priority books search it (``"Arsenal vs
Chelsea"``), the market and selection (``"1X2: Arsenal"``) and the book's own code (1xBet ``W1``, Parimatch ``1``).
Kick-off and signing times are shown in ``ORACLE_TIMEZONE`` (IST). The ticket's prices are the signed ones; past
``BET_SLIP_FRESH_MINUTES`` it says to re-check them at the book.

The text channels: the coupon (copy, Telegram, email's plain part, WhatsApp), a compact SMS line, and the
WhatsApp and mail deep links.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, search_code
from app.domain.oracle.slip_formatter import inr
from app.models.consultant import VerdictStatus

TICKET_PREFIX = "BD-"
SMS_LIMIT = 300  # the Sentinel's Twilio dispatcher adds its headline inside its own 320
RULE = "─" * 34
# The badge each bookmaker's slip wears on the ticket (Group 106 brief): foreground on background
BOOK_BADGES: dict[str, tuple[str, str]] = {
    "parimatch": ("#F8FF13", "#111111"), "1xbet": ("#007ACC", "#FFFFFF"), "stake": ("#00E701", "#1A2C38"),
}
GOLD_BADGE = ("#C89B3C", "#1C1917")


class NotIssuable(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason, self.message = reason, message


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _when(iso: str | None, zone: ZoneInfo) -> str | None:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return moment.astimezone(zone).strftime("%a %d %b %Y, %H:%M %Z")


def sport_label(sport_key: str | None, sports: Mapping[str, Sequence[str]]) -> str | None:
    key = (sport_key or "").casefold()
    return next((label for label, prefixes in sports.items() if any(key.startswith(p) for p in prefixes)), None)


@dataclass(frozen=True, slots=True)
class TicketLeg:
    number: int
    leg_id: str
    sport: str | None
    league: str | None
    fixture: str
    kickoff: str | None  # in ORACLE_TIMEZONE
    market: str  # "1X2: Arsenal": the market and selection as the slip shows them
    code: str  # the book's own code: 1xBet "W1", Parimatch "1"
    price: Decimal | None
    fair_probability: float | None
    ev: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"number": self.number, "leg_id": self.leg_id, "sport": self.sport, "league": self.league, "fixture": self.fixture, "kickoff": self.kickoff,
                "market": self.market, "code": self.code, "price": None if self.price is None else str(self.price),
                "fair_probability": self.fair_probability, "implied_probability": None if not self.price else round(float(1 / self.price), 4), "ev": self.ev}


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket_no: str
    report_id: str
    signed_at: datetime
    signed_at_local: str
    age_minutes: int
    fresh: bool
    book: str | None
    book_label: str
    badge: tuple[str, str]  # (foreground, background)
    status: str
    title: str
    summary: str
    slip_score: float
    tier: str
    kind: str
    slip_title: str | None
    legs: tuple[TicketLeg, ...]
    odds: Decimal | None
    stake_inr: Decimal
    stake_usd: Decimal | None
    inr_per_usd: str | None
    payout: Decimal | None
    net_winnings: Decimal | None
    tds: Decimal | None
    take_home: Decimal | None
    tds_rate_pct: float | None
    stop_pct: float
    stop_floor: Decimal
    joint_probability: float | None
    joint_ev: float | None
    seal: str
    signed_by: str
    architect: str
    booking_code: str | None = None

    @property
    def watermark(self) -> str:
        return f"Handcrafted by BetDoc Core Engine · Architect: {self.architect}"

    def as_dict(self) -> dict[str, Any]:
        money = lambda v: None if v is None else str(v)  # noqa: E731
        return {
            "ticket_no": self.ticket_no, "report_id": self.report_id, "signed_at": self.signed_at.isoformat(), "signed_at_local": self.signed_at_local,
            "age_minutes": self.age_minutes, "fresh": self.fresh, "book": self.book, "book_label": self.book_label,
            "badge": {"foreground": self.badge[0], "background": self.badge[1]},
            "verdict": {"status": self.status, "title": self.title, "summary": self.summary}, "slip_score": self.slip_score, "tier": self.tier,
            "kind": self.kind, "slip_title": self.slip_title, "legs": [leg.as_dict() for leg in self.legs], "odds": money(self.odds),
            "stake_inr": money(self.stake_inr), "stake_usd": money(self.stake_usd), "inr_per_usd": self.inr_per_usd, "payout_inr": money(self.payout),
            "net_winnings_inr": money(self.net_winnings), "tds_inr": money(self.tds), "take_home_inr": money(self.take_home), "tds_rate_pct": self.tds_rate_pct,
            "stop_loss_pct": self.stop_pct, "stop_loss_floor_inr": money(self.stop_floor), "joint_probability": self.joint_probability, "joint_ev": self.joint_ev,
            "seal": self.seal, "signed_by": self.signed_by, "architect": self.architect, "watermark": self.watermark, "booking_code": self.booking_code,
        }


def issue(report: Mapping[str, Any], seal: str, *, seal_holds: bool, booking_code: str | None, now: datetime, timezone: str, fresh_minutes: int,
          sports: Mapping[str, Sequence[str]]) -> Ticket:
    """The ticket of a signed verdict, or NotIssuable: a broken seal, a verdict that is not an approval, no stake."""
    if not seal_holds:
        raise NotIssuable("SEAL_BROKEN", "This verdict's report no longer matches its seal: no ticket is issued from it.")
    verdict = report.get("verdict") or {}
    status = verdict.get("status")
    if status == VerdictStatus.APPROVED_WITH_CURE.value:
        raise NotIssuable("NOT_APPROVED", "Approved only with the prescription: apply it in the Consultant, and the cured slip's own verdict gets the ticket.")
    if status != VerdictStatus.APPROVED_GOLD.value:
        raise NotIssuable("NOT_APPROVED", "The Consultant rejected this slip to protect the capital: no ticket is issued for it.")
    capital = report.get("consultant") or {}
    stake = _decimal(capital.get("stake_inr")) or Decimal(0)
    if stake <= 0:
        raise NotIssuable("NO_STAKE", "The verdict carries no stake to put on a ticket.")
    zone = ZoneInfo(timezone)
    slip = report.get("slip") or {}
    book = slip.get("book")
    signed_at = datetime.fromisoformat(str(report["signed_at"]))
    age = max(0, int((now - signed_at).total_seconds() // 60))
    legs = tuple(
        TicketLeg(i, str(leg["leg_id"]), sport_label(leg.get("sport_key"), sports), leg.get("league"), str(leg.get("fixture") or ""),
                  _when(leg.get("kickoff"), zone), str(leg.get("label") or ""), search_code(book or "", str(leg.get("market") or ""), str(leg.get("selection") or "")),
                  _decimal(leg.get("price")), leg.get("fair_probability"), leg.get("ev"))
        for i, leg in enumerate(slip.get("legs") or [], 1)
    )
    tax = capital.get("tax") or {}
    stop = capital.get("stop_loss") or {}
    rating = report.get("rating") or {}
    return Ticket(
        ticket_no=TICKET_PREFIX + str(report["report_id"]).replace("-", "")[:10].upper(), report_id=str(report["report_id"]), signed_at=signed_at,
        signed_at_local=_when(str(report["signed_at"]), zone) or str(report["signed_at"]), age_minutes=age, fresh=age <= fresh_minutes,
        book=book, book_label=ASHOKA_BOOKMAKERS.get(book or "", book or "the best book"), badge=BOOK_BADGES.get(book or "", GOLD_BADGE),
        status=str(status), title=str(verdict.get("title") or ""), summary=str(verdict.get("summary") or ""),
        slip_score=float(rating.get("score") or 0.0), tier=str(rating.get("tier") or ""), kind=str(slip.get("kind") or ""), slip_title=slip.get("title"),
        legs=legs, odds=_decimal(slip.get("odds")), stake_inr=stake, stake_usd=_decimal(capital.get("stake_usd")),
        inr_per_usd=(capital.get("fx") or {}).get("inr_per_usd"), payout=_decimal(capital.get("payout_inr")), net_winnings=_decimal(tax.get("net_winnings")),
        tds=_decimal(tax.get("tds")), take_home=_decimal(tax.get("net_post_tax_payout")), tds_rate_pct=tax.get("tds_rate_pct"),
        stop_pct=float(stop.get("pct") or 0.0), stop_floor=_decimal(stop.get("floor_inr")) or Decimal(0),
        joint_probability=slip.get("joint_probability"), joint_ev=slip.get("joint_ev"), seal=seal, signed_by=str(report.get("signed_by") or ""),
        architect=str(report.get("developer_credit") or ""), booking_code=booking_code,
    )


# ================================================================ the text channels
def _kind(ticket: Ticket) -> str:
    return ticket.kind.replace("_", " ").title()


def coupon_text(ticket: Ticket) -> str:
    """The ticket as plain text: copy, Telegram, WhatsApp and email's plain part."""
    usd = f" (${ticket.stake_usd})" if ticket.stake_usd is not None else ""
    lines = [
        f"BETDOC BET-SLIP · {ticket.ticket_no}",
        f"{ticket.book_label} · {_kind(ticket)}" + (f" @ {ticket.odds}" if ticket.odds is not None else ""),
        f"{ticket.title} · Slip Score {ticket.slip_score:.0f}/100 {ticket.tier}",
        f"Signed {ticket.signed_at_local}",
        RULE,
    ]
    for leg in ticket.legs:
        where = " · ".join(part for part in (leg.league, leg.kickoff) if part)
        lines.append(f"{leg.number}) {leg.fixture}" + (f" ({where})" if where else ""))
        lines.append(f"   {leg.market} [{leg.code}]" + (f" @ {leg.price}" if leg.price is not None else ""))
    lines.append(RULE)
    lines.append(f"Stake {inr(ticket.stake_inr)}{usd} (KUMBHA's Kelly stake)")
    if ticket.payout is not None:
        tds = f" · TDS {inr(ticket.tds)}" if ticket.tds is not None else ""
        home = f" · take home {inr(ticket.take_home)}" if ticket.take_home is not None else ""
        lines.append(f"Returns {inr(ticket.payout)}{tds}{home}")
    lines.append(f"Stop-loss: cash out once it is worth under {inr(ticket.stop_floor)}")
    lines.append(f"Booking code: {ticket.booking_code or 'not recorded yet'}")
    if not ticket.fresh:
        lines.append(f"Prices as signed {ticket.age_minutes} min ago: re-check them at {ticket.book_label} before placing.")
    lines.append(f"Seal {ticket.seal[:16]}… (HMAC-SHA256)")
    lines.append(ticket.watermark)
    return "\n".join(lines)


def sms_text(ticket: Ticket) -> str:
    """One compact line for SMS: the legs by the book's own codes, the stake and the floor."""
    head = f"{ticket.ticket_no} {ticket.book_label}" + (f" @{ticket.odds}" if ticket.odds is not None else "") + ": "
    tail = f". Stake {inr(ticket.stake_inr)}, floor {inr(ticket.stop_floor)}. Seal {ticket.seal[:8]}"
    parts = [f"{leg.fixture} {leg.code}" + (f" {leg.price}" if leg.price is not None else "") for leg in ticket.legs]

    def line(shown: int) -> str:
        more = f"; +{len(parts) - shown} more" if shown < len(parts) else ""
        return head + "; ".join(parts[:shown]) + more + tail

    shown = len(parts)
    while shown > 1 and len(line(shown)) > SMS_LIMIT:
        shown -= 1
    return line(shown)[:SMS_LIMIT]


def whatsapp_url(ticket: Ticket) -> str:
    return "https://wa.me/?text=" + urllib.parse.quote(coupon_text(ticket))


def mailto_url(ticket: Ticket) -> str:
    subject = f"BetDoc Bet-Slip {ticket.ticket_no} · {ticket.book_label}"
    return "mailto:?" + urllib.parse.urlencode({"subject": subject, "body": coupon_text(ticket)}, quote_via=urllib.parse.quote)


def sms_url(ticket: Ticket) -> str:
    return "sms:?&body=" + urllib.parse.quote(sms_text(ticket))
