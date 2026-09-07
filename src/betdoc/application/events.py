"""The envelope pattern: transport metadata separated from domain payload.

Every message on the bus is an :class:`EventEnvelope`. Consumers therefore get
``trace_id`` for correlation, ``retry_count`` for poison detection, and
``timestamp`` for staleness gating without any payload type needing to know
those concepts exist. The payload stays a clean domain object.

Serialisation is always ``model_dump_json``. Never ``pickle`` (arbitrary code
execution on a compromised bus) and never bare ``json`` (silently drops
``Decimal``, ``UUID`` and ``datetime`` fidelity).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import Self

__all__ = [
    "EventEnvelope",
    "PayloadT",
    "RawOddsPayload",
    "Streams",
    "new_trace_id",
    "utc_now",
]

PayloadT = TypeVar("PayloadT", bound=BaseModel)

_STRICT: Final[ConfigDict] = ConfigDict(
    frozen=True,
    extra="forbid",
    validate_default=True,
    revalidate_instances="never",
    str_strip_whitespace=True,
)


def utc_now() -> datetime:
    """Timezone-aware UTC now. Never naive, never local."""
    return datetime.now(UTC)


def new_trace_id() -> str:
    """A compact, log-friendly correlation id (32 hex characters)."""
    return uuid.uuid4().hex


class Streams:
    """Canonical stream names. Centralised so a typo cannot split a topic.

    A misspelled stream name does not raise: it silently creates a second,
    empty stream that nobody consumes, and the data is simply gone.
    """

    RAW_ODDS: Final[str] = "betdoc:raw_odds"
    NORMALISED_ODDS: Final[str] = "betdoc:normalised_odds"
    OPPORTUNITIES: Final[str] = "betdoc:opportunities"
    BET_INTENTS: Final[str] = "betdoc:bet_intents"
    SETTLEMENTS: Final[str] = "betdoc:settlements"

    @staticmethod
    def dlq(stream_name: str) -> str:
        """Dead letter queue paired with a stream. One DLQ per stream."""
        return f"{stream_name}:dlq"


class RawOddsPayload(BaseModel):
    """An un-normalised price exactly as the venue reported it.

    Deliberately permissive on values and strict on shape: the ingestor's job
    is to move bytes reliably, and the data-quality layer downstream decides
    what is trustworthy.
    """

    model_config = _STRICT

    bookmaker: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=128)
    market_key: str = Field(min_length=1, max_length=128)
    outcome_key: str = Field(min_length=1, max_length=64)
    decimal_odds: float = Field(gt=1.0, le=10_000.0)
    bookmaker_timestamp: datetime
    received_at: datetime = Field(default_factory=utc_now)
    sport_key: str | None = Field(default=None, max_length=64)

    @field_validator("bookmaker_timestamp", "received_at", mode="after")
    @classmethod
    def _reject_naive_datetimes(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "datetime must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @property
    def ingest_latency_ms(self) -> float:
        """Venue clock to our clock. Negative values indicate clock skew."""
        return (self.received_at - self.bookmaker_timestamp).total_seconds() * 1_000.0

    @property
    def dedupe_key(self) -> str:
        return (
            f"{self.bookmaker}:{self.event_id}:{self.market_key}:{self.outcome_key}"
        )


class EventEnvelope(BaseModel, Generic[PayloadT]):
    """Transport wrapper carrying the metadata every consumer needs.

    Immutable by design. Mutating a message in flight makes redelivery
    non-deterministic, so :meth:`with_retry` returns a new envelope instead.
    """

    model_config = _STRICT

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    trace_id: str = Field(
        default_factory=new_trace_id,
        min_length=8,
        max_length=64,
        description="Bound to structlog by every service that handles the event.",
    )
    timestamp: datetime = Field(default_factory=utc_now)
    retry_count: int = Field(default=0, ge=0, le=1_000)
    event_type: str = Field(default="", max_length=128)
    data: PayloadT

    @field_validator("timestamp", mode="after")
    @classmethod
    def _reject_naive_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "timestamp must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @classmethod
    def wrap(
        cls,
        data: PayloadT,
        *,
        trace_id: str | None = None,
        event_type: str | None = None,
    ) -> Self:
        """Build an envelope, inheriting or minting a trace id."""
        return cls(
            data=data,
            trace_id=trace_id or new_trace_id(),
            event_type=event_type or type(data).__name__,
        )

    def with_retry(self) -> Self:
        """A copy with ``retry_count`` incremented. The original is untouched."""
        return self.model_copy(update={"retry_count": self.retry_count + 1})

    @property
    def age_ms(self) -> float:
        """Time since creation. The staleness gate for any consumer."""
        return (utc_now() - self.timestamp).total_seconds() * 1_000.0

    def is_stale(self, max_age_ms: float) -> bool:
        return self.age_ms > max_age_ms

    def log_context(self) -> dict[str, Any]:
        """Fields to bind to structlog. Never includes the payload body."""
        return {
            "event_id": str(self.event_id),
            "trace_id": self.trace_id,
            "event_type": self.event_type,
            "retry_count": self.retry_count,
            "age_ms": round(self.age_ms, 3),
        }
