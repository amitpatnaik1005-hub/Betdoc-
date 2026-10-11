"""ASHVATTHAMA, the execution router (Commander #17, Group 80). Developed for Amit Ashok Kumar Patnaik.

Every slip goes to one venue, Parimatch by default (``https://parimatchs1.com/en/all-live``, the primary capital
account, rupees), else one of the OmniRoute venues (Pinnacle, Betfair, Stake, 1xBet; the exchange takes singles only).

* **Paper mode** simulates the exact execution: each leg's live price on the target book is snapped from the
  normalised quote store (Aryabhata's per-market book hashes, where Parimatch's injected prices land); a book
  quoting the market suspended refuses the slip, a price more than ``PAPER_SLIPPAGE_TOLERANCE_PCT`` under the
  request refuses it (``PRICE_MOVED``), anything else fills at the live price, and the receipt logs the slippage
  against the request, the quote's age, the router's own latency and the venue round trip it models
  (``PAPER_SIM_LATENCY_MS``). A leg the book does not quote fills at the requested price, flagged unverified.
* **Live mode** never places anything: no bookmaker is automated (Parimatch publishes no betting API). The router
  issues the placement ticket (venue, URL, stake in rupees, the price to accept at least) for the trader to place
  by hand and record in the ledger.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.domain.bookmakers.adapters import canonical_bookmaker
from app.domain.sports.twin_bridge import EXCHANGES, OMNI_VENUES, PRIMARY_CAPITAL_VENUE
from app.services.aryabhata_pipeline import read_market_books

COMMANDER = "ASHVATTHAMA"
FILLED, REJECTED, TICKET = "FILLED", "REJECTED", "TICKET"
ODDS_STEP = Decimal("0.0001")


def venue_url(settings: Settings, venue: str) -> str | None:
    return {"parimatch": settings.PARIMATCH_TARGET_URL, "1xbet": settings.ONE_XBET_TARGET_URL, "stake": settings.STAKE_TARGET_URL,
            "betfair": "https://www.betfair.com/exchange/plus/", "pinnacle": "https://www.pinnacle.com/"}.get(venue)


@dataclass(frozen=True, slots=True)
class ExecLeg:
    match_id: str
    requested_odds: Decimal
    quote_market: str | None = None  # the platform market key the books quote ("Match Odds", "Totals 2.5")
    quote_selection: str | None = None  # its selection code ("HOME", "OVER")


@dataclass
class ExecutionReceipt:
    mode: str  # PAPER | LIVE
    status: str  # FILLED | REJECTED | TICKET
    venue: str
    venue_url: str | None
    requested_odds: Decimal
    executed_odds: Decimal | None
    live_odds: Decimal | None
    slippage_pct: float | None
    price_source: str
    quote_age_seconds: float | None
    router_latency_ms: float
    venue_latency_ms: float
    slip_ref: str
    reason: str | None = None
    legs: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"commander": COMMANDER, "mode": self.mode, "status": self.status, "venue": self.venue, "venue_url": self.venue_url,
                "requested_odds": str(self.requested_odds), "executed_odds": None if self.executed_odds is None else str(self.executed_odds),
                "live_odds": None if self.live_odds is None else str(self.live_odds), "slippage_pct": self.slippage_pct, "price_source": self.price_source,
                "quote_age_seconds": self.quote_age_seconds, "router_latency_ms": round(self.router_latency_ms, 3),
                "venue_latency_ms": round(self.venue_latency_ms, 3), "total_latency_ms": round(self.router_latency_ms + self.venue_latency_ms, 3),
                "slip_ref": self.slip_ref, "reason": self.reason, "legs": self.legs}


class ExecutionRefused(ValueError):
    def __init__(self, receipt: ExecutionReceipt) -> None:
        super().__init__(receipt.reason or "refused")
        self.receipt = receipt


def resolve_venue(target: str | None) -> str:
    venue = canonical_bookmaker(target) if target else PRIMARY_CAPITAL_VENUE
    if venue not in OMNI_VENUES:
        raise ValueError(f"{target!r} is not an OmniRoute venue ({', '.join(OMNI_VENUES)})")
    return venue


async def _snap(redis: Redis | None, settings: Settings, venue: str, legs: Sequence[ExecLeg], now: datetime) -> list[dict[str, Any]]:
    """Each leg's live price on ``venue`` (None when the book does not quote it fresh)."""
    keyed = [f"{leg.match_id}|{leg.quote_market}" if leg.quote_market and leg.quote_selection else None for leg in legs]
    books: dict[str, Any] = {}
    if redis is not None and any(keyed):
        try:
            books = await read_market_books(redis, settings, [k for k in keyed if k])
        except (RedisError, OSError, TimeoutError):
            books = {}
    out = []
    for leg, key in zip(legs, keyed, strict=True):
        row: dict[str, Any] = {"match_id": leg.match_id, "quote_market": leg.quote_market, "quote_selection": leg.quote_selection,
                               "requested_odds": str(leg.requested_odds), "live_odds": None, "suspended": False, "quote_age_seconds": None}
        if key:
            for line in books.get(key, ()):
                if canonical_bookmaker(line.bookmaker_id) != venue:
                    continue
                age = (now - line.seen_at).total_seconds()
                price = line.prices.get(str(leg.quote_selection))
                if age > settings.PAPER_QUOTE_MAX_AGE_SECONDS or price is None:
                    continue
                if row["quote_age_seconds"] is None or age < row["quote_age_seconds"]:
                    row.update({"live_odds": Decimal(price), "suspended": bool(line.is_suspended), "quote_age_seconds": round(age, 3)})
        out.append(row)
    return out


