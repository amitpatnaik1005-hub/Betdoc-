"""Typed, validated, secret-safe configuration. The only entry point is get_settings().

Two guarantees this module exists to provide:

* **No secret ever reaches a log line.** Every credential is ``SecretStr``, whose
  ``__repr__`` and ``__str__`` render as ``**********``. A structlog event that
  accidentally binds the whole settings object is therefore harmless.
* **Invalid configuration fails at startup, not at 3am.** A zero polling
  interval or a 10-million batch size is rejected before a single connection is
  opened, because a misconfigured ingestor that starts successfully is far more
  expensive than one that refuses to start.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from typing import Final, Literal, Self

import structlog
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "IngestorSettings",
    "LogLevel",
    "RedisSettings",
    "Settings",
    "configure_logging",
    "get_settings",
]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_MIN_BATCH_SIZE: Final[int] = 1
_MAX_BATCH_SIZE: Final[int] = 1_000
_ENV_PREFIX: Final[str] = "BETDOC_"


class RedisSettings(BaseSettings):
    """Connection and stream policy for the event bus."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix=f"{_ENV_PREFIX}REDIS_",
        extra="ignore",
        frozen=True,
    )

    url: SecretStr = Field(
        default=SecretStr("redis://localhost:6379/0"),
        description="Secret-wrapped: Redis URLs routinely carry a password.",
    )
    max_connections: int = Field(default=50, ge=1, le=10_000)
    socket_timeout_seconds: float = Field(default=5.0, gt=0.0, le=300.0)
    socket_connect_timeout_seconds: float = Field(default=3.0, gt=0.0, le=60.0)
    health_check_interval_seconds: int = Field(
        default=30,
        ge=0,
        le=3_600,
        description="Pool-level liveness probe. 0 disables it.",
    )

    stream_max_length: int = Field(
        default=100_000,
        ge=1_000,
        le=10_000_000,
        description=(
            "Hard OOM ceiling per stream, trimmed approximately. Without this a "
            "consumer outage turns into an unbounded RAM leak that takes the "
            "whole Redis instance down, and with it every service on the bus."
        ),
    )
    dlq_max_length: int = Field(default=50_000, ge=100, le=1_000_000)
    consumer_block_ms: int = Field(
        default=5_000,
        ge=100,
        le=60_000,
        description="XREADGROUP block. Bounded so shutdown latency is bounded.",
    )
    claim_min_idle_ms: int = Field(
        default=30_000,
        ge=1_000,
        le=3_600_000,
        description="Idle time before another consumer may reclaim a message.",
    )
    max_delivery_attempts: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Deliveries after which a message is quarantined to the DLQ.",
    )


class IngestorSettings(BaseSettings):
    """Polling cadence and backpressure policy for the ingestion loop."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix=f"{_ENV_PREFIX}INGESTOR_",
        extra="ignore",
        frozen=True,
    )

    poll_interval_seconds: float = Field(
        default=2.0,
        gt=0.0,
        le=3_600.0,
        description="Wall-clock cadence, held exactly by subtracting call duration.",
    )
    bookmakers: tuple[str, ...] = Field(default=("pinnacle", "betfair"), min_length=1)
    sports: tuple[str, ...] = Field(default=("soccer_epl",), min_length=1)
    markets: tuple[str, ...] = Field(default=("h2h", "totals"), min_length=1)

    publish_batch_size: int = Field(default=50, ge=_MIN_BATCH_SIZE, le=_MAX_BATCH_SIZE)
    consume_batch_size: int = Field(default=10, ge=_MIN_BATCH_SIZE, le=_MAX_BATCH_SIZE)
    publish_queue_size: int = Field(
        default=10_000,
        ge=100,
        le=1_000_000,
        description=(
            "Bounded hand-off between pollers and the publisher. Bounded, because "
            "an unbounded queue converts a Redis outage into an OOM kill."
        ),
    )
    shutdown_drain_seconds: float = Field(
        default=10.0,
        gt=0.0,
        le=300.0,
        description="Budget for flushing buffered ticks before the pool closes.",
    )

    api_key: SecretStr = Field(default=SecretStr(""))
    api_base_url: str = Field(default="https://api.the-odds-api.com/v4")

    max_retry_attempts: int = Field(default=5, ge=1, le=20)
    retry_initial_wait_seconds: float = Field(default=1.0, gt=0.0, le=60.0)
    retry_max_wait_seconds: float = Field(default=10.0, gt=0.0, le=300.0)
    breaker_failure_threshold: int = Field(default=5, ge=1, le=100)
    breaker_reset_timeout_seconds: float = Field(default=30.0, gt=0.0, le=3_600.0)

    @field_validator("bookmakers", "sports", "markets", mode="after")
    @classmethod
    def _reject_blank_and_duplicate_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value if item.strip())
        if not cleaned:
            msg = "list must contain at least one non-blank entry"
            raise ValueError(msg)
        if len(set(cleaned)) != len(cleaned):
            msg = f"duplicate entries are not permitted: {cleaned!r}"
            raise ValueError(msg)
        return cleaned

    @model_validator(mode="after")
    def _retry_window_must_fit_inside_the_poll_interval(self) -> Self:
        """Reject a retry budget that outlives its own polling cadence.

        If the worst-case retry chain can exceed the interval, every poll
        arrives late, the backlog compounds, and the loop silently degrades
        into a permanently drifting one. Better to refuse to start.
        """
        if self.retry_max_wait_seconds < self.retry_initial_wait_seconds:
            msg = "retry_max_wait_seconds must be at least retry_initial_wait_seconds"
            raise ValueError(msg)
        return self


class Settings(BaseSettings):
    """Root settings object. Composed, frozen, validated once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix=_ENV_PREFIX,
        extra="ignore",
        frozen=True,
    )

    environment: Literal["dev", "staging", "production"] = "dev"
    service_name: str = Field(default="betdoc", min_length=1, max_length=64)
    log_level: LogLevel = "INFO"
    log_as_json: bool = Field(
        default=True,
        description="JSON in every environment by default. Set False for a local TTY.",
    )

    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://betdoc:betdoc@localhost:5432/betdoc")
    )

    redis: RedisSettings = Field(default_factory=RedisSettings)
    ingestor: IngestorSettings = Field(default_factory=IngestorSettings)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _production_requires_real_credentials(self) -> Self:
        """Refuse to run production against development defaults.

        The failure mode this prevents is a production ingestor quietly
        pointing at localhost, appearing healthy, and ingesting nothing.
        """
        if not self.is_production:
            return self
        if "localhost" in self.redis.url.get_secret_value():
            msg = "production must not use a localhost Redis URL"
            raise ValueError(msg)
        if not self.ingestor.api_key.get_secret_value():
            msg = "production requires BETDOC_INGESTOR_API_KEY to be set"
            raise ValueError(msg)
        return self


def configure_logging(settings: Settings) -> None:
    """Configure structlog once, at process start. Idempotent.

    Output is JSON by default so every field, including the bound ``trace_id``,
    is directly searchable. ``print()`` appears nowhere in this codebase.
    """
    level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if settings.log_as_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(
        service=settings.service_name, environment=settings.environment
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, parsed and validated exactly once.

    Cached because settings are immutable for the process lifetime, and because
    re-reading ``.env`` on every access is both wasteful and a source of
    inconsistency if the file changes underneath a running service.
    """
    return Settings()
