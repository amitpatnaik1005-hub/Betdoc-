"""Pydantic V2 contracts for the Control Panel."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.control_panel import SystemSettingsModel

REDACTED = "***REDACTED***"

_COLUMNS = SystemSettingsModel.__table__.columns
SECRET_FIELDS: frozenset[str] = frozenset(c.name for c in _COLUMNS if c.name.endswith("_api_key"))
NON_NULLABLE_FIELDS: frozenset[str] = frozenset(c.name for c in _COLUMNS if not c.nullable)

ThemeLiteral = Literal["light", "dark", "auto"]
_HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")


class ControlPanelSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class SettingsRead(ControlPanelSchema):
    id: int
    developer_name: str
    app_version: str
    build_info: str
    theme: ThemeLiteral
    accent_color: str
    reduce_motion: bool
    odds_api_key: str | None
    news_api_key: str | None
    omniroute_url: str | None
    bots_enabled: bool
    research_frequency_minutes: int
    default_kelly_fraction: float
    global_stop_loss: float
    max_bet_size: float
    max_daily_exposure: float
    max_stake_pct: float
    last_emergency_stop_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SettingsUpdate(ControlPanelSchema):
    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    developer_name: str | None = Field(default=None, min_length=1, max_length=128)
    app_version: str | None = Field(default=None, min_length=1, max_length=32)
    build_info: str | None = Field(default=None, min_length=1, max_length=64)
    theme: ThemeLiteral | None = None
    accent_color: str | None = Field(default=None, max_length=16)
    reduce_motion: bool | None = None
    odds_api_key: str | None = Field(default=None, max_length=128)
    news_api_key: str | None = Field(default=None, max_length=128)
    omniroute_url: str | None = Field(default=None, max_length=255)
    bots_enabled: bool | None = None
    research_frequency_minutes: int | None = Field(default=None, ge=1, le=10_080)
    default_kelly_fraction: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    global_stop_loss: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    max_bet_size: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    max_daily_exposure: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    max_stake_pct: float | None = Field(default=None, ge=1.0, le=10.0, allow_inf_nan=False)

    @field_validator("odds_api_key", "news_api_key", "omniroute_url")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("accent_color")
    @classmethod
    def _hex_colour(cls, value: str | None) -> str | None:
        if value is not None and not _HEX_COLOUR.match(value):
            raise ValueError("accent_color must be a hex colour such as #3b82f6.")
        return value

    @field_validator("omniroute_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith(("http://", "https://")):
            raise ValueError("omniroute_url must start with http:// or https://.")
        return value

    @model_validator(mode="after")
    def _reject_nulls_for_required_columns(self) -> "SettingsUpdate":
        nulls = sorted(
            name for name in self.model_fields_set if name in NON_NULLABLE_FIELDS and getattr(self, name) is None
        )
        if nulls:
            raise ValueError(f"These settings cannot be null: {', '.join(nulls)}.")
        return self


# ---------------------------------------------------------------- Group 86: the emergency fortress (KAUTILYA, CHANAKYA & SHIVAJI)
LockoutModeLiteral = Literal["ARMED", "MANUAL_KILL", "AUTO_DRAWDOWN_TRIPPED", "STREAK_COOLDOWN"]


def _setting(name: str):  # noqa: ANN202 - a request default read from Settings when the request is parsed
    from app.core.config import get_settings  # noqa: PLC0415

    return lambda: getattr(get_settings(), name)


class EmergencyStateRead(ControlPanelSchema):
    id: int
    global_kill_engaged: bool
    lockout_mode: LockoutModeLiteral
    parimatch_locked: bool
    onexbet_locked: bool
    auto_kill_drawdown_pct: float
    auto_kill_window_hours: int
    max_consecutive_losses: int
    reference_bankroll_inr: float | None
    reference_bankroll_usd: float | None
    current_drawdown_inr: float
    current_drawdown_usd: float | None
    current_drawdown_pct: float | None
    current_consecutive_losses: int
    last_triggered_by: str | None
    last_trigger_reason: str | None
    cooldown_expires_at: datetime | None
    updated_at: datetime


class EmergencyAuditRead(ControlPanelSchema):
    id: UUID
    event_type: Literal["GLOBAL_KILL_ENGAGED", "GLOBAL_KILL_RESUMED", "AUTO_DRAWDOWN_TRIPPED", "STREAK_LOCKOUT_TRIPPED", "BOOKMAKER_LOCKED", "BOOKMAKER_UNLOCKED"]
    target_scope: Literal["GLOBAL", "PARIMATCH", "1XBET"]
    triggered_by: str
    reason: str
    drawdown_pct_snapshot: float | None
    protected_capital_inr: float | None
    protected_capital_usd: float | None
    prev_hash: str
    entry_hash: str
    created_at: datetime


class EmergencyDashboardResponse(ControlPanelSchema):
    state: EmergencyStateRead
    audits: list[EmergencyAuditRead]
    chain_intact: bool  # every entry's hash recomputes and links to the one before
    architect: str


class EmergencyTriggerRequest(ControlPanelSchema):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    triggered_by: str = Field(default="Amit Ashok Kumar Patnaik", min_length=1, max_length=128)
    reason: str = Field(default="Manual operator emergency kill switch engaged from Control Panel.", min_length=3, max_length=512)


class EmergencyResumeRequest(ControlPanelSchema):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    triggered_by: str = Field(default="Amit Ashok Kumar Patnaik", min_length=1, max_length=128)
    reason: str = Field(default="Operator verified market stability and resumed BetDoc operations.", min_length=3, max_length=512)
    restore_daily_exposure_inr: float = Field(default_factory=_setting("EMERGENCY_RESUME_DAILY_EXPOSURE_INR"), gt=0.0, allow_inf_nan=False)


class BookmakerLockRequest(ControlPanelSchema):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bookmaker: Literal["PARIMATCH", "1XBET"]
    locked: bool
    triggered_by: str = Field(default="Amit Ashok Kumar Patnaik", min_length=1, max_length=128)
    reason: str = Field(default="Bookmaker feed latency / margin anomaly lock toggled.", min_length=3, max_length=512)


class AutoDrawdownCheckRequest(ControlPanelSchema):
    model_config = ConfigDict(extra="forbid")

    reference_bankroll_inr: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)  # none: measured from the ledgers
    rolling_loss_inr: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    threshold_drawdown_pct: float = Field(default_factory=_setting("EMERGENCY_AUTO_KILL_DRAWDOWN_PCT"), ge=1.0, le=95.0, allow_inf_nan=False)
    window_hours: int = Field(default_factory=_setting("EMERGENCY_AUTO_KILL_WINDOW_HOURS"), ge=1, le=168)
    consecutive_losses: int | None = Field(default=None, ge=0, le=25)
    max_consecutive_losses: int = Field(default_factory=_setting("EMERGENCY_MAX_CONSECUTIVE_LOSSES"), ge=1, le=25)


class AutoDrawdownCheckResponse(ControlPanelSchema):
    tripped: bool
    lockout_mode: LockoutModeLiteral
    drawdown_pct: float | None
    threshold_drawdown_pct: float
    rolling_loss_inr: float
    rolling_loss_usd: float | None
    reference_bankroll_inr: float | None
    reference_bankroll_usd: float | None
    consecutive_losses: int
    max_consecutive_losses: int
    measured: bool  # every input came from the ledgers (none supplied by hand)
    verdict_summary: str
    state: EmergencyStateRead
