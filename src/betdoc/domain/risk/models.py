from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# FIXING IMPORTS FOR OUR ARCHITECTURE
from betdoc.domain.intelligence.advisor_models import MarketOpportunity, RejectionReason
from betdoc.domain.shared.money import BPS_DENOMINATOR


class RiskVerdict(str, Enum):
    APPROVED = "approved"
    APPROVED_REDUCED = "approved_reduced"
    REJECTED = "rejected"

class StakeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity: MarketOpportunity
    requested_stake_paise: int = Field(gt=0)
    instrument_key: str = Field(min_length=1, max_length=256)

class ConstraintBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    single_stake_cap_paise: int = Field(ge=0)
    sport_exposure_headroom_paise: int = Field(ge=0)
    daily_loss_headroom_paise: int = Field(ge=0)
    settled_liquidity_paise: int = Field(ge=0)

class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: RiskVerdict
    approved_stake_paise: int = Field(ge=0)
    requested_stake_paise: int = Field(gt=0)
    rejection_reason: RejectionReason | None
    binding_constraint: RejectionReason | None
    detail: str
    expected_value_bps: int
    constraints: ConstraintBreakdown
    decided_at: datetime

    @property
    def is_actionable(self) -> bool:
        return self.approved_stake_paise > 0

@dataclass(frozen=True, slots=True)
class RiskPolicy:
    min_expected_value_bps: int = 50
    daily_budget_utilisation_bps: int = 2_500
    min_viable_stake_paise: int = 5_000
    max_quote_age: timedelta = timedelta(seconds=5)
    allow_partial_downsizing: bool = True
    require_volatility_signal: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.daily_budget_utilisation_bps <= BPS_DENOMINATOR:
            raise ValueError("daily_budget_utilisation_bps must be within [1, 10000]")
        if self.min_viable_stake_paise <= 0:
            raise ValueError("min_viable_stake_paise must be positive")
        if self.max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be a positive duration")
