"""Cashout adapters for the in-play stop-loss shield (Group 77).

When the shield fires, the adapter for the bet's bookmaker issues a ``CashoutTicket``: what to cash out, the
value the shield saw, the stop-loss floor, and the steps to take. Parimatch, 1xBet and Stake offer no cashout
API, and BetDoc does not log in to a bookmaker's site or drive it (the standing rule for these books: no
scraping, no automation of their pages), so every adapter here is MANUAL: the ticket goes to the user's phone
through the Sentinel, the user takes the cashout at the book, and the amount actually received is recorded
on the bet (``record``), which settles it as CASHED_OUT in Ashoka's ledger. The ticket says so in plain words:
the shield cannot promise an amount, and a book may suspend cashout exactly when the game swings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_bets_ledger import UserPlacedBet
from app.services import user_pnl_tracker as tracker


class CashoutMode(StrEnum):
    MANUAL = "MANUAL"  # the user takes it at the bookmaker; BetDoc records what was received


@dataclass(frozen=True, slots=True)
class CashoutTicket:
    bookmaker: str
    label: str
    mode: CashoutMode
    bet_id: str
    booking_code: str | None
    stake_inr: Decimal
    floor_inr: Decimal
    value_inr: Decimal
    value_source: str  # offer | fair_value
    reason: str
    instructions: list[str]
    issued_at: datetime
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"bookmaker": self.bookmaker, "label": self.label, "mode": self.mode.value, "bet_id": self.bet_id, "booking_code": self.booking_code,
                "stake_inr": str(self.stake_inr), "floor_inr": str(self.floor_inr), "value_inr": str(self.value_inr), "value_source": self.value_source,
                "reason": self.reason, "instructions": list(self.instructions), "caveats": list(self.caveats), "issued_at": self.issued_at.isoformat()}

    def text(self) -> str:
        return "\n".join([self.reason, *[f"{i}. {step}" for i, step in enumerate(self.instructions, 1)], *self.caveats])


CAVEATS = [
    "The amount is the book's to offer: it may be lower than shown, or suspended for a moment after a goal or a red card. Take it as soon as it reopens.",
    "BetDoc does not log in to the bookmaker or place the cashout for you.",
]


class CashoutAdapter:
    """A bookmaker's cashout path. Subclasses name the book and say where its bets are found."""

    bookmaker = "OTHER"
    label = "your bookmaker"
    mode = CashoutMode.MANUAL

    def locate(self, bet: UserPlacedBet) -> str:
        """Where the bet is found at the book."""
        if bet.booking_code:
            return f"In your {self.label} account, open your bet history and find the bet with booking code {bet.booking_code}."
        return f"In your {self.label} account, open your open bets and find the ₹{bet.stake_inr:,} bet placed {bet.placed_at:%d %b %H:%M} UTC."

    def ticket(self, bet: UserPlacedBet, *, floor: Decimal, value: Decimal, value_source: str, reason: str, now: datetime) -> CashoutTicket:
        seen = f"₹{value:,} ({'the offer you last read' if value_source == 'offer' else 'fair value from the live market'})"
        return CashoutTicket(
            bookmaker=self.bookmaker, label=self.label, mode=self.mode, bet_id=str(bet.id), booking_code=bet.booking_code, stake_inr=bet.stake_inr,
            floor_inr=floor, value_inr=value, value_source=value_source, reason=reason,
            instructions=[self.locate(bet), f"Take the cashout now: the shield saw {seen} against a floor of ₹{floor:,}.",
                          "Record the amount you received in BetDoc (the shield's emergency cashout), so the bet settles at what you actually got."],
            issued_at=now, caveats=list(CAVEATS),
        )

    async def record(self, session: AsyncSession, bet: UserPlacedBet, amount: Decimal, now: datetime) -> UserPlacedBet:
        """The cashout the user took: the bet settles CASHED_OUT at that amount (one ledger, one P&L)."""
        if amount < 0:
            raise ValueError("a cashout amount cannot be negative")
        return await tracker.record_cashout(session, bet, amount, now)


_REGISTRY: dict[str, CashoutAdapter] = {}


def register(adapter: CashoutAdapter) -> CashoutAdapter:
    _REGISTRY[adapter.bookmaker] = adapter
    return adapter


def adapter_for(bookmaker: str) -> CashoutAdapter:
    """The adapter for a ledger bookmaker (``PlacedBookmaker`` value); the generic one for any other."""
    from app.adapters.bookmakers import one_xbet_adapter, parimatch_adapter, stake_adapter  # noqa: F401, PLC0415 - registers them

    return _REGISTRY.get(bookmaker, GENERIC)


GENERIC = CashoutAdapter()
