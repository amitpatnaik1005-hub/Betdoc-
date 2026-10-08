"""Fleet Command contracts: ingestion source config, live health, dead letters."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

FleetStatus = Literal["HEALTHY", "DEGRADED", "FATAL", "DISABLED", "NEEDS_KEY", "IDLE"]
FleetMode = Literal["celery", "inprocess", "offline"]


class FleetSourceRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    display_name: str
    description: str
    docs_url: str | None
    requires_api_key: bool
    is_enabled: bool
    status: FleetStatus
    has_api_key: bool
    api_key_hint: str | None  # pre-masked at write time; the key itself is never returned
    key_origin: Literal["vault", "environment"] | None
    interval_seconds: float
    default_interval_seconds: float
    consecutive_failures: int
    failure_threshold: int
    paused_at: datetime | None
    last_error: str | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    ping_ms: int | None  # mean request round-trip of the last successful run
    success_rate: float | None  # over the last runs_in_window runs
    runs_in_window: int
    ticks_last_run: int | None
    fixtures_last_run: int | None
    unmapped: list[str]  # names missing from the alias dictionary (provisional ids were used)
    unmapped_count: int
    quota_remaining: float | None
    runner: str | None


class FleetOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    mode: FleetMode  # who is running ingestion right now
    redis_available: bool
    vault_configured: bool
    board_cells: int | None  # live board cells updated within the snapshot TTL
    sources: list[FleetSourceRead]


class FleetSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool | None = None
    # null resets to the adapter's default cadence
    interval_seconds: float | None = Field(default=None, ge=5, le=3600)


class FleetApiKeyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=8, max_length=512)


class FleetRunAccepted(BaseModel):
    source_id: str
    dispatched_to: Literal["celery", "manual"]


class FleetDeadLetter(BaseModel):
    source_id: str
    status: str
    failures: int
    error: str
    runner: str | None = None
    at: datetime
