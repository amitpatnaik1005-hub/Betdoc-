from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bet_structures import AnyBetStructure
from app.schemas.math import ValueBetFlag
from app.schemas.risk import RiskMetrics


class OracleStrategyType(StrEnum):
    AGGRESSIVE_GROWTH = "AGGRESSIVE_GROWTH"
    BALANCED = "BALANCED"
    CAPITAL_PRESERVATION = "CAPITAL_PRESERVATION"
    RECOVERY = "RECOVERY"


class DynamicStrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_allowed_drawdown: float = Field(..., gt=0.0, le=1.0)      # fraction, matches KUMBHA current_drawdown
    target_win_rate: float = Field(..., ge=0.0, le=1.0)           # fraction
    base_kelly_fraction: float = Field(..., gt=0.0, le=1.0)       # 0.25 = quarter Kelly
    max_legs_per_combination: int = Field(..., ge=1, le=8)


class OracleContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_value_bets: list[ValueBetFlag] = Field(default_factory=list, max_length=500)
    risk_metrics: RiskMetrics
    strategy_params: DynamicStrategyParams
    bankroll: float = Field(..., gt=0.0, le=1e12)
    max_exposure_pct: float = Field(..., ge=0.0, le=100.0)        # percent, matches KUMBHA exposure_pct


class OracleSuggestion(BaseModel):
    structure: AnyBetStructure
    total_ev_pct: float
    capital_allocated: float
    rationale: str


class OracleResponse(BaseModel):
    suggestions: list[OracleSuggestion] = Field(default_factory=list)
    strategy_used: OracleStrategyType
    risk_temperature: float
    total_capital_deployed: float
