"""Dynamic, immutable odds schemas for the Universal Data Ingestion Layer.

Design contract
---------------
* **Immutable / hashable**: every model is ``frozen=True`` and every collection is a
  ``tuple``, so a tick can be published to N consumers across N tasks with zero
  defensive copying and zero mutation races.
* **Strict**: ``extra="forbid"``. All derived analytics are plain ``@property``
  values, never ``@computed_field``, so serialise -> validate round-trips cleanly
  (a serialised computed field would be rejected by ``extra="forbid"``).
  We persist *facts*; metrics are derived at read time.
* **Dynamic**: ``AnyMarket`` is a callable-discriminated union. Unknown market
  keys degrade gracefully into ``GenericMarket`` instead of raising, so a new
  bookmaker market (player props, corners, bookings) flows through the pipeline
  on day one without a schema deploy.
* **orjson / msgspec compatible**: all field types are JSON primitives, enums are
  ``StrEnum``, and datetimes are timezone-aware RFC 3339.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    model_validator,
)

try:  # optional acceleration for bulk (list-of-dict) encoding
    from orjson import dumps as _orjson_dumps
except ModuleNotFoundError:  # pragma: no cover - orjson is an optional extra
    _orjson_dumps = None

__all__ = [
    "AnyMarket",
    "DecimalOdds",
    "GenericMarket",
    "GenericSelection",
    "HandicapMarket",
    "HandicapSelection",
    "MarketType",
    "MoneylineMarket",
    "MoneylineSelection",
    "OddsTick",
    "OutcomeSide",
    "QuarterLine",
    "SourceTransport",
    "TotalsMarket",
    "TotalsSelection",
    "encode_tick",
    "encode_ticks",
    "decode_tick",
    "utc_now",
]

# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

MAX_DECIMAL_ODDS: Final[float] = 10_000.0
_LINE_STEP: Final[float] = 0.25
_EPS: Final[float] = 1e-9


def utc_now() -> datetime:
    """Timezone-aware wall clock. Use ONLY for stamping, never for durations."""
    return datetime.now(UTC)


def _validate_quarter_line(value: float) -> float:
    """Asian lines are always multiples of 0.25 (0.0, -0.25, +1.75, 2.5 ...)."""
    if not math.isfinite(value):
        msg = "line must be a finite number"
        raise ValueError(msg)
    steps = value / _LINE_STEP
    if abs(steps - round(steps)) > _EPS:
        msg = f"line {value!r} is not a multiple of {_LINE_STEP}"
        raise ValueError(msg)
    return round(steps) * _LINE_STEP


def _validate_decimal_odds(value: float) -> float:
    if not math.isfinite(value):
        msg = "decimal odds must be a finite number"
        raise ValueError(msg)
    return value


QuarterLine = Annotated[float, AfterValidator(_validate_quarter_line)]
DecimalOdds = Annotated[
    float,
    Field(gt=1.0, le=MAX_DECIMAL_ODDS),
    AfterValidator(_validate_decimal_odds),
]


class MarketType(StrEnum):
    """Canonical market families. GENERIC is the dynamic escape hatch."""

    MONEYLINE = "moneyline"
    ASIAN_HANDICAP = "asian_handicap"
    TOTALS = "totals"
    GENERIC = "generic"


class OutcomeSide(StrEnum):
    """Canonical outcome identity, decoupled from the bookmaker's label."""

    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"
    OTHER = "other"


class SourceTransport(StrEnum):
    WEBSOCKET = "websocket"
    REST = "rest"
    SSE = "sse"
    FIX = "fix"
    REPLAY = "replay"


# --------------------------------------------------------------------------- #
# Selections
# --------------------------------------------------------------------------- #

_STRICT_CONFIG: Final[ConfigDict] = ConfigDict(
    frozen=True,
    extra="forbid",
    str_strip_whitespace=True,
    validate_default=True,
    revalidate_instances="never",
    ser_json_timedelta="float",
)


