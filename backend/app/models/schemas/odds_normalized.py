from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OddsSelection(BaseModel):
    """Represents a single outcome (e.g., 'Yes', 'No', 'Home Team', 'Draw')"""
    name: str = Field(description="Name of the outcome")
    price: float = Field(description="Decimal odds for this outcome")
    size: Optional[float] = Field(default=None, description="Available liquidity or volume at this price")

    @field_validator("price", mode="before")
    @classmethod
    def convert_to_decimal(cls, v: any) -> float:
        if v is None:
            return 0.0
        try:
            val = float(v)
            if 0 < val < 1.0:
                # Prediction market probability to decimal odds (e.g. 0.50 -> 2.0)
                return round(1.0 / val, 3)
            elif val <= 0 or val == 1.0:
                # 0 or 1 probabilities mean the market is locked/resolved. 
                # Cap it at a safe flat value to prevent ZeroDivisionError.
                return 1.0
            return round(val, 3)
        except (ValueError, TypeError):
            return 0.0


class OrderBookTier(BaseModel):
    """Represents a single price level in an exchange (Betfair/Polymarket)"""
    price: float
    size: float


class OrderBook(BaseModel):
    """Deep liquidity data for exchange/prediction markets"""
    bids: List[OrderBookTier] = Field(default_factory=list, description="Buy orders / Back liquidity")
    asks: List[OrderBookTier] = Field(default_factory=list, description="Sell orders / Lay liquidity")


class NormalizedOdds(BaseModel):
    """
    Law 1: The schema is immutable. All scrapers (API or Headless) 
    must conform their output exactly to this structure before pushing to Redis.
    """
    model_config = ConfigDict(populate_by_name=True, strict=False)

    match_id: str = Field(description="Unique identifier from the provider")
    provider: str = Field(description="e.g., 'kalshi', 'polymarket', 'stake', 'betfair'")
    timestamp: datetime = Field(description="Source timestamp of the odds")
    market_type: str = Field(description="Enum: moneyline, over_under, binary_prediction, exchange")
    
    # Flexible architecture replacing rigid home/away
    selections: List[OddsSelection] = Field(
        default_factory=list, 
        description="Standard sportsbook odds or top-of-book prediction prices"
    )
    
    # Advanced depth architecture for Betfair / Polymarket
    order_book: Optional[OrderBook] = Field(
        default=None, 
        description="Deep order book data for exchanges"
    )
    
    total_matched_volume: Optional[float] = Field(
        default=None, 
        description="Total volume traded on the market"
    )

    @field_validator("provider", "market_type", mode="before")
    @classmethod
    def sanitize_strings(cls, v: str) -> str:
        if not isinstance(v, str):
            return str(v).strip().lower()
        return v.strip().lower()
