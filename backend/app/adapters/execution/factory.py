"""Omni-Sniper execution adapters: ``BaseExecutionAdapter`` does everything every venue needs, a
subclass only translates our order into one bookmaker's JSON dialect and back.

Every shot, in order:

1. Session: a bearer token from the ``SessionManager`` (refreshed 5 min before expiry).
2. Outbound rate limit: a Redis token bucket per venue (``bets_per_second``/``burst``, default
   2/s). Simultaneous edges queue micro-sequentially for up to ``SNIPER_RATE_MAX_WAIT_SECONDS``;
   past that the order is refused un-sent rather than earning the account a 429 ban.
3. Fire, with the slippage floor (``min_acceptable_odds``) in the payload: the venue must reject
   rather than fill below it. Strict timeouts on every phase of the request.
4. A ``401`` means the request was refused before it was processed: the session is refreshed
   (``stale=`` the refused token) and the same order, with the same client reference, re-fires once.
5. Classification (see ``bookmaker_gateway``): ACCEPTED needs the venue's ``remote_bet_id``;
   anything ambiguous is UNKNOWN, never a rejection.

What went out and what came back are kept verbatim (minus credentials) for the payload inspector.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import httpx

from app.adapters.execution.venue import VenueConfig
from app.adapters.ingestion.base import ThrottledError
from app.core.config import Settings
from app.services.bookmaker_gateway import BookmakerOrder, BookmakerOutcome, BookmakerResult
from app.services.id_mapper import RemoteIds, VenueEvent
from app.services.omni_throttle import RateLimit, TokenBucket
from app.services.session_manager import SessionError, SessionManager

Emit = Callable[[str, str], Awaitable[None]]  # (step, message) -> the execution terminal
_MAX_BODY_CHARS = 4000


class RemoteStatus(StrEnum):
    OPEN = "OPEN"  # accepted, not settled
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    REJECTED = "REJECTED"  # the venue never struck it (or cancelled it): the stake comes back
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True, slots=True)
class RemoteBet:
    status: RemoteStatus
    remote_bet_id: str | None = None
    client_ref: str | None = None
    matched_odds: Decimal | None = None


class VenueUnavailableError(RuntimeError):
    """The venue could not be asked (network, auth, 5xx): the resolver backs off and retries."""


def _decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number > 1 else None


def _amount(value: object) -> Decimal | None:
    """A stake amount (zero allowed); anything unparseable is treated as not reported."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:_MAX_BODY_CHARS]


async def _quiet(_: str, __: str) -> None:
    return None


