"""Pydantic V2 contracts for The Lab's quant toolkit (PANINI): dutching, matched betting, Avellaneda-Stoikov, cointegration."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.phantom.manager import CointegrationSignal, MatchedBettingMode

MAX_LEGS = 32  # keeps stakes/inputs within the 2048-char JSON columns

DecimalOdds = Annotated[float, Field(gt=1.0, allow_inf_nan=False)]


class PhantomSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


def _decode_json_fields(cls: type[BaseModel], data: Any, mapping: dict[str, str]) -> Any:
    """Map `<source>_json` string attributes onto decoded target fields for ORM objects or dicts."""
    if isinstance(data, Mapping):
        if not any(source in data for source in mapping):
            return data
        payload = {k: v for k, v in data.items() if k in cls.model_fields and k not in mapping.values()}
        raw_values = {target: data.get(source) for source, target in mapping.items()}
    elif all(hasattr(data, source) for source in mapping):
        payload = {name: getattr(data, name) for name in cls.model_fields if name not in mapping.values()}
        raw_values = {target: getattr(data, source) for source, target in mapping.items()}
    else:
        return data
    for target, raw in raw_values.items():
        try:
            payload[target] = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{target} could not be decoded from JSON.") from exc
    return payload


# --------------------------------------------------------------------------- shared reads


class PhantomCalculationLogRead(PhantomSchema):
    id: UUID
    calc_type: Literal["DUTCHING", "MATCHED_BETTING", "AVELLANEDA", "COINTEGRATION"]
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _decode_json(cls, data: Any) -> Any:
        return _decode_json_fields(cls, data, {"inputs_json": "inputs", "outputs_json": "outputs"})


# --------------------------------------------------------------------------- 1. dutching


class DutchingRequest(PhantomSchema):
    target_total_stake: float = Field(gt=0, allow_inf_nan=False)
    odds: list[DecimalOdds] = Field(min_length=2, max_length=MAX_LEGS)


class DutchingResponse(PhantomSchema):
    stakes: list[float]
    guaranteed_return: float
    total_implied_probability: float
    leg_returns: list[float]
    profit: float
    log: PhantomCalculationLogRead


# --------------------------------------------------------------------------- 2. matched betting


class MatchedBettingRequest(PhantomSchema):
    back_stake: float = Field(gt=0, allow_inf_nan=False)
    back_odds: float = Field(gt=1.0, allow_inf_nan=False)
    lay_odds: float = Field(gt=1.0, allow_inf_nan=False)
    lay_commission_pct: float = Field(ge=0, lt=100, allow_inf_nan=False)
    mode: MatchedBettingMode


class MatchedBettingResponse(PhantomSchema):
    mode: MatchedBettingMode
    lay_stake: float
    lay_liability: float
    back_win_profit: float
    lay_win_profit: float
    log: PhantomCalculationLogRead


# --------------------------------------------------------------------------- 3. Avellaneda-Stoikov


class MarketMakerRequest(PhantomSchema):
    mid_price: float = Field(gt=0, allow_inf_nan=False)
    inventory: float = Field(allow_inf_nan=False)
    gamma: float = Field(gt=0, allow_inf_nan=False)
    volatility_sigma: float = Field(ge=0, allow_inf_nan=False)
    time_horizon_t: float = Field(ge=0, allow_inf_nan=False)
    current_time_t: float = Field(ge=0, allow_inf_nan=False)
    liquidity_k: float = Field(gt=0, allow_inf_nan=False)


class MarketMakerResponse(PhantomSchema):
    time_remaining: float
    reservation_price: float
    optimal_spread: float
    optimal_ask: float
    optimal_bid: float
    inventory_skew: float
    log: PhantomCalculationLogRead


# --------------------------------------------------------------------------- 4. cointegration


class CointegrationRequest(PhantomSchema):
    current_z_score: float = Field(allow_inf_nan=False)
    entry_threshold: float = Field(allow_inf_nan=False)
    exit_threshold: float = Field(allow_inf_nan=False)
    stop_loss_threshold: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def _threshold_ordering(self) -> "CointegrationRequest":
        if not 0 <= self.exit_threshold < self.entry_threshold < self.stop_loss_threshold:
            raise ValueError("Thresholds must satisfy 0 <= exit_threshold < entry_threshold < stop_loss_threshold.")
        return self


class CointegrationResponse(PhantomSchema):
    signal: CointegrationSignal
    abs_z_score: float
    distance_to_stop_loss: float
    log: PhantomCalculationLogRead
