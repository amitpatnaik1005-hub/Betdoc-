import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SettlementStatus = Literal["WON", "LOST", "VOID", "HALF_WON", "HALF_LOST", "REJECTED", "CASH_OUT"]


class ActiveBetOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    exchange: str
    match_id: str
    market_type: str
    selection: str
    stake: float
    odds: float
    placed_at: datetime
    status: str
    strategy_name: str | None = None


class CashOutQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bet_id: uuid.UUID
    original_stake: float
    original_odds: float
    current_live_odds: float
    fair_value: float
    cash_out_offered: float
    margin_applied: float  # absolute amount deducted: fair_value - cash_out_offered


class SettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SettlementStatus
    payout: float | None = Field(default=None, allow_inf_nan=False)
    force_regrade: bool = False


class StrategyAnalyticsNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_name: str
    total_bets: int
    bets_won: int
    bets_lost: int
    bets_half_won: int = 0
    bets_half_lost: int = 0
    bets_void: int = 0
    bets_cashed_out: int = 0
    volume: float
    net_profit: float
    roi_pct: float


class ArenaFilterParams(BaseModel):
    """Parsed via Depends(); currency is validated in the engine (-> HTTP 400)."""
    model_config = ConfigDict(extra="forbid")

    currency: str = "INR"