class BaseExecutionAdapter(ABC):
    def __init__(self, venue: VenueConfig, *, http: httpx.AsyncClient, sessions: SessionManager, limiter: TokenBucket, settings: Settings) -> None:
        self.venue = venue
        self.http = http
        self.sessions = sessions
        self.limiter = limiter
        self.settings = settings
        self.rate = RateLimit(requests_per_minute=float(venue.bets_per_second) * 60.0, burst=venue.burst)

    # -------------------------------------------------------------- dialect hooks
    @abstractmethod
    def place_payload(self, order: BookmakerOrder, remote: RemoteIds) -> dict[str, Any]: ...

    @abstractmethod
    def parse_receipt(self, body: Any) -> tuple[str | None, Decimal | None, Decimal | None]:
        """(remote_bet_id, matched_odds, matched_stake) from a 2xx answer; a missing id makes the
        outcome UNKNOWN, a missing matched stake means the whole stake was matched."""

    @abstractmethod
    def status_params(self, remote_ids: Sequence[str], client_refs: Sequence[str]) -> dict[str, str]: ...

    @abstractmethod
    def parse_statuses(self, body: Any) -> list[RemoteBet]: ...

    def parse_events(self, body: Any) -> list[VenueEvent]:
        return []

    # -------------------------------------------------------------- the shot
    async def place(self, order: BookmakerOrder, remote: RemoteIds, emit: Emit = _quiet) -> BookmakerResult:
        payload = self.place_payload(order, remote)
        await emit("auth", f"Authenticating with {self.venue.display_name}…")
        try:
            token = await self.sessions.bearer(self.venue)
        except SessionError as exc:
            return self._result(BookmakerOutcome.REJECTED, exc.reason, payload)

        started = time.perf_counter()
        response, failure = await self._fire(order, payload, token, emit)
        if response is not None and response.status_code == 401:
            await emit("auth", "401 Unauthorized: refreshing the session and re-firing…")
            try:
                token = await self.sessions.bearer(self.venue, stale=token)
            except SessionError as exc:
                return self._result(BookmakerOutcome.REJECTED, exc.reason, payload, response=response)
            response, failure = await self._fire(order, payload, token, emit)
            if response is not None and response.status_code == 401:
                return self._result(BookmakerOutcome.REJECTED, "AUTH_FAILED", payload, response=response)
        del token
        latency = int((time.perf_counter() - started) * 1000)
        if failure is not None:
            return self._result(failure[0], failure[1], payload, latency=latency)
        assert response is not None

        body = _body(response)
        if not 200 <= response.status_code < 300:
            reason = f"BOOKMAKER_HTTP_{response.status_code}"
            if isinstance(body, Mapping) and str(body.get("error", "")).upper() in ("PRICE_BELOW_MINIMUM", "SLIPPAGE", "ODDS_CHANGED"):
                reason = "SLIPPAGE_REJECTED"  # the price moved below min_acceptable_odds in flight
            return self._result(BookmakerOutcome.REJECTED, reason, payload, response=response, body=body, latency=latency)
        remote_bet_id, matched, filled = self.parse_receipt(body)
        if not remote_bet_id:
            # A success code without a bet id: it may well be placed, so it is not a rejection
            return self._result(BookmakerOutcome.UNKNOWN, "BOOKMAKER_BAD_RESPONSE", payload, response=response, body=body, latency=latency)
        requested = order.venue_stake
        if filled is not None and filled > requested:
            # More matched than asked for: the bet exists, but not as reserved. Keep it in exposure for a person
            return self._result(BookmakerOutcome.UNKNOWN, "FILL_EXCEEDS_REQUEST", payload, response=response, body=body, latency=latency)
        if filled is not None and filled <= 0:
            # Immediate-or-cancel with nothing matched: the order lapsed, no bet exists
            return self._result(BookmakerOutcome.REJECTED, "NOT_MATCHED", payload, response=response, body=body, latency=latency)
        reason = "BOOKMAKER_ACCEPTED" if filled is None or filled == requested else "PARTIAL_FILL"
        if matched is not None and matched < order.min_acceptable_odds:
            reason = "SLIPPAGE_VIOLATION"  # the venue filled below our floor: the bet exists, flag it
        return BookmakerResult(
            BookmakerOutcome.ACCEPTED,
            reason,
            remote_bet_id,
            response.status_code,
            matched_odds=matched,
            filled_stake=requested if filled is None else filled,
            venue_id=self.venue.id,
            request_payload=payload,
            response_payload=body,
            latency_ms=latency,
        )

    async def _fire(
        self, order: BookmakerOrder, payload: dict[str, Any], token: str, emit: Emit
    ) -> tuple[httpx.Response | None, tuple[BookmakerOutcome, str] | None]:
        try:
            waited = await self.limiter.acquire(self.venue.id, self.rate)
        except ThrottledError:
            return None, (BookmakerOutcome.REJECTED, "OUTBOUND_THROTTLED")  # never sent
        if waited > 0:
            await emit("rate", f"Queued {waited * 1000:.0f} ms behind the {self.venue.bets_per_second}/s outbound limit")
        await emit("fire", f"Firing {order.selection} @ {order.odds} (min {order.min_acceptable_odds}) for {order.stake_inr}…")
        timeout = httpx.Timeout(self.settings.CFO_BOOKMAKER_TIMEOUT_SECONDS, connect=min(3.0, self.settings.CFO_BOOKMAKER_TIMEOUT_SECONDS))
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": order.client_ref}
        try:
            response = await self.http.post(self.venue.url(self.venue.place_path), json=payload, headers=headers, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol, httpx.InvalidURL):
            return None, (BookmakerOutcome.REJECTED, "BOOKMAKER_UNREACHABLE")  # the request never left
        except httpx.TimeoutException:
            return None, (BookmakerOutcome.UNKNOWN, "BOOKMAKER_TIMEOUT")
        except httpx.TransportError:
            return None, (BookmakerOutcome.UNKNOWN, "BOOKMAKER_TRANSPORT_ERROR")
        return response, None

    def _result(
        self,
        outcome: BookmakerOutcome,
        reason: str,
        payload: dict[str, Any],
        *,
        response: httpx.Response | None = None,
        body: Any = None,
        latency: int | None = None,
    ) -> BookmakerResult:
        if response is not None and body is None:
            body = _body(response)
        return BookmakerResult(
            outcome,
            reason,
            http_status=response.status_code if response is not None else None,
            venue_id=self.venue.id,
            request_payload=payload,
            response_payload=body,
            latency_ms=latency,
        )

    # -------------------------------------------------------------- the resolver's view
    async def fetch_statuses(self, remote_ids: Sequence[str], client_refs: Sequence[str]) -> list[RemoteBet]:
        """The venue's "my bets" answer for these orders. Raises ``VenueUnavailableError`` when it can't be asked."""
        if not remote_ids and not client_refs:
            return []
        body = await self._get(self.venue.status_path, self.status_params(remote_ids, client_refs))
        return self.parse_statuses(body)

    async def fetch_events(self) -> list[VenueEvent]:
        if not self.venue.events_path:
            return []
        return self.parse_events(await self._get(self.venue.events_path, {}))

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        try:
            token = await self.sessions.bearer(self.venue)
        except SessionError as exc:
            raise VenueUnavailableError(exc.reason) from exc
        for attempt in range(2):
            try:
                await self.limiter.acquire(self.venue.id, self.rate)
                response = await self.http.get(
                    self.venue.url(path),
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(self.settings.CFO_BOOKMAKER_TIMEOUT_SECONDS),
                )
            except (httpx.HTTPError, ThrottledError) as exc:
                raise VenueUnavailableError(type(exc).__name__) from exc
            if response.status_code == 401 and attempt == 0:
                try:
                    token = await self.sessions.bearer(self.venue, stale=token)
                except SessionError as exc:
                    raise VenueUnavailableError(exc.reason) from exc
                continue
            if response.status_code != 200:
                raise VenueUnavailableError(f"HTTP_{response.status_code}")
            return _body(response)
        raise VenueUnavailableError("AUTH_FAILED")