class SelectionBase(BaseModel):
    """A single priceable runner within a market."""

    model_config = _STRICT_CONFIG

    name: str = Field(min_length=1, max_length=128, description="Raw bookmaker label.")
    outcome: OutcomeSide
    price: DecimalOdds
    line: QuarterLine | None = None
    is_suspended: bool = False
    max_stake: float | None = Field(default=None, gt=0.0, description="Book limit, if exposed.")

    @property
    def implied_probability(self) -> float:
        return 1.0 / self.price


class MoneylineSelection(SelectionBase):
    """1X2 / two-way moneyline runner. Carries no line by definition."""

    outcome: Literal[OutcomeSide.HOME, OutcomeSide.DRAW, OutcomeSide.AWAY]
    line: None = None


class HandicapSelection(SelectionBase):
    """Asian handicap runner. Each side carries its own (mirrored) line."""

    outcome: Literal[OutcomeSide.HOME, OutcomeSide.AWAY]
    line: QuarterLine


class TotalsSelection(SelectionBase):
    """Over/Under runner. Both sides share the same line."""

    outcome: Literal[OutcomeSide.OVER, OutcomeSide.UNDER]
    line: QuarterLine


class GenericSelection(SelectionBase):
    """Anything we have not modelled yet: props, corners, cards, correct score."""

    attributes: dict[str, JsonValue] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Markets
# --------------------------------------------------------------------------- #


class MarketBase(BaseModel):
    """Common market surface. Subclasses pin the selection type and arity."""

    model_config = _STRICT_CONFIG

    market_type: MarketType
    key: str = Field(min_length=1, max_length=128, description="Raw bookmaker market key.")
    line: QuarterLine | None = None
    last_update: AwareDatetime | None = None
    is_suspended: bool = False

    @property
    def selections(self) -> tuple[SelectionBase, ...]:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def expected_arity(self) -> tuple[int, ...] | None:
        """Selection counts that constitute a *complete* book for this market."""
        return None

    @property
    def is_complete(self) -> bool:
        arity = self.expected_arity
        return arity is not None and len(self.selections) in arity

    @property
    def implied_probabilities(self) -> dict[OutcomeSide, float]:
        return {s.outcome: s.implied_probability for s in self.selections}

    @property
    def overround(self) -> float | None:
        """Bookmaker margin. ``None`` when the market is not a complete book.

        Never devig with this value: proportional normalisation is biased by the
        favourite-longshot effect. Use Shin or the power method downstream.
        """
        if not self.is_complete or any(s.is_suspended for s in self.selections):
            return None
        return sum(s.implied_probability for s in self.selections) - 1.0

    def selection(self, outcome: OutcomeSide) -> SelectionBase | None:
        return next((s for s in self.selections if s.outcome is outcome), None)

    @property
    def price_vector(self) -> tuple[tuple[str, float], ...]:
        """Deterministic (outcome, price) pairs used for change detection."""
        return tuple(sorted((f"{s.outcome}:{s.line}", s.price) for s in self.selections))


class MoneylineMarket(MarketBase):
    market_type: Literal[MarketType.MONEYLINE] = MarketType.MONEYLINE
    line: None = None
    runners: tuple[MoneylineSelection, ...] = Field(min_length=2, max_length=3)

    @property
    def selections(self) -> tuple[SelectionBase, ...]:
        return self.runners

    @property
    def expected_arity(self) -> tuple[int, ...]:
        return (2, 3)

    @model_validator(mode="after")
    def _unique_outcomes(self) -> Self:
        outcomes = [r.outcome for r in self.runners]
        if len(set(outcomes)) != len(outcomes):
            msg = f"duplicate moneyline outcomes in market {self.key!r}"
            raise ValueError(msg)
        return self


