"""Pydantic V2 contracts shared by the service and API layers (no service imports => no cycles)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.domain.integration.models import BetStatus

SubsystemName = Literal["vault", "oracle", "arena", "lab"]


class _Schema(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class VaultMetrics(_Schema):
    kind: Literal["vault"] = "vault"
    credentials_stored: int = Field(ge=0)
    providers: int = Field(ge=0)


class OracleMetrics(_Schema):
    kind: Literal["oracle"] = "oracle"
    predictions_total: int = Field(ge=0)
    approved_predictions: int = Field(ge=0)
    automated_predictions_enabled: bool
    last_prediction_at: datetime | None


class ArenaMetrics(_Schema):
    kind: Literal["arena"] = "arena"
    open_bets: int = Field(ge=0)
    settled_bets: int = Field(ge=0)
    open_exposure: float = Field(ge=0.0)
    realized_pnl: float
    live_betting_enabled: bool


class LabMetrics(_Schema):
    kind: Literal["lab"] = "lab"
    settled_bets: int = Field(ge=0)
    hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    average_edge: float | None


SubsystemMetrics = Annotated[VaultMetrics | OracleMetrics | ArenaMetrics | LabMetrics, Field(discriminator="kind")]


class SubsystemStatus(_Schema):
    name: SubsystemName
    status: Literal["ok", "error"]
    latency_ms: float | None = Field(default=None, ge=0.0)
    last_error: str | None = None
    metrics: SubsystemMetrics | None = None


class TelemetryResponse(_Schema):
    generated_at: datetime
    overall_status: Literal["ok", "degraded", "down"]
    realized_pnl: float | None
    subsystems: list[SubsystemStatus]


class PredictionResult(_Schema):
    prediction_id: int = Field(validation_alias=AliasChoices("prediction_id", "id"))
    event_id: str
    model_name: str
    probability: float = Field(ge=0.0, le=1.0)
    implied_probability: float = Field(ge=0.0, le=1.0)
    edge: float
    decimal_odds: float = Field(gt=1.0)
    recommended_stake: float = Field(ge=0.0)
    approved: bool
    risk_reason: str
    created_at: datetime


class BetResult(_Schema):
    id: int
    prediction_id: int
    event_id: str
    stake: float = Field(gt=0.0)
    decimal_odds: float = Field(gt=1.0)
    status: BetStatus
    pnl: float | None
    placed_at: datetime
    settled_at: datetime | None


class ConstraintChange(_Schema):
    subsystem: str
    flag: str
    enabled: bool
    reason: str | None


class CascadeResult(_Schema):
    triggered: bool
    limit_breached: float
    changes: list[ConstraintChange]
    triggered_at: datetime


class StoredCredential(_Schema):
    id: int
    provider: str
    masked_key: str