class GenericJsonExecutionAdapter(BaseExecutionAdapter):
    """BetDoc's canonical order API (the sandbox speaks it; so can any partner that adopts it).

    Place   POST {place_path}  {client_ref, event_id, selection_id, odds, min_acceptable_odds, stake, currency,
                                time_in_force: "IOC"}
            -> {remote_bet_id, status, matched_odds, matched_stake}  (matched_stake < stake: a partial fill,
               the remainder lapsed; absent: matched in full)
    Status  GET  {status_path}?ids=..&client_refs=.. -> {bets: [{remote_bet_id, client_ref, status, matched_odds}]}
    Events  GET  {events_path} -> {events: [{id, sport_key, home, away, commence_time, outcomes: {HOME: id, ...}}]}
    """

    def place_payload(self, order: BookmakerOrder, remote: RemoteIds) -> dict[str, Any]:
        return {
            "client_ref": order.client_ref,
            "event_id": remote.event_id,
            "selection_id": remote.selection_id,
            "odds": str(order.odds),
            "min_acceptable_odds": str(order.min_acceptable_odds),
            "stake": str(order.venue_stake),
            "currency": order.currency,
            "time_in_force": "IOC",  # match what you can now, cancel the rest: nothing sits unmatched
        }

    def parse_receipt(self, body: Any) -> tuple[str | None, Decimal | None, Decimal | None]:
        if not isinstance(body, Mapping):
            return None, None, None
        remote = body.get("remote_bet_id") or body.get("bet_id") or body.get("id")
        remote_id = str(remote).strip()[:128] if isinstance(remote, str | int) and str(remote).strip() else None
        filled = next((_amount(body[k]) for k in ("matched_stake", "filled_stake", "size_matched") if body.get(k) is not None), None)
        return remote_id, _decimal(body.get("matched_odds")), filled

    def status_params(self, remote_ids: Sequence[str], client_refs: Sequence[str]) -> dict[str, str]:
        params: dict[str, str] = {}
        if remote_ids:
            params["ids"] = ",".join(remote_ids)
        if client_refs:
            params["client_refs"] = ",".join(client_refs)
        return params

    def parse_statuses(self, body: Any) -> list[RemoteBet]:
        rows = body.get("bets") if isinstance(body, Mapping) else None
        out: list[RemoteBet] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            try:
                status = RemoteStatus(str(row.get("status", "")).upper())
            except ValueError:
                continue  # an unknown status is not evidence of anything: leave the order pending
            remote = row.get("remote_bet_id")
            ref = row.get("client_ref")
            out.append(RemoteBet(status, str(remote) if remote else None, str(ref) if ref else None, _decimal(row.get("matched_odds"))))
        return out

    def parse_events(self, body: Any) -> list[VenueEvent]:
        rows = body.get("events") if isinstance(body, Mapping) else None
        events: list[VenueEvent] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping) or not row.get("id"):
                continue
            outcomes = row.get("outcomes") if isinstance(row.get("outcomes"), Mapping) else {}
            events.append(
                VenueEvent(
                    event_id=str(row["id"]),
                    sport_key=str(row.get("sport_key", "")),
                    home=str(row.get("home", "")),
                    away=str(row.get("away", "")),
                    commence_time=str(row.get("commence_time", "")),
                    outcomes={str(k): str(v) for k, v in outcomes.items()},
                )
            )
        return events


ADAPTERS: dict[str, type[BaseExecutionAdapter]] = {"generic_json": GenericJsonExecutionAdapter}


def build_adapter(venue: VenueConfig, *, http: httpx.AsyncClient, sessions: SessionManager, limiter: TokenBucket, settings: Settings) -> BaseExecutionAdapter:
    try:
        adapter = ADAPTERS[venue.adapter]
    except KeyError as exc:
        raise ValueError(f"Unknown execution adapter '{venue.adapter}'") from exc
    return adapter(venue, http=http, sessions=sessions, limiter=limiter, settings=settings)