class HandicapMarket(MarketBase):
    market_type: Literal[MarketType.ASIAN_HANDICAP] = MarketType.ASIAN_HANDICAP
    line: QuarterLine = Field(description="Handicap applied to the HOME side.")
    runners: tuple[HandicapSelection, HandicapSelection]

    @property
    def selections(self) -> tuple[SelectionBase, ...]:
        return self.runners

    @property
    def expected_arity(self) -> tuple[int, ...]:
        return (2,)

    @model_validator(mode="after")
    def _mirrored_lines(self) -> Self:
        home, away = self.runners
        if home.outcome is away.outcome:
            msg = f"handicap market {self.key!r} must hold one HOME and one AWAY runner"
            raise ValueError(msg)
        if abs(home.line + away.line) > _EPS:
            msg = (
                f"handicap lines must mirror: got {home.line} / {away.line} "
                f"in market {self.key!r}"
            )
            raise ValueError(msg)
        expected = home.line if home.outcome is OutcomeSide.HOME else away.line
        if abs(expected - self.line) > _EPS:
            msg = f"market line {self.line} does not match HOME runner line {expected}"
            raise ValueError(msg)
        return self


class TotalsMarket(MarketBase):
    market_type: Literal[MarketType.TOTALS] = MarketType.TOTALS
    line: QuarterLine = Field(description="The Over/Under threshold.")
    runners: tuple[TotalsSelection, TotalsSelection]

    @property
    def selections(self) -> tuple[SelectionBase, ...]:
        return self.runners

    @property
    def expected_arity(self) -> tuple[int, ...]:
        return (2,)

    @model_validator(mode="after")
    def _shared_line(self) -> Self:
        over, under = self.runners
        if over.outcome is under.outcome:
            msg = f"totals market {self.key!r} must hold one OVER and one UNDER runner"
            raise ValueError(msg)
        if abs(over.line - under.line) > _EPS or abs(over.line - self.line) > _EPS:
            msg = f"totals market {self.key!r} has inconsistent lines"
            raise ValueError(msg)
        return self


class GenericMarket(MarketBase):
    """Dynamic fallback: preserves any market shape without a schema change."""

    market_type: Literal[MarketType.GENERIC] = MarketType.GENERIC
    runners: tuple[GenericSelection, ...] = Field(min_length=1)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def selections(self) -> tuple[SelectionBase, ...]:
        return self.runners


def _market_tag(value: object) -> str:
    """Callable discriminator: route unknown market types to ``generic``.

    Accepts both raw mappings (JSON ingest) and model instances (Python ingest).
    """
    raw: object
    if isinstance(value, dict):
        raw = value.get("market_type")
    else:
        raw = getattr(value, "market_type", None)
    try:
        return MarketType(raw).value  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return MarketType.GENERIC.value


AnyMarket = Annotated[
    Annotated[MoneylineMarket, Tag(MarketType.MONEYLINE.value)]
    | Annotated[HandicapMarket, Tag(MarketType.ASIAN_HANDICAP.value)]
    | Annotated[TotalsMarket, Tag(MarketType.TOTALS.value)]
    | Annotated[GenericMarket, Tag(MarketType.GENERIC.value)],
    Discriminator(_market_tag),
]


# --------------------------------------------------------------------------- #
# Master payload
# --------------------------------------------------------------------------- #


