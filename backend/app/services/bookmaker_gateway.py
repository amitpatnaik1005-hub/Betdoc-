"""The bookmaker leg of a two-phase execution: place one order, classify the answer three ways.

* ACCEPTED: the bookmaker holds the bet (we have its ``remote_bet_id``). Commit the reservation.
* REJECTED: the bet definitively does not exist: an error response, a price below the order's
  ``min_acceptable_odds``, an id that cannot be mapped, or a request that never left this machine.
  Roll the reservation back.
* UNKNOWN: the request went out and no trustworthy answer came back (read timeout, connection cut
  mid-response, a 2xx without a bet id). The bet may be live, so its stake stays in exposure until
  the order resolver or a person says otherwise.

``PaperBookmaker`` fills every order in-process (``CFO_EXECUTION_MODE=paper``, the default).
``app.services.sniper.SniperGateway`` routes live orders to each bookmaker's execution venue.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol


class BookmakerOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BookmakerOrder:
    client_ref: str  # the idempotency key: a retried order is the same order at the bookmaker
    bookmaker_id: str
    fixture_id: str
    market: str
    selection: str
    odds: Decimal
    stake_inr: Decimal
    min_acceptable_odds: Decimal  # slippage floor: the bookmaker must reject rather than fill below it
    user_id: uuid.UUID | None = None
    currency: str = "INR"  # the venue account's currency
    stake: Decimal | None = None  # the stake in that currency (None: stake_inr, an INR venue)

    @property
    def venue_stake(self) -> Decimal:
        return self.stake_inr if self.stake is None else self.stake


@dataclass(frozen=True, slots=True)
class BookmakerResult:
    outcome: BookmakerOutcome
    reason: str  # audit reason: BOOKMAKER_ACCEPTED, BOOKMAKER_HTTP_500, UNMAPPED_FIXTURE, ...
    reference: str | None = None  # the bookmaker's remote_bet_id
    http_status: int | None = None
    matched_odds: Decimal | None = None  # the price actually filled, when the venue reports it
    # How much of the stake was matched, in the order's currency (None: all of it). Orders go out
    # immediate-or-cancel, so an unmatched remainder lapses instead of waiting in the book.
    filled_stake: Decimal | None = None
    venue_id: str | None = None
    request_payload: dict[str, Any] | None = None  # exactly what went out (never credentials)
    response_payload: Any = None  # exactly what came back
    latency_ms: int | None = None
    steps: list[str] = field(default_factory=list)


class BookmakerGateway(Protocol):
    async def prepare(self, order: BookmakerOrder) -> Any:
        """Everything that needs the database (routing, id translation), done before the bankroll is
        locked. Returns an opaque route for ``place``, or a REJECTED ``BookmakerResult`` to abort."""
        ...

    async def place(self, order: BookmakerOrder, route: Any = None) -> BookmakerResult: ...


class BookmakerConfigurationError(RuntimeError):
    """Live mode without a usable venue setup: refuse to trade rather than guess."""


class PaperBookmaker:
    """Simulated fills: every order is accepted at the requested price. No money leaves the system."""

    async def prepare(self, order: BookmakerOrder) -> None:
        return None

    async def place(self, order: BookmakerOrder, route: Any = None) -> BookmakerResult:  # noqa: ARG002 - nothing to route
        reference = f"paper_{uuid.uuid5(uuid.NAMESPACE_URL, order.client_ref).hex[:16]}"
        payload = {
            "client_ref": order.client_ref,
            "fixture_id": order.fixture_id,
            "selection": order.selection,
            "odds": str(order.odds),
            "min_acceptable_odds": str(order.min_acceptable_odds),
            "stake": str(order.venue_stake),
            "currency": order.currency,
        }
        return BookmakerResult(
            BookmakerOutcome.ACCEPTED,
            "PAPER_FILL",
            reference,
            200,
            matched_odds=order.odds,
            filled_stake=order.venue_stake,
            venue_id="paper",
            request_payload=payload,
            response_payload={"remote_bet_id": reference, "status": "OPEN", "matched_odds": str(order.odds), "matched_stake": str(order.venue_stake)},
            latency_ms=0,
        )
