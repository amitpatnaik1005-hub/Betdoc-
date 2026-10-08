"""Pydantic V2 contracts for the Control Panel."""

import re
from datetime import datetime
from typing import Literal

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
