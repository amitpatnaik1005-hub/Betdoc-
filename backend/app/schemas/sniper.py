"""Contracts for the Omni-Sniper API: venues, credentials, mappings, the terminal, executions, the DLQ."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.cfo_vault import AuditEvent
from app.schemas.market import WireDecimal

VENUE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
PATH = re.compile(r"^/[A-Za-z0-9_\-./{}]*$")


class VenueSession(BaseModel):
    authenticated: bool
    expires_at: datetime | None = None
    seconds_left: int | None = None


class VenueRead(BaseModel):
    id: str
    display_name: str
    adapter: str
    base_url: str
    auth_type: str
    bets_per_second: WireDecimal
    burst: int
    routes: list[str]
    is_enabled: bool
    is_sandbox: bool
    has_credentials: bool
    credentials_hint: str | None
    fixtures_mapped: int
    session: VenueSession


class VenueUpsert(BaseModel):
    """A real bookmaker's execution API (admin). Its credentials are set separately, write-only."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    adapter: Literal["generic_json"] = "generic_json"
    base_url: str = Field(min_length=8, max_length=255)
    auth_type: Literal["oauth2_client_credentials", "static_bearer"] = "oauth2_client_credentials"
    token_path: str | None = Field(default="/oauth/token", max_length=128)
    refresh_path: str | None = Field(default=None, max_length=128)
    place_path: str = Field(default="/bets", max_length=128)
    status_path: str = Field(default="/bets", max_length=128)
    events_path: str | None = Field(default=None, max_length=128)
    bets_per_second: Annotated[Decimal, Field(gt=0, le=50, allow_inf_nan=False)] = Decimal("2")
    burst: Annotated[int, Field(ge=1, le=50)] = 2
    routes: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=50)
    selection_codes: dict[str, Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=dict)
    is_enabled: bool = True

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not VENUE_ID.match(value) or value == "sandbox":
            raise ValueError("lowercase letters, digits and _, starting with a letter ('sandbox' is reserved)")
        return value

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.lower().startswith("https://"):
            raise ValueError("an execution venue must be https")
        return value.rstrip("/")

    @field_validator("token_path", "refresh_path", "place_path", "status_path", "events_path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        if value is not None and not PATH.match(value):
            raise ValueError("paths are relative to base_url and start with /")
        return value

    @field_validator("routes")
    @classmethod
    def _routes(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("only the sandbox venue may route every bookmaker")
        return value

    @field_validator("selection_codes")
    @classmethod
    def _codes(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) - {"HOME", "DRAW", "AWAY"}:
            raise ValueError("selection_codes keys are HOME, DRAW, AWAY")
        return value

    @model_validator(mode="after")
    def _token_path(self) -> VenueUpsert:
        if self.auth_type == "oauth2_client_credentials" and not self.token_path:
            raise ValueError("oauth2_client_credentials needs a token_path")
        return self


class VenueCredentials(BaseModel):
    """Write-only. Encrypted with MASTER_VAULT_KEY before it touches the database."""

    model_config = ConfigDict(extra="forbid")

    client_id: str | None = Field(default=None, min_length=1, max_length=256)
    client_secret: str | None = Field(default=None, min_length=8, max_length=512)
    api_key: str | None = Field(default=None, min_length=8, max_length=512)

    @model_validator(mode="after")
    def _one_kind(self) -> VenueCredentials:
        oauth = self.client_id is not None and self.client_secret is not None
        if oauth == (self.api_key is not None):
            raise ValueError("give client_id + client_secret, or api_key")
        return self


class MappingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["fixture", "selection"]
    canonical_key: str = Field(min_length=1, max_length=256)
    remote_id: str = Field(min_length=1, max_length=128)


class CatalogSyncRead(BaseModel):
    venue_id: str
    events: int
    mapped: int
    unresolved: list[str]


class FeedLine(BaseModel):
    ts: datetime
    step: str
    message: str
    level: str
    ref: str | None = None
    bookmaker: str | None = None


class ExecutionRead(BaseModel):
    """One execution attempt's outcome with exactly what was sent and received."""

    id: UUID
    event: AuditEvent
    reason: str
    idempotency_key: UUID | None
    ledger_id: UUID | None
    fixture_id: str | None
    selection: str | None
    stake_inr: WireDecimal | None
    odds: WireDecimal | None
    created_at: datetime
    venue_id: str | None = None
    remote_bet_id: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    matched_odds: str | None = None
    request_payload: dict[str, Any] | None = None
    response_payload: Any = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["WON", "LOST", "VOID", "NOT_PLACED", "OPEN"]
    remote_bet_id: str | None = Field(default=None, min_length=1, max_length=128)