class OddsTick(BaseModel):
    """The immutable unit of ingestion: one bookmaker's view of one event.

    Telemetry semantics (do not conflate these three clocks):

    * ``bookmaker_timestamp`` - when the book priced it (their clock, skew-prone).
    * ``received_at``         - when our socket read completed (our wall clock).
    * ``received_monotonic_ns`` - monotonic reference for in-process latency
      budgets; immune to NTP steps, meaningless across processes or reboots.
    """

    model_config = _STRICT_CONFIG

    tick_id: UUID = Field(default_factory=uuid4)
    sequence: int | None = Field(default=None, ge=0, description="WS sequence, gap detection.")

    bookmaker: str = Field(min_length=1, max_length=64)
    transport: SourceTransport

    event_id: str = Field(min_length=1, max_length=128, description="Bookmaker event id.")
    canonical_event_id: str | None = Field(default=None, max_length=128)
    sport_key: str = Field(min_length=1, max_length=64)
    league: str | None = Field(default=None, max_length=128)
    home_team: str = Field(min_length=1, max_length=128)
    away_team: str = Field(min_length=1, max_length=128)
    commence_time: AwareDatetime
    is_live: bool = False

    markets: tuple[AnyMarket, ...] = Field(min_length=1)

    bookmaker_timestamp: AwareDatetime
    received_at: AwareDatetime = Field(default_factory=utc_now)
    received_monotonic_ns: int = Field(default_factory=time.monotonic_ns, ge=0)

    # ---------------------------- telemetry ---------------------------- #

    @property
    def ingest_latency_ms(self) -> float:
        """Book-to-us latency. Can be negative under clock skew: alert, never clamp."""
        delta = self.received_at - self.bookmaker_timestamp
        return delta.total_seconds() * 1_000.0

    def age_ms(self, *, now: datetime | None = None) -> float:
        """Total staleness measured from the bookmaker's own pricing clock."""
        reference = now or utc_now()
        return (reference - self.bookmaker_timestamp).total_seconds() * 1_000.0

    def is_stale(self, max_age_ms: float, *, now: datetime | None = None) -> bool:
        """Hard gate for the risk engine: never stake on a stale price."""
        return self.age_ms(now=now) > max_age_ms

    def telemetry(self) -> dict[str, float | int | str]:
        return {
            "bookmaker": self.bookmaker,
            "transport": self.transport.value,
            "event_id": self.event_id,
            "market_count": len(self.markets),
            "selection_count": sum(len(m.selections) for m in self.markets),
            "ingest_latency_ms": self.ingest_latency_ms,
            "age_ms": self.age_ms(),
        }

    # ---------------------------- identity ----------------------------- #

    @property
    def dedupe_key(self) -> str:
        return f"{self.bookmaker}:{self.event_id}"

    @property
    def price_digest(self) -> str:
        """Stable 16-hex digest of every price on the tick.

        Lets pollers suppress unchanged snapshots without holding the payload,
        which is the difference between a usable REST quota and a burnt one.
        """
        hasher = hashlib.blake2b(digest_size=8)
        for market in sorted(self.markets, key=lambda m: (m.key, m.line or 0.0)):
            hasher.update(market.key.encode())
            hasher.update(repr(market.line).encode())
            for outcome, price in market.price_vector:
                hasher.update(outcome.encode())
                hasher.update(repr(price).encode())
        return hasher.hexdigest()

    def market(self, key: str) -> AnyMarket | None:
        return next((m for m in self.markets if m.key == key), None)

    @model_validator(mode="after")
    def _distinct_market_keys(self) -> Self:
        keys = [(m.key, m.line) for m in self.markets]
        if len(set(keys)) != len(keys):
            msg = f"duplicate (market key, line) pairs on tick for event {self.event_id!r}"
            raise ValueError(msg)
        return self


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #


def encode_tick(tick: OddsTick) -> bytes:
    """Single-tick encode. Pydantic-core's Rust serialiser is the fast path here."""
    return tick.model_dump_json().encode("utf-8")


def encode_ticks(ticks: tuple[OddsTick, ...]) -> bytes:
    """Bulk encode for Redis Streams / Parquet batching. Uses orjson when present."""
    payloads = [t.model_dump(mode="json") for t in ticks]
    if _orjson_dumps is not None:
        return _orjson_dumps(payloads)
    import json

    return json.dumps(payloads, separators=(",", ":")).encode("utf-8")


def decode_tick(data: bytes | str) -> OddsTick:
    """Validate straight from bytes: skips the intermediate Python dict entirely."""
    return OddsTick.model_validate_json(data)
