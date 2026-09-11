
"""Inbound bookmaker settlement callback handling.



Signatures are verified with :func:`hmac.compare_digest` before any payload

parsing so that a forged body is rejected in constant time and never reaches

downstream subscribers. Events are deduplicated by identifier because

bookmakers retry webhooks aggressively.

"""



from __future__ import annotations



import asyncio

import hashlib

import hmac

import inspect

import json

import logging

from collections import OrderedDict

from dataclasses import dataclass, field

from datetime import datetime, timezone

from decimal import Decimal, InvalidOperation

from enum import Enum

from typing import Any, Awaitable, Callable, Final, Mapping



__all__ = [

    "CallbackHandler",

    "SettlementError",

    "SettlementEvent",

    "SettlementOutcome",

]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



MONEY_EXPONENT: Final[Decimal] = Decimal("0.01")

DEFAULT_DEDUPE_CAPACITY: Final[int] = 10_000



SettlementCallback = Callable[["SettlementEvent"], Awaitable[None] | None]





class SettlementError(RuntimeError):

    """Raised when a settlement callback cannot be trusted or parsed.



    Kept local to the reconciliation domain so that callers are not forced to

    catch an infrastructure configuration exception for a domain-level event.

    """



    __slots__ = ("bookmaker", "code")



    def __init__(

        self,

        message: str,

        *,

        code: str = "SETTLEMENT_ERROR",

        bookmaker: str | None = None,

    ) -> None:

        super().__init__(message)

        self.code: str = code

        self.bookmaker: str | None = bookmaker



    def __str__(self) -> str:

        prefix = f"{self.bookmaker}/" if self.bookmaker else ""

        return f"[{prefix}{self.code}] {super().__str__()}"





class SettlementOutcome(str, Enum):

    """Normalised settlement result for a single bet."""



    WON = "WON"

    LOST = "LOST"

    HALF_WON = "HALF_WON"

    HALF_LOST = "HALF_LOST"

    VOID = "VOID"

    PUSH = "PUSH"

    CASHED_OUT = "CASHED_OUT"

    CANCELLED = "CANCELLED"

    UNKNOWN = "UNKNOWN"



    @property

    def is_stake_returned(self) -> bool:

        """Whether the full stake is returned with no P&L impact."""

        return self in {

            SettlementOutcome.VOID,

            SettlementOutcome.PUSH,

            SettlementOutcome.CANCELLED,

        }



    @property

    def is_settled(self) -> bool:

        """Whether the bet has reached a final financial state."""

        return self is not SettlementOutcome.UNKNOWN





_OUTCOME_ALIASES: Final[dict[str, SettlementOutcome]] = {

    "WON": SettlementOutcome.WON,

    "WIN": SettlementOutcome.WON,

    "WINNER": SettlementOutcome.WON,

    "LOST": SettlementOutcome.LOST,

    "LOSE": SettlementOutcome.LOST,

    "LOSER": SettlementOutcome.LOST,

    "HALF_WON": SettlementOutcome.HALF_WON,

    "HALF_WIN": SettlementOutcome.HALF_WON,

    "HALF_LOST": SettlementOutcome.HALF_LOST,

    "HALF_LOSE": SettlementOutcome.HALF_LOST,

    "VOID": SettlementOutcome.VOID,

    "VOIDED": SettlementOutcome.VOID,

    "REMOVED": SettlementOutcome.VOID,

    "PUSH": SettlementOutcome.PUSH,

    "DRAW_NO_BET": SettlementOutcome.PUSH,

    "CASHED_OUT": SettlementOutcome.CASHED_OUT,

    "CASHOUT": SettlementOutcome.CASHED_OUT,

    "CANCELLED": SettlementOutcome.CANCELLED,

    "CANCELED": SettlementOutcome.CANCELLED,

    "LAPSED": SettlementOutcome.CANCELLED,

}





@dataclass(frozen=True, slots=True)

class SettlementEvent:

    """A single settled bet as reported by a bookmaker."""



    event_id: str

    bookmaker: str

    bet_id: str

    outcome: SettlementOutcome

    stake: Decimal = Decimal("0.00")

    payout: Decimal = Decimal("0.00")

    market_id: str | None = None

    selection_id: int | None = None

    currency: str = "INR"

    settled_at: datetime = field(

        default_factory=lambda: datetime.now(tz=timezone.utc)

    )

    received_at: datetime = field(

        default_factory=lambda: datetime.now(tz=timezone.utc)

    )

    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)



    @property

    def profit(self) -> Decimal:

        """Net P&L: payout minus stake, rounded to paise precision."""

        return (self.payout - self.stake).quantize(MONEY_EXPONENT)



    @property

    def is_profitable(self) -> bool:

        """Whether this settlement produced a positive net return."""

        return self.profit > 0



    def to_dict(self) -> dict[str, Any]:

        """Return a JSON-safe projection for audit logging."""

        return {

            "event_id": self.event_id,

            "bookmaker": self.bookmaker,

            "bet_id": self.bet_id,

            "outcome": self.outcome.value,

            "stake": str(self.stake),

            "payout": str(self.payout),

            "profit": str(self.profit),

            "market_id": self.market_id,

            "selection_id": self.selection_id,

            "currency": self.currency,

            "settled_at": self.settled_at.isoformat(),

            "received_at": self.received_at.isoformat(),

        }





