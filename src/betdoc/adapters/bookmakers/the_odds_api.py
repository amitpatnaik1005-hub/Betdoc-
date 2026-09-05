"""The Odds API v4 adapter: dynamic, pooled, quota-aware REST ingestion.

Turns a polled REST endpoint into a continuous tick stream by running one
independent poller task per sport that pushes into a bounded queue, so a slow
or throttled sport can never head-of-line block the others.

Nothing about sports, regions or markets is hardcoded: pass any combination
supported by the venue and unknown market keys land in ``GenericMarket``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx
from pydantic import ValidationError
from tenacity import (
    RetryCallState,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

from betdoc.application.ports.bookmaker_client import (
    AuthenticationError,
    BaseBookmakerAdapter,
    BookmakerError,
    PayloadSchemaError,
    RateLimitedError,
    RawPayload,
    TransientBookmakerError,
)
from betdoc.domain.models.odds import (
    AnyMarket,
    GenericMarket,
    GenericSelection,
    HandicapMarket,
    HandicapSelection,
    MarketType,
    MoneylineMarket,
    MoneylineSelection,
    OddsTick,
    OutcomeSide,
    SourceTransport,
    TotalsMarket,
    TotalsSelection,
    utc_now,
)

__all__ = ["TheOddsApiAdapter"]

logger: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_BASE_URL: Final[str] = "https://api.the-odds-api.com/v4"
_MONEYLINE_KEYS: Final[frozenset[str]] = frozenset(
    {"h2h", "h2h_lay", "h2h_3_way", "draw_no_bet", "h2h_p1", "h2h_q1", "h2h_h1"}
)
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
_QUOTA_HEADER: Final[str] = "x-requests-remaining"
_LOW_QUOTA_THRESHOLD: Final[int] = 500


# --------------------------------------------------------------------------- #
# Retry policy
# --------------------------------------------------------------------------- #


class _WaitRespectingRetryAfter(wait_base):
    """Exponential backoff with jitter, but obey ``Retry-After`` when the venue
    sends one. Ignoring it is the fastest route to a hard IP ban."""

    def __init__(self, fallback: wait_base, *, ceiling: float = 60.0) -> None:
        self._fallback = fallback
        self._ceiling = ceiling

    def __call__(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        if outcome is not None and outcome.failed:
            exc = outcome.exception()
            if isinstance(exc, RateLimitedError) and exc.retry_after is not None:
                return min(max(exc.retry_after, 0.0), self._ceiling)
        return float(self._fallback(retry_state))


_WAIT_POLICY: Final[wait_base] = _WaitRespectingRetryAfter(
    wait_exponential_jitter(initial=0.5, max=30.0, exp_base=2.0, jitter=1.0)
)


@dataclass(frozen=True, slots=True)
class _ErrorSignal:
    """Terminal fault ferried from a poller task to the consuming generator."""

    error: BaseException


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


class TheOddsApiAdapter(BaseBookmakerAdapter):
    """Concrete REST adapter for The Odds API v4.

    Args:
        api_key: Venue API key.
        sports: Sport keys to poll, e.g. ``("soccer_epl", "basketball_nba")``.
        regions: Region keys, e.g. ``("eu", "uk", "us")``.
        markets: Market keys, e.g. ``("h2h", "spreads", "totals")``.
        bookmakers: Optional venue filter; overrides ``regions`` when supplied.
        poll_interval: Base seconds between polls per sport.
        emit_unchanged: When False (default) identical snapshots are suppressed.
        queue_size: Bounded backpressure buffer. On overflow the OLDEST tick is
            dropped, because in betting a stale price is worthless.
    """

    def __init__(
        self,
        *,
        api_key: str,
        sports: Sequence[str],
        regions: Sequence[str] = ("eu",),
        markets: Sequence[str] = ("h2h",),
        bookmakers: Sequence[str] | None = None,
        base_url: str = DEFAULT_BASE_URL,
        odds_format: str = "decimal",
        date_format: str = "iso",
        poll_interval: float = 2.0,
        emit_unchanged: bool = False,
        queue_size: int = 10_000,
        max_connections: int = 100,
        max_keepalive: int = 40,
        connect_timeout: float = 3.0,
        read_timeout: float = 8.0,
        max_attempts: int = 6,
        bookmaker_name: str = "the_odds_api",
    ) -> None:
        super().__init__(bookmaker=bookmaker_name, transport=SourceTransport.REST)

        if not api_key:
            msg = "api_key must not be empty"
            raise ValueError(msg)
        if not sports:
            msg = "at least one sport key is required"
            raise ValueError(msg)
        if not markets:
            msg = "at least one market key is required"
            raise ValueError(msg)
        if poll_interval < 0.2:
            msg = "poll_interval below 0.2s will exhaust a REST quota, use a WS adapter"
            raise ValueError(msg)

        self._api_key = api_key
        self._sports = tuple(dict.fromkeys(sports))
        self._regions = tuple(dict.fromkeys(regions))
        self._markets = tuple(dict.fromkeys(markets))
        self._bookmakers = tuple(dict.fromkeys(bookmakers)) if bookmakers else None
        self._base_url = base_url.rstrip("/")
        self._odds_format = odds_format
        self._date_format = date_format
        self._poll_interval = poll_interval
        self._emit_unchanged = emit_unchanged
        self._max_attempts = max_attempts

        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
            keepalive_expiry=30.0,
        )
        self._timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
        )

        self._client: httpx.AsyncClient | None = None
        self._connect_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._queue: asyncio.Queue[OddsTick | _ErrorSignal] = asyncio.Queue(maxsize=queue_size)
        self._digests: dict[str, str] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------ lifecycle ------------------------------ #

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._client is not None and not self._client.is_closed:
                return
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                limits=self._limits,
                timeout=self._timeout,
                http2=True,
                follow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "betdoc-ingestor/1.0"},
            )
            self._closed.clear()
            self._logger.info(
                "connected sports=%d regions=%s markets=%s",
                len(self._sports),
                ",".join(self._regions),
                ",".join(self._markets),
            )

    async def close(self) -> None:
        self._closed.set()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        async with self._connect_lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
        self._logger.info("closed metrics=%s", self.metrics.snapshot())

    async def is_connected(self) -> bool:
        return self._client is not None and not self._client.is_closed

    # ------------------------------ streaming ------------------------------ #

    async def stream_live_ticks(
        self,
        *,
        sports: Sequence[str] | None = None,
        markets: Sequence[str] | None = None,
    ) -> AsyncIterator[OddsTick]:
        """Fan every configured sport into one continuous, ordered tick stream."""
        await self.connect()
        selected_sports = tuple(sports) if sports else self._sports
        selected_markets = tuple(markets) if markets else self._markets

        for sport in selected_sports:
            task = asyncio.create_task(
                self._poll_sport_forever(sport, selected_markets),
                name=f"{self.bookmaker}:poll:{sport}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        try:
            while not self._closed.is_set() or not self._queue.empty():
                item = await self._queue.get()
                if isinstance(item, _ErrorSignal):
                    raise item.error
                self.metrics.ticks_emitted += 1
                self.metrics.last_tick_monotonic_ns = item.received_monotonic_ns
                yield item
        finally:
            # Generator closed, consumer cancelled, or terminal fault: never
            # leave orphaned pollers burning API quota.
            for task in tuple(self._tasks):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _poll_sport_forever(self, sport: str, markets: Sequence[str]) -> None:
        """One resilient poll loop per sport. Terminal faults are forwarded."""
        while not self._closed.is_set():
            started = asyncio.get_running_loop().time()
            try:
                events = await self._fetch_odds(sport, markets)
                for payload in self._iter_bookmaker_payloads(events):
                    self._offer(payload)
            except asyncio.CancelledError:
                raise
            except (AuthenticationError, ValueError) as exc:
                # Non-recoverable: stop this poller and surface to the consumer.
                self.metrics.transport_errors += 1
                await self._queue.put(_ErrorSignal(exc))
                return
            except BookmakerError:
                # tenacity already exhausted its attempts: log, pause, resume.
                self.metrics.transport_errors += 1
                self._logger.warning("poll failed for sport=%s", sport, exc_info=True)
                await self._sleep(self._poll_interval * 4)
                continue

            elapsed = asyncio.get_running_loop().time() - started
            await self._sleep(max(self._effective_interval() - elapsed, 0.0))

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep: shuts down instantly instead of after a full tick."""
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._closed.wait(), timeout=seconds)

    def _effective_interval(self) -> float:
        """Widen the interval as quota depletes: outlive the day, do not sprint."""
        remaining = self.metrics.quota_remaining
        if remaining is None or remaining > _LOW_QUOTA_THRESHOLD:
            return self._poll_interval
        if remaining <= 0:
            return max(self._poll_interval * 30.0, 60.0)
        return self._poll_interval * (1.0 + _LOW_QUOTA_THRESHOLD / max(remaining, 1))

    def _offer(self, payload: RawPayload) -> None:
        """Normalise, suppress unchanged prices, enqueue with drop-oldest policy."""
        try:
            tick = self.normalize_payload(payload)
        except PayloadSchemaError:
            self.metrics.payload_errors += 1
            self._logger.warning("quarantined payload", exc_info=True)
            return

        digest = tick.price_digest
        if not self._emit_unchanged and self._digests.get(tick.dedupe_key) == digest:
            self.metrics.ticks_suppressed += 1
            return
        self._digests[tick.dedupe_key] = digest

        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.metrics.ticks_dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(tick)

    # ------------------------------ networking ----------------------------- #

    @retry(
        retry=retry_if_exception_type(TransientBookmakerError),
        wait=_WAIT_POLICY,
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _fetch_odds(
        self, sport: str, markets: Sequence[str]
    ) -> tuple[dict[str, Any], ...]:
        """GET one sport's odds snapshot. Retries 429/5xx with capped backoff."""
        if self._client is None or self._client.is_closed:
            await self.connect()
        assert self._client is not None  # noqa: S101 - narrowed by connect()

        params: dict[str, str] = {
            "apiKey": self._api_key,
            "markets": ",".join(markets),
            "oddsFormat": self._odds_format,
            "dateFormat": self._date_format,
        }
        if self._bookmakers:
            params["bookmakers"] = ",".join(self._bookmakers)
        else:
            params["regions"] = ",".join(self._regions)

        try:
            self.metrics.requests_sent += 1
            response = await self._client.get(f"/sports/{sport}/odds", params=params)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            msg = f"transport failure for sport={sport}: {exc!r}"
            raise TransientBookmakerError(msg, bookmaker=self.bookmaker) from exc

        self._record_quota(response)

        if response.status_code in (401, 403):
            msg = f"authentication rejected (HTTP {response.status_code})"
            raise AuthenticationError(msg, bookmaker=self.bookmaker)
        if response.status_code == 429:
            msg = "rate limited by venue"
            raise RateLimitedError(
                msg, bookmaker=self.bookmaker, retry_after=self._parse_retry_after(response)
            )
        if response.status_code in _RETRYABLE_STATUS:
            msg = f"retryable upstream status {response.status_code} for sport={sport}"
            raise TransientBookmakerError(msg, bookmaker=self.bookmaker)
        if response.status_code >= 400:
            msg = f"permanent upstream status {response.status_code}: {response.text[:256]}"
            raise BookmakerError(msg, bookmaker=self.bookmaker)

        try:
            body: Any = response.json()
        except ValueError as exc:
            msg = f"non-JSON body for sport={sport}"
            raise TransientBookmakerError(msg, bookmaker=self.bookmaker) from exc

        if not isinstance(body, list):
            msg = f"expected a JSON array of events, got {type(body).__name__}"
            raise PayloadSchemaError(msg, bookmaker=self.bookmaker)
        return tuple(event for event in body if isinstance(event, dict))

    async def fetch_snapshot(
        self, *, sports: Sequence[str] | None = None, markets: Sequence[str] | None = None
    ) -> tuple[OddsTick, ...]:
        """One-shot pull across sports. Used for cold start and reconciliation."""
        await self.connect()
        selected_markets = tuple(markets) if markets else self._markets
        results = await asyncio.gather(
            *(self._fetch_odds(s, selected_markets) for s in (sports or self._sports)),
            return_exceptions=True,
        )
        payloads: list[RawPayload] = []
        for outcome in results:
            if isinstance(outcome, BaseException):
                self.metrics.transport_errors += 1
                self._logger.warning("snapshot leg failed", exc_info=outcome)
                continue
            payloads.extend(self._iter_bookmaker_payloads(outcome))
        return self.normalize_batch(payloads)

    def _record_quota(self, response: httpx.Response) -> None:
        raw = response.headers.get(_QUOTA_HEADER)
        if raw is None:
            return
        try:
            self.metrics.quota_remaining = int(float(raw))
        except ValueError:
            return
        if self.metrics.quota_remaining is not None and (
            self.metrics.quota_remaining <= _LOW_QUOTA_THRESHOLD
        ):
            self._logger.warning("api quota low: %s remaining", self.metrics.quota_remaining)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # ---------------------------- normalisation ---------------------------- #

    @staticmethod
    def _iter_bookmaker_payloads(
        events: Sequence[dict[str, Any]],
    ) -> Iterator[dict[str, Any]]:
        """Flatten venue events into one payload per (event, bookmaker).

        The venue nests many books inside one event, but an ``OddsTick`` is one
        book's view of one event, so we fan out before normalising.
        """
        for event in events:
            books = event.get("bookmakers")
            if not isinstance(books, list):
                continue
            base = {k: v for k, v in event.items() if k != "bookmakers"}
            for book in books:
                if isinstance(book, dict):
                    yield {**base, "bookmaker": book}

    def normalize_payload(self, payload: RawPayload) -> OddsTick:
        """Convert one flattened venue payload into a validated ``OddsTick``."""
        received_at = utc_now()
        book = payload.get("bookmaker")
        if not isinstance(book, dict):
            msg = "payload is missing its flattened 'bookmaker' object"
            raise PayloadSchemaError(msg, bookmaker=self.bookmaker, payload=payload)

        try:
            event_id = str(payload["id"])
            sport_key = str(payload["sport_key"])
            home_team = str(payload["home_team"])
            away_team = str(payload["away_team"])
            commence_time = self._parse_ts(payload["commence_time"])
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"unusable event envelope: {exc!r}"
            raise PayloadSchemaError(msg, bookmaker=self.bookmaker, payload=payload) from exc

        book_ts = self._parse_ts(book.get("last_update")) or received_at
        raw_markets = book.get("markets")
        if not isinstance(raw_markets, list) or not raw_markets:
            msg = f"no markets present for event {event_id!r}"
            raise PayloadSchemaError(msg, bookmaker=self.bookmaker, payload=payload)

        markets: list[AnyMarket] = []
        for raw_market in raw_markets:
            if isinstance(raw_market, dict):
                markets.extend(self._build_markets(raw_market, home_team, away_team))
        if not markets:
            msg = f"no market survived normalisation for event {event_id!r}"
            raise PayloadSchemaError(msg, bookmaker=self.bookmaker, payload=payload)

        try:
            return OddsTick(
                bookmaker=str(book.get("key") or self.bookmaker),
                transport=self.transport,
                event_id=event_id,
                sport_key=sport_key,
                league=str(payload["sport_title"]) if payload.get("sport_title") else None,
                home_team=home_team,
                away_team=away_team,
                commence_time=commence_time,
                is_live=commence_time <= received_at,
                markets=tuple(markets),
                bookmaker_timestamp=book_ts,
                received_at=received_at,
            )
        except ValidationError as exc:
            msg = f"domain validation failed for event {event_id!r}: {exc.errors(include_url=False)}"
            raise PayloadSchemaError(msg, bookmaker=self.bookmaker, payload=payload) from exc

    def _build_markets(
        self, raw_market: dict[str, Any], home_team: str, away_team: str
    ) -> tuple[AnyMarket, ...]:
        """Map one venue market key to one or more domain markets.

        Venue keys such as ``alternate_totals`` pack several lines into a single
        market, so we split by line: one domain market per priceable book.
        """
        key = str(raw_market.get("key") or "").strip()
        raw_outcomes = raw_market.get("outcomes")
        if not key or not isinstance(raw_outcomes, list):
            return ()

        last_update = self._parse_ts(raw_market.get("last_update"))
        market_type = self._classify(key)
        parsed = [
            item
            for item in (self._parse_outcome(o, home_team, away_team) for o in raw_outcomes)
            if item is not None
        ]
        if not parsed:
            return ()

        try:
            if market_type is MarketType.MONEYLINE:
                return (
                    MoneylineMarket(
                        key=key,
                        last_update=last_update,
                        runners=tuple(
                            MoneylineSelection(name=n, outcome=o, price=p)
                            for n, o, p, _ in parsed
                            if o in (OutcomeSide.HOME, OutcomeSide.DRAW, OutcomeSide.AWAY)
                        ),
                    ),
                )

            if market_type is MarketType.ASIAN_HANDICAP:
                out: list[AnyMarket] = []
                for line, group in self._group_by_abs_line(parsed).items():
                    runners = tuple(
                        HandicapSelection(name=n, outcome=o, price=p, line=pt)
                        for n, o, p, pt in group
                        if o in (OutcomeSide.HOME, OutcomeSide.AWAY) and pt is not None
                    )
                    if len(runners) != 2:
                        continue
                    home_runner = next(r for r in runners if r.outcome is OutcomeSide.HOME)
                    out.append(
                        HandicapMarket(
                            key=f"{key}@{line:g}",
                            line=home_runner.line,
                            last_update=last_update,
                            runners=(runners[0], runners[1]),
                        )
                    )
                return tuple(out)

            if market_type is MarketType.TOTALS:
                totals: list[AnyMarket] = []
                for line, group in self._group_by_line(parsed).items():
                    runners = tuple(
                        TotalsSelection(name=n, outcome=o, price=p, line=line)
                        for n, o, p, _ in group
                        if o in (OutcomeSide.OVER, OutcomeSide.UNDER)
                    )
                    if len(runners) != 2:
                        continue
                    totals.append(
                        TotalsMarket(
                            key=f"{key}@{line:g}",
                            line=line,
                            last_update=last_update,
                            runners=(runners[0], runners[1]),
                        )
                    )
                return tuple(totals)

            # Dynamic path: unknown venue market, preserved verbatim.
            return (
                GenericMarket(
                    key=key,
                    last_update=last_update,
                    runners=tuple(
                        GenericSelection(name=n, outcome=o, price=p, line=pt)
                        for n, o, p, pt in parsed
                    ),
                    attributes={"venue_market_key": key},
                ),
            )
        except ValidationError:
            # An incoherent market must not poison the rest of the event.
            self.metrics.payload_errors += 1
            self._logger.debug("dropped invalid market key=%s", key, exc_info=True)
            return ()

    @staticmethod
    def _classify(key: str) -> MarketType:
        """Substring classification so new venue keys route themselves."""
        lowered = key.lower()
        if lowered in _MONEYLINE_KEYS:
            return MarketType.MONEYLINE
        if "spread" in lowered or "handicap" in lowered:
            return MarketType.ASIAN_HANDICAP
        if "total" in lowered or "over_under" in lowered:
            return MarketType.TOTALS
        return MarketType.GENERIC

    def _parse_outcome(
        self, raw: Any, home_team: str, away_team: str
    ) -> tuple[str, OutcomeSide, float, float | None] | None:
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        price = raw.get("price")
        if not isinstance(name, str) or not isinstance(price, (int, float)):
            return None
        if isinstance(price, bool) or float(price) <= 1.0:
            return None
        point_raw = raw.get("point")
        point = float(point_raw) if isinstance(point_raw, (int, float)) else None
        return name, self._to_outcome_side(name, home_team, away_team), float(price), point

    @staticmethod
    def _to_outcome_side(name: str, home_team: str, away_team: str) -> OutcomeSide:
        """Resolve the venue's free-text label to a canonical outcome identity."""
        normalized = name.strip().casefold()
        if normalized == home_team.strip().casefold():
            return OutcomeSide.HOME
        if normalized == away_team.strip().casefold():
            return OutcomeSide.AWAY
        return {
            "draw": OutcomeSide.DRAW,
            "tie": OutcomeSide.DRAW,
            "over": OutcomeSide.OVER,
            "under": OutcomeSide.UNDER,
            "yes": OutcomeSide.YES,
            "no": OutcomeSide.NO,
        }.get(normalized, OutcomeSide.OTHER)

    @staticmethod
    def _group_by_line(
        parsed: Sequence[tuple[str, OutcomeSide, float, float | None]],
    ) -> dict[float, list[tuple[str, OutcomeSide, float, float | None]]]:
        grouped: dict[float, list[tuple[str, OutcomeSide, float, float | None]]] = {}
        for item in parsed:
            point = item[3]
            if point is None:
                continue
            grouped.setdefault(point, []).append(item)
        return grouped

    @staticmethod
    def _group_by_abs_line(
        parsed: Sequence[tuple[str, OutcomeSide, float, float | None]],
    ) -> dict[float, list[tuple[str, OutcomeSide, float, float | None]]]:
        """Pair mirrored handicap sides (-1.5 with +1.5) into one market."""
        grouped: dict[float, list[tuple[str, OutcomeSide, float, float | None]]] = {}
        for item in parsed:
            point = item[3]
            if point is None:
                continue
            grouped.setdefault(abs(point), []).append(item)
        return grouped

    @staticmethod
    def _parse_ts(value: Any) -> datetime | None:
        """RFC 3339 with a trailing ``Z``. Returns None rather than raising."""
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
