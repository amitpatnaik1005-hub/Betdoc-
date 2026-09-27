from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PnLTimeframe(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


class VaultFilterParams(BaseModel):
    """Parsed from query params via Depends(). Cross-field checks live in the engine
    (raised as VaultQueryError -> HTTP 400) because model errors inside a dependency
    would surface as HTTP 500."""
    model_config = ConfigDict(extra="forbid")

    currency: str = "INR"
    start_date: datetime | None = None
    end_date: datetime | None = None


class CapitalOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_currency: str
    starting_bankroll: float
    total_exposure: float
    available_capital: float
    gross_profit: float
    gross_loss: float
    net_pnl: float
    total_volume: float
    roi_pct: float
    active_bets_count: int
    settled_bets_count: int


class TimeframePnLNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: datetime
    profit: float
    volume: float
    yield_pct: float
    bets_won: int
    bets_lost: int


class WaterfallNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    value: float


class MarketPnL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_type: str
    net_profit: float
    volume: float
    roi_pct: float


class BookmakerPnL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange: str
    net_profit: float
    volume: float
    roi_pct: float


class GrowthNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    actual_bankroll: float
    projected_bankroll: float
    cumulative_ev: float