class CallbackHandler:

    """Verify, parse, deduplicate and fan out bookmaker settlement callbacks."""



    __slots__ = ("_bookmaker", "_callbacks", "_dedupe_capacity", "_lock", "_secret", "_seen")



    def __init__(

        self,

        secret: str | bytes,

        *,

        bookmaker: str = "betfair",

        dedupe_capacity: int = DEFAULT_DEDUPE_CAPACITY,

    ) -> None:

        if not secret:

            raise SettlementError(

                "webhook signing secret must not be empty", code="MALFORMED"

            )

        self._secret: bytes = (

            secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)

        )

        self._bookmaker: str = bookmaker

        self._dedupe_capacity: int = max(1, int(dedupe_capacity))

        self._callbacks: list[SettlementCallback] = []

        self._seen: "OrderedDict[str, None]" = OrderedDict()

        self._lock: asyncio.Lock = asyncio.Lock()



    @property

    def bookmaker(self) -> str:

        """Bookmaker this handler is configured for."""

        return self._bookmaker



    @property

    def subscriber_count(self) -> int:

        """Number of registered subscribers."""

        return len(self._callbacks)



    def register(self, callback: SettlementCallback) -> None:

        """Subscribe ``callback`` to settlement events.



        Sync and async callables are both accepted; registration is idempotent.

        """

        if not callable(callback):

            raise SettlementError("callback must be callable", code="MALFORMED")

        if callback not in self._callbacks:

            self._callbacks.append(callback)



    def unregister(self, callback: SettlementCallback) -> bool:

        """Remove ``callback``. Returns whether it was registered."""

        try:

            self._callbacks.remove(callback)

        except ValueError:

            return False

        return True



    def compute_signature(self, payload: bytes | str) -> str:

        """Return the expected hex HMAC-SHA256 signature for ``payload``."""

        body = payload.encode("utf-8") if isinstance(payload, str) else payload

        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()



    def verify_signature(self, payload: bytes | str, signature: str) -> bool:

        """Constant-time comparison of a provided signature against ``payload``.



        Accepts bare hex digests and the ``sha256=<hex>`` prefixed form.

        """

        if not signature:

            return False

        candidate = signature.strip()

        if "=" in candidate:

            algorithm, _, remainder = candidate.partition("=")

            if algorithm.strip().lower() not in ("sha256", "hmac-sha256"):

                return False

            candidate = remainder.strip()

        expected = self.compute_signature(payload)

        return hmac.compare_digest(expected, candidate.lower())



    async def handle_betfair_callback(

        self, payload: bytes | str, signature: str

    ) -> SettlementEvent:

        """Authenticate and process one Betfair settlement callback.



        Raises

        ------

        SettlementError

            ``INVALID_SIGNATURE`` when the HMAC does not match, ``MALFORMED``

            when the body is not a usable settlement document, or

            ``DUPLICATE`` when the event has already been processed.

        """

        if not self.verify_signature(payload, signature):

            _LOG.warning(

                "rejected %s settlement callback: signature mismatch", self._bookmaker

            )

            raise SettlementError(

                "settlement callback signature verification failed",

                code="INVALID_SIGNATURE",

                bookmaker=self._bookmaker,

            )



        event = self._parse_betfair_payload(payload)



        async with self._lock:

            if event.event_id in self._seen:

                raise SettlementError(

                    f"settlement event {event.event_id} has already been processed",

                    code="DUPLICATE",

                    bookmaker=self._bookmaker,

                )

            self._seen[event.event_id] = None

            while len(self._seen) > self._dedupe_capacity:

                self._seen.popitem(last=False)



        await self.dispatch(event)

        return event



    async def dispatch(self, event: SettlementEvent) -> None:

        """Invoke every subscriber concurrently, isolating their failures.



        A subscriber raising does not prevent the others from running; each

        failure is logged with the offending callback name.

        """

        if not self._callbacks:

            _LOG.debug("no settlement subscribers registered; dropping %s", event.event_id)

            return



        subscribers = tuple(self._callbacks)

        results = await asyncio.gather(

            *(self._invoke(callback, event) for callback in subscribers),

            return_exceptions=True,

        )

        for callback, result in zip(subscribers, results):

            if isinstance(result, BaseException):

                _LOG.error(

                    "settlement subscriber %s failed for event %s: %s",

                    getattr(callback, "__qualname__", repr(callback)),

                    event.event_id,

                    result,

                    exc_info=result,

                )



    @staticmethod

    async def _invoke(callback: SettlementCallback, event: SettlementEvent) -> None:

        """Call a subscriber, awaiting it when it returns an awaitable."""

        outcome = callback(event)

        if inspect.isawaitable(outcome):

            await outcome



    def _parse_betfair_payload(self, payload: bytes | str) -> SettlementEvent:

        """Decode a Betfair-shaped settlement document into a domain event."""

        raw_text: str = (

            payload.decode("utf-8", errors="strict")

            if isinstance(payload, bytes)

            else payload

        )

        try:

            document: Any = json.loads(raw_text)

        except (json.JSONDecodeError, UnicodeDecodeError) as error:

            raise SettlementError(

                f"settlement callback body is not valid JSON: {error}",

                code="MALFORMED",

                bookmaker=self._bookmaker,

            ) from error



        if not isinstance(document, Mapping):

            raise SettlementError(

                "settlement callback body must be a JSON object",

                code="MALFORMED",

                bookmaker=self._bookmaker,

            )



        bet_id = self._first_string(document, "betId", "bet_id", "orderId")

        if not bet_id:

            raise SettlementError(

                "settlement callback is missing betId",

                code="MALFORMED",

                bookmaker=self._bookmaker,

            )



        event_id = (

            self._first_string(document, "eventId", "event_id", "id")

            or f"{self._bookmaker}:{bet_id}"

        )

        outcome = self._coerce_outcome(

            self._first_string(document, "status", "betOutcome", "outcome", "result")

        )

        stake = self._coerce_money(

            document, "stake", "sizeSettled", "sizeMatched", "size"

        )

        payout = self._coerce_money(document, "payout", "settledAmount", "returns")

        profit_hint = self._first_value(document, "profit", "netProfit")



        if payout == 0 and profit_hint is not None:

            payout = (stake + self._as_decimal(profit_hint, "profit")).quantize(

                MONEY_EXPONENT

            )

        if outcome.is_stake_returned and payout == 0:

            payout = stake



        selection_raw = self._first_value(document, "selectionId", "selection_id")

        selection_id: int | None

        try:

            selection_id = int(selection_raw) if selection_raw is not None else None

        except (TypeError, ValueError):

            selection_id = None



        return SettlementEvent(

            event_id=event_id,

            bookmaker=self._bookmaker,

            bet_id=bet_id,

            outcome=outcome,

            stake=stake,

            payout=payout,

            market_id=self._first_string(document, "marketId", "market_id"),

            selection_id=selection_id,

            currency=self._first_string(document, "currencyCode", "currency") or "INR",

            settled_at=self._coerce_timestamp(

                self._first_string(

                    document, "settledDate", "settled_at", "timestamp", "itemDate"

                )

            ),

            raw=dict(document),

        )



    @staticmethod

    def _first_value(document: Mapping[str, Any], *keys: str) -> Any:

        for key in keys:

            if key in document and document[key] is not None:

                return document[key]

        return None



    @classmethod

    def _first_string(cls, document: Mapping[str, Any], *keys: str) -> str | None:

        value = cls._first_value(document, *keys)

        if value is None:

            return None

        text = str(value).strip()

        return text or None



    @staticmethod

    def _coerce_outcome(value: str | None) -> SettlementOutcome:

        if not value:

            return SettlementOutcome.UNKNOWN

        normalised = value.strip().upper().replace("-", "_").replace(" ", "_")

        return _OUTCOME_ALIASES.get(normalised, SettlementOutcome.UNKNOWN)



    def _coerce_money(self, document: Mapping[str, Any], *keys: str) -> Decimal:

        value = self._first_value(document, *keys)

        if value is None:

            return Decimal("0.00")

        return self._as_decimal(value, keys[0])



    def _as_decimal(self, value: Any, field_name: str) -> Decimal:

        """Coerce a JSON scalar to ``Decimal`` without binary float drift."""

        try:

            if isinstance(value, Decimal):

                amount = value

            elif isinstance(value, float):

                amount = Decimal(repr(value))

            else:

                amount = Decimal(str(value))

        except (InvalidOperation, TypeError, ValueError) as error:

            raise SettlementError(

                f"settlement field {field_name} is not numeric: {value!r}",

                code="MALFORMED",

                bookmaker=self._bookmaker,

            ) from error

        return amount.quantize(MONEY_EXPONENT)



    @staticmethod

    def _coerce_timestamp(value: str | None) -> datetime:

        """Parse an ISO-8601 timestamp, defaulting to now in UTC."""

        if not value:

            return datetime.now(tz=timezone.utc)

        text = value.strip().replace("Z", "+00:00")

        try:

            parsed = datetime.fromisoformat(text)

        except ValueError:

            return datetime.now(tz=timezone.utc)

        if parsed.tzinfo is None:

            return parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

