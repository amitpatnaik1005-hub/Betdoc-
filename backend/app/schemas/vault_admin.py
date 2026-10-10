"""Request bodies for the Vault & Fleet admin API (Group 70). Secrets arrive as SecretStr and are never echoed."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.core.fleet_overlay import MARKET_PROFILES

_SPORT_KEY = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_CURRENCY = re.compile(r"^[A-Z]{3,5}$")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccountSecretsIn(_Body):
    username: SecretStr | None = Field(default=None, max_length=255)
    password: SecretStr | None = Field(default=None, max_length=512)
    api_key: SecretStr | None = Field(default=None, max_length=2048)
    token: SecretStr | None = Field(default=None, max_length=4096)
    totp_seed: SecretStr | None = Field(default=None, max_length=256)
    notes: SecretStr | None = Field(default=None, max_length=4000)
    url: SecretStr | None = Field(default=None, max_length=2048)

    def plain(self) -> dict[str, str | None]:
        """The secrets that were sent: a value replaces, an empty string clears."""
        out: dict[str, str | None] = {}
        for name in self.model_fields_set & set(AccountSecretsIn.model_fields):
            value = getattr(self, name)
            text = value.get_secret_value().strip() if value is not None else ""
            out[name] = text or None
        return out


class AccountCreate(AccountSecretsIn):
    bookmaker: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    currency: str | None = Field(default=None, max_length=8)
    balance: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    stake_cap: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if not _CURRENCY.fullmatch(value):
            raise ValueError("currency must be a code like INR, USD, USDT")
        return value


class AccountUpdate(_Body):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    currency: str | None = Field(default=None, max_length=8)
    priority: int | None = Field(default=None, ge=1, le=1000)
    balance: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    stake_cap: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)
    is_active: bool | None = None
    secrets: AccountSecretsIn | None = None

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return AccountCreate._currency(value)  # type: ignore[arg-type]

    def changes(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.model_fields_set if name != "secrets" and (getattr(self, name) is not None or name in ("balance", "stake_cap"))}


class FleetConfigUpdate(_Body):
    sports: list[str] | None = Field(default=None, max_length=200)
    markets_by_sport: dict[str, str] | None = None  # sport key or "*" -> "h2h" | "full" | "h2h,totals,spreads"
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str | None = Field(default=None, max_length=64)
    account_routing: bool | None = None

    @field_validator("sports")
    @classmethod
    def _sports(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        keys = [v.strip().lower() for v in value if v.strip()]
        bad = [k for k in keys if not _SPORT_KEY.fullmatch(k)]
        if bad:
            raise ValueError(f"not sport keys like soccer_epl: {bad[:3]}")
        return list(dict.fromkeys(keys))

    @field_validator("markets_by_sport")
    @classmethod
    def _markets(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        out: dict[str, str] = {}
        for sport, spec in value.items():
            if sport != "*" and not _SPORT_KEY.fullmatch(sport):
                raise ValueError(f"{sport!r} is not a sport key")
            spec = MARKET_PROFILES.get(spec.strip().lower(), spec.strip().lower())
            if not set(spec.split(",")) <= {"h2h", "totals", "spreads"} or "h2h" not in spec.split(","):
                raise ValueError(f"{sport}: markets must be h2h, or h2h with totals / spreads")
            out[sport] = ",".join(m for m in ("h2h", "spreads", "totals") if m in spec.split(","))
        return out

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def _clock(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not _CLOCK.fullmatch(value):  # type: ignore[arg-type]
            raise ValueError("quiet hours are HH:MM (24h)")
        return value

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # noqa: PLC0415

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return value


class BackupExportRequest(_Body):
    passphrase: SecretStr = Field(min_length=12, max_length=512)
    current_password: SecretStr = Field(min_length=1, max_length=512)  # re-authentication: the whole vault leaves in this payload


class BackupRestoreRequest(_Body):
    backup: dict[str, Any]
    passphrase: SecretStr = Field(min_length=12, max_length=512)

    @model_validator(mode="after")
    def _size(self) -> BackupRestoreRequest:
        if len(str(self.backup.get("ciphertext", ""))) > 20_000_000:
            raise ValueError("backup too large")
        return self


class ProviderLinkRequest(_Body):
    force: bool = True  # replace the key Fleet Command runs on with this one


class ProviderToggle(_Body):
    is_active: bool
