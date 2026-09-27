import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RiskMetrics(BaseModel):
    var_95: float = 0.0
    var_99: float = 0.0
    cvar_95: float = 0.0
    cvar_99: float = 0.0
    max_drawdown: float = 0.0          # fraction, 0.15 = 15%
    current_drawdown: float = 0.0      # fraction
    sharpe_ratio: float = 0.0          # per-bet, not annualized
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    omega_ratio: float = 0.0
    total_exposure: float = 0.0
    exposure_pct: float = 0.0          # percent of bankroll, 12.5 = 12.5%
    kelly_portfolio_fraction: float = 0.0
    total_bets: int = 0
    win_rate: float = 0.0              # fraction, excludes VOID


class StopLossConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    daily_loss_limit: float | None = Field(default=None, gt=0)
    consecutive_loss_limit: int | None = Field(default=None, ge=1, le=1000)
    trailing_stop_pct: float | None = Field(default=None, gt=0, le=100)  # percent
    cooldown_minutes: int | None = Field(default=None, ge=1, le=10080)
    session_loss_limit: float | None = Field(default=None, gt=0)
    enabled: bool = True


class StopLossStatus(BaseModel):
    is_triggered: bool
    trigger_reason: str | None = None
    daily_loss_current: float = 0.0
    consecutive_losses_current: int = 0
    cooldown_until: datetime | None = None
    session_loss_current: float = 0.0
    session_started_at: datetime | None = None


class StopLossEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    trigger_type: str
    trigger_value: float
    threshold: float
    triggered_at: datetime


class SessionResetOut(BaseModel):
    session_started_at: datetime
