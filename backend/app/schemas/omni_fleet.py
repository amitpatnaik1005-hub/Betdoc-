"""Fleet Command contracts: the Universal Ingestion Matrix's config, health, failover and providers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.adapters.ingestion.factory import SOURCE_ID_PATTERN, ProviderSpec

FleetStatus = Literal["HEALTHY", "DEGRADED", "TRIPPED", "QUOTA_RESERVE", "FATAL", "DISABLED", "NEEDS_KEY", "IDLE"]
FleetMode = Literal["celery", "inprocess", "offline"]
RESERVED_IDS = frozenset({"providers", "deadletter"})


class FailoverNoteRead(BaseModel):
    group: str
    replacing: str
    reason: str


class FleetSourceRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    display_name: str
    description: str
    docs_url: str | None
    kind: Literal["builtin", "config"]
    cost: Literal["free", "metered"]
    priority: int
    coverage: list[str]  # canonical market groups (sport keys)
    requires_api_key: bool
    is_enabled: bool
    status: FleetStatus
    # Failover routing
    role: Literal["always_on", "primary", "failover", "standby", "unavailable"]
    availability: str
    scope: list[str]  # groups this source is fetching right now
    covering: list[FailoverNoteRead]
    # Circuit breaker
    breaker_state: Literal["closed", "open", "half_open"]
    breaker_remaining_seconds: float | None
    # Credentials (write-only; only a masked hint or the variable's NAME is ever returned)
    has_api_key: bool
    api_key_hint: str | None
    key_origin: Literal["vault", "environment"] | None
    secret_env: str | None
    # Cadence, throttle, dead-letter
    interval_seconds: float
    default_interval_seconds: float
    rate_limit_rpm: float
    burst: int
    consecutive_failures: int
    failure_threshold: int
    paused_at: datetime | None
    last_error: str | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    # Health
    ping_ms: int | None
    success_rate: float | None
    runs_in_window: int
    ticks_last_run: int | None
    fixtures_last_run: int | None
    malformed_last_run: int | None
    throttled_ms: int | None
    devig: dict[str, int]
    unmapped: list[str]
    unmapped_count: int
    quota_remaining: float | None
    quota_used: float | None
    quota_limit: float | None
    quota_fraction: float | None
    runner: str | None
    spec: dict[str, Any] | None  # config providers only (contains no secrets)


class FleetGroupRead(BaseModel):
    group: str
    active: list[str]
    free: list[str]
    down: dict[str, str]
    failover: bool
    uncovered: bool


class FleetOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    mode: FleetMode  # who is running ingestion right now
    redis_available: bool
    vault_configured: bool
    board_cells: int | None  # live board cells updated within the snapshot TTL
    quota_reserve: float
    sources: list[FleetSourceRead]
    groups: list[FleetGroupRead]


class FleetSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool | None = None
    # null resets to the adapter's default cadence
    interval_seconds: float | None = Field(default=None, ge=5, le=86_400)


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


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    spec: ProviderSpec
    is_enabled: bool = False  # preview first, then switch it on


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ProviderSpec


class ProviderPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ProviderSpec
    sport: str = Field(min_length=2, max_length=64)  # a canonical key from spec.coverage
    sample: Any  # one response body, exactly as the provider returns it


class PreviewTick(BaseModel):
    match_id: str
    home_team: str
    away_team: str
    selection: str
    odds: float
    true_probability: float
    home_canonical: bool
    away_canonical: bool


class ProviderPreview(BaseModel):
    events_seen: int
    events_normalized: int
    malformed: int
    unmapped: list[str]
    devig: dict[str, int]
    ticks: list[PreviewTick]
