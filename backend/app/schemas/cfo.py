"""Pydantic V2 contracts for THE VAULT - CFO Advisory."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CfoSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class AdvisoryRequest(CfoSchema):
    current_bankroll: float = Field(gt=0, allow_inf_nan=False)
    active_exposure: float = Field(ge=0, allow_inf_nan=False)
    variance_threshold_pct: float = Field(ge=0, le=100, allow_inf_nan=False)


class AdvisoryResponse(CfoSchema):
    id: UUID
    capital_health_score: float
    variance_status: Literal["HIGH", "STABLE"]
    suggestions: list[str]
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _decode_suggestions_json(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            if "suggestions_json" not in data:
                return data
            payload = {k: v for k, v in data.items() if k in cls.model_fields and k != "suggestions"}
            raw = data["suggestions_json"]
        elif hasattr(data, "suggestions_json"):
            payload = {name: getattr(data, name) for name in cls.model_fields if name != "suggestions"}
            raw = data.suggestions_json
        else:
            return data
        try:
            payload["suggestions"] = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("suggestions_json is not valid JSON.") from exc
        return payload


class StressTestRequest(CfoSchema):
    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)

    scenario: str = Field(min_length=1, max_length=64)
    portfolio_value: float = Field(gt=0, allow_inf_nan=False)
    shock_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    survival_threshold_pct: float = Field(ge=0, le=100, allow_inf_nan=False)


class StressTestResult(CfoSchema):
    id: UUID
    scenario_name: str
    portfolio_value_before: float
    simulated_pnl: float
    simulated_drawdown_pct: float
    survived: bool
    recommendation: str
    created_at: datetime


class TaxCalculateRequest(CfoSchema):
    year: int = Field(ge=1900, le=9999)
    total_profit: float = Field(allow_inf_nan=False)
    tax_allowance: float = Field(ge=0, allow_inf_nan=False)
    tax_rate_pct: float = Field(ge=0, le=100, allow_inf_nan=False)


class TaxRecordRead(CfoSchema):
    id: UUID
    year: int
    total_profit: float
    taxable_amount: float
    estimated_tax: float
    last_calculated_at: datetime


class AlertRead(CfoSchema):
    id: UUID
    level: Literal["INFO", "WARNING", "CRITICAL"]
    message: str
    is_read: bool
    created_at: datetime
