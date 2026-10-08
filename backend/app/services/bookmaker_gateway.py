"""The bookmaker leg of a two-phase execution: place one order, classify the answer three ways.

* ACCEPTED: the bookmaker holds the bet (we have its reference). Commit the reservation.
* REJECTED: the bet definitively does not exist: an error response, or a request that never left
  this machine (connection refused, DNS failure, pool exhausted). Roll the reservation back.
* UNKNOWN: the request went out and no trustworthy answer came back (read timeout, connection cut
  mid-response, a 2xx without a bet reference). The bet may be live, so its stake must stay in
  exposure until reconciliation says otherwise; restoring it would let the bankroll be spent twice.

``PaperBookmaker`` fills every order and is the default (``CFO_EXECUTION_MODE=paper``).
``HttpBookmaker`` posts to a configured partner endpoint (``CFO_EXECUTION_MODE=live``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.core.config import Settings

logger = logging.getLogger("betdoc.cfo")


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


@dataclass(frozen=True, slots=True)
class BookmakerResult:
    outcome: BookmakerOutcome
    reason: str  # audit reason: BOOKMAKER_ACCEPTED, BOOKMAKER_HTTP_500, BOOKMAKER_TIMEOUT, ...
    reference: str | None = None
    http_status: int | None = None


class BookmakerGateway(Protocol):
    async def place(self, order: BookmakerOrder) -> BookmakerResult: ...


class BookmakerConfigurationError(RuntimeError):
    """Live mode without a usable endpoint or key: refuse to trade rather than guess."""


class PaperBookmaker:
    """Simulated fills: every order is accepted at the requested price. No money leaves the system."""

    async def place(self, order: BookmakerOrder) -> BookmakerResult:
        reference = f"paper_{uuid.uuid5(uuid.NAMESPACE_URL, order.client_ref).hex[:16]}"
        return BookmakerResult(BookmakerOutcome.ACCEPTED, "PAPER_FILL", reference, 200)


class HttpBookmaker:
    """A partner bookmaker's order endpoint: ``POST {base_url}/bets`` with a bearer key."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + "/bets"
        self._api_key = api_key
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return f"HttpBookmaker(url={self._url!r}, api_key='***')"

    async def place(self, order: BookmakerOrder) -> BookmakerResult:
        body = {
            "client_ref": order.client_ref,
            "bookmaker_id": order.bookmaker_id,
            "fixture_id": order.fixture_id,
            "market": order.market,
            "selection": order.selection,
            "odds": str(order.odds),
            "stake": str(order.stake_inr),
            "currency": "INR",
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Idempotency-Key": order.client_ref}
        try:
            # A hard ceiling on top of httpx's per-phase timeouts: the bankroll row is locked meanwhile
            async with asyncio.timeout(self._timeout + 1.0):
                response = await self._http.post(self._url, json=body, headers=headers, timeout=httpx.Timeout(self._timeout))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol, httpx.InvalidURL):
            return BookmakerResult(BookmakerOutcome.REJECTED, "BOOKMAKER_UNREACHABLE")  # the request never left
        except (httpx.TimeoutException, TimeoutError):
            return BookmakerResult(BookmakerOutcome.UNKNOWN, "BOOKMAKER_TIMEOUT")
        except httpx.TransportError:
            return BookmakerResult(BookmakerOutcome.UNKNOWN, "BOOKMAKER_TRANSPORT_ERROR")

        if not 200 <= response.status_code < 300:
            return BookmakerResult(BookmakerOutcome.REJECTED, f"BOOKMAKER_HTTP_{response.status_code}", http_status=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        reference = None
        if isinstance(payload, dict):
            for key in ("bet_id", "id", "reference", "ref"):
                if isinstance(payload.get(key), str | int) and str(payload[key]).strip():
                    reference = str(payload[key]).strip()[:128]
                    break
        if reference is None:
            # A success code without a bet reference: it may well be placed, so it is not a rejection
            return BookmakerResult(BookmakerOutcome.UNKNOWN, "BOOKMAKER_BAD_RESPONSE", http_status=response.status_code)
        return BookmakerResult(BookmakerOutcome.ACCEPTED, "BOOKMAKER_ACCEPTED", reference, response.status_code)


def build_gateway(settings: Settings, http: httpx.AsyncClient) -> BookmakerGateway:
    if settings.CFO_EXECUTION_MODE == "paper":
        return PaperBookmaker()
    base = settings.CFO_BOOKMAKER_BASE_URL or ""
    if urlparse(base).scheme != "https" or not urlparse(base).hostname:
        raise BookmakerConfigurationError("CFO_EXECUTION_MODE=live needs an https CFO_BOOKMAKER_BASE_URL")
    if settings.CFO_BOOKMAKER_API_KEY is None or not settings.CFO_BOOKMAKER_API_KEY.get_secret_value():
        raise BookmakerConfigurationError("CFO_EXECUTION_MODE=live needs CFO_BOOKMAKER_API_KEY")
    return HttpBookmaker(http, base, settings.CFO_BOOKMAKER_API_KEY.get_secret_value(), settings.CFO_BOOKMAKER_TIMEOUT_SECONDS)
