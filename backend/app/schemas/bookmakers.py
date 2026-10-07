"""Strict Pydantic V2 contracts for the bookmaker domain."""

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

BOOKMAKER_NAME_PATTERN: Final[str] = r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9])?$"
MAX_SECRET_LENGTH: Final[int] = 1024

BookmakerName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=BOOKMAKER_NAME_PATTERN
    ),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
DecimalOdds = Annotated[float, Field(gt=1.0, le=10_000.0, allow_inf_nan=False)]
StakeAmount = Annotated[float, Field(gt=0.0, le=1_000_000_000.0, allow_inf_nan=False)]
PriorityRank = Annotated[int, Field(ge=1, le=1_000_000)]


def _validate_secret(value: SecretStr | None) -> SecretStr | None:
    if value is None:
        return None
    raw = value.get_secret_value().strip()
    if not raw:
        raise ValueError("api_key_encrypted must not be blank.")
    if len(raw) > MAX_SECRET_LENGTH:
        raise ValueError(f"api_key_encrypted must be at most {MAX_SECRET_LENGTH} characters.")
    return SecretStr(raw)


class _StrictModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", allow_inf_nan=False)


class BookmakerConfigCreate(_StrictModel):
    name: BookmakerName
    is_active: bool = False
    api_key_encrypted: SecretStr | None = None
    base_url: AnyHttpUrl | None = None
    priority_rank: PriorityRank = 100

    @field_validator("api_key_encrypted")
    @classmethod
    def _check_secret(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_secret(value)


class BookmakerConfigUpdate(_StrictModel):
    name: BookmakerName | None = None
    is_active: bool | None = None
    api_key_encrypted: SecretStr | None = None
    base_url: AnyHttpUrl | None = None
    priority_rank: PriorityRank | None = None

    @field_validator("api_key_encrypted")
    @classmethod
    def _check_secret(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_secret(value)

    @model_validator(mode="after")
    def _validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided for an update.")
        nulls = sorted(
            field
            for field in self.model_fields_set & {"name", "is_active", "priority_rank"}
            if getattr(self, field) is None
        )
        if nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(nulls)}.")
        return self


class BookmakerConfigRead(_StrictModel):
    """Public representation. ``api_key_encrypted`` is intentionally absent."""

    id: uuid.UUID
    name: str
    is_active: bool
    base_url: str | None
    priority_rank: int
    has_api_key: bool
    created_at: datetime
    updated_at: datetime


class OddsComparisonRequest(_StrictModel):
    sport: NonEmptyText
    league: NonEmptyText
    match: NonEmptyText
    market: NonEmptyText
    selection: NonEmptyText
    odds: Annotated[dict[BookmakerName, DecimalOdds], Field(min_length=2, max_length=500)]
    execute: bool = False
    stake: StakeAmount | None = None

    @field_validator("odds")
    @classmethod
    def _reject_case_insensitive_duplicates(cls, value: dict[str, float]) -> dict[str, float]:
        seen: set[str] = set()
        for name in value:
            key = name.casefold()
            if key in seen:
                raise ValueError(f"Duplicate bookmaker in odds (case-insensitive): {name!r}.")
            seen.add(key)
        return value

    @model_validator(mode="after")
    def _require_stake_for_execution(self) -> Self:
        if self.execute and self.stake is None:
            raise ValueError("stake is required when execute is true.")
        return self


class OmniRouteExecuteRequest(_StrictModel):
    bookmaker_name: BookmakerName
    odds: DecimalOdds
    stake: StakeAmount
    market: NonEmptyText | None = None
    selection: NonEmptyText | None = None


class OmniRouteExecuteResponse(_StrictModel):
    status: Literal["SUCCESS", "FAILED"]
    bookmaker_name: str
    requested_odds: float
    executed_odds: float | None
    latency_ms: float
    transaction_id: str
    failure_reason: str | None


class OddsComparisonResponse(_StrictModel):
    best_bookmaker: str
    best_odds: float
    mean_odds: float
    edge_percentage: float
    placement_instruction: str
    considered_bookmakers: list[str]
    ignored_bookmakers: list[str]
    route: OmniRouteExecuteResponse | None = None