async def execute_paper(redis: Redis | None, settings: Settings, *, target: str | None, legs: Sequence[ExecLeg], stake_inr: Decimal,
                        now: datetime, multiple: bool) -> ExecutionReceipt:
    """Simulate the slip on the venue. Raises ``ExecutionRefused`` (with its receipt) when the venue would refuse it."""
    started = time.perf_counter()
    venue = resolve_venue(target)
    requested = Decimal(1)
    for leg in legs:
        requested *= leg.requested_odds
    requested = requested.quantize(ODDS_STEP, rounding=ROUND_DOWN)
    receipt = ExecutionReceipt("PAPER", FILLED, venue, venue_url(settings, venue), requested, None, None, None, "requested_unverified", None, 0.0,
                               float(settings.PAPER_SIM_LATENCY_MS), f"PAPER-{venue.upper()}-{uuid.uuid4().hex[:12].upper()}")
    if multiple and venue in EXCHANGES:
        receipt.status, receipt.reason = REJECTED, f"{venue} is an exchange: it takes singles only"
    snapped = await _snap(redis, settings, venue, legs, now)
    receipt.legs = [{**row, "live_odds": None if row["live_odds"] is None else str(row["live_odds"])} for row in snapped]
    if receipt.status == FILLED and any(row["suspended"] for row in snapped):
        receipt.status, receipt.reason = REJECTED, f"MARKET_SUSPENDED: {venue} has the market suspended"
    if receipt.status == FILLED and all(row["live_odds"] is not None for row in snapped):
        live = Decimal(1)
        for row in snapped:
            live *= row["live_odds"]
        live = live.quantize(ODDS_STEP, rounding=ROUND_DOWN)
        receipt.live_odds = live
        receipt.price_source = f"{venue}_live"
        receipt.quote_age_seconds = max(row["quote_age_seconds"] for row in snapped)
        floor = requested * (Decimal(1) - Decimal(str(settings.PAPER_SLIPPAGE_TOLERANCE_PCT)) / Decimal(100))
        if live < floor:
            receipt.status, receipt.reason = REJECTED, f"PRICE_MOVED: {venue} now offers {live} against the {requested} requested"
        else:
            receipt.executed_odds = live
    elif receipt.status == FILLED:
        receipt.executed_odds = requested
        if any(row["live_odds"] is not None for row in snapped):
            receipt.price_source = "partially_verified"
    if receipt.executed_odds is not None:
        receipt.slippage_pct = round(float((requested - receipt.executed_odds) / requested * 100), 4)
    receipt.router_latency_ms = (time.perf_counter() - started) * 1000.0
    if receipt.status == REJECTED:
        raise ExecutionRefused(receipt)
    return receipt


def live_ticket(settings: Settings, *, target: str | None, odds: Decimal, stake_inr: Decimal, description: str) -> ExecutionReceipt:
    """The placement ticket for a real-money slip: nothing is placed automatically."""
    venue = resolve_venue(target)
    receipt = ExecutionReceipt("LIVE", TICKET, venue, venue_url(settings, venue), odds, None, None, None, "trader_places_by_hand", None, 0.0, 0.0,
                               f"LIVE-{venue.upper()}-{uuid.uuid4().hex[:12].upper()}",
                               f"Place {description} on {venue} at {odds} or better for ₹{stake_inr:,.2f}, then record it in the ledger.")
    return receipt
