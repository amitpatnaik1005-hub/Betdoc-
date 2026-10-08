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


@dataclass(frozen=True, slots=True)
class BookmakerResult:
    outcome: BookmakerOutcome
    reason: str  # audit reason: BOOKMAKER_ACCEPTED, BOOKMAKER_HTTP_500, UNMAPPED_FIXTURE, ...
    reference: str | None = None  # the bookmaker's remote_bet_id
    http_status: int | None = None
    matched_odds: Decimal | None = None  # the price actually filled, when the venue reports it
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
            "stake": str(order.stake_inr),
        }
        return BookmakerResult(
            BookmakerOutcome.ACCEPTED,
            "PAPER_FILL",
            reference,
            200,
            matched_odds=order.odds,
            venue_id="paper",
            request_payload=payload,
            response_payload={"remote_bet_id": reference, "status": "OPEN", "matched_odds": str(order.odds)},
            latency_ms=0,
        )
