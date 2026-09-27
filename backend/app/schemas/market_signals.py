from datetime import datetime, timedelta, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_FUTURE_SKEW = timedelta(minutes=5)


class OddsType(StrEnum):
    BACK = "BACK"
    LAY = "LAY"


class OddsTick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bookmaker_id: str = Field(..., min_length=1, max_length=64)
    match_id: str = Field(..., min_length=1, max_length=128)
    selection_id: str = Field(..., min_length=1, max_length=128)
    market_type: str = Field(..., min_length=1, max_length=64)
    odds_type: OddsType
    decimal_odds: float = Field(..., ge=1.0, le=1000.0, allow_inf_nan=False)
    line: float | None = Field(default=None, ge=-1000.0, le=100_000.0, allow_inf_nan=False)
    timestamp: datetime
    is_sharp: bool = False

    @field_validator("line")
    @classmethod
    def _normalize_line(cls, v: float | None) -> float | None:
        return None if v is None else round(v, 4)

    @field_validator("timestamp")
    @classmethod
    def _utc_and_not_future(cls, v: datetime) -> datetime:
        v = v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)
        if v > datetime.now(timezone.utc) + MAX_FUTURE_SKEW:
            raise ValueError("Tick timestamp is too far in the future")
        return v


class SteamAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: str
    selection_id: str
    market_type: str
    opening_odds: float
    current_odds: float
    opening_line: float | None
    current_line: float | None
    implied_prob_delta_pct: float          # percentage points, positive = shortening
    triggering_bookmakers: list[str]
    detected_at: datetime


class LineShopResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: str
    selection_id: str
    best_back_odds: float
    best_back_bookmaker: str
    true_odds_consensus: float
    ev_pct: float


class ArbitrageOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: str
    selection_id: str
    back_bookmaker: str
    back_odds: float
    lay_bookmaker: str
    lay_odds: float
    exchange_commission_pct: float         # fraction, 0.02 = 2%
    net_profit_pct: float                  # percent of back stake


class SurebetLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_id: str
    bookmaker_id: str
    odds: float
    recommended_stake_pct: float           # fraction of total outlay; legs sum to 1.0


class MarketSurebet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: str
    market_type: str
    line: float | None
    legs: list[SurebetLeg]
    profit_pct: float                      # guaranteed return on total outlay, percent


class TickIngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingested: int
    steam_detection_scheduled: bool
