from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlaceBetRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    idempotency_key: str = Field(..., min_length=1, max_length=128)
    exchange_name: str = Field(..., min_length=1)
    match_id: str = Field(..., min_length=1)
    market_type: str = Field(default="Match Odds")
    selection: str = Field(..., min_length=1)
    currency: str = Field(default="USD")
    odds: Decimal
    stake: Decimal = Field(..., gt=0)
    true_probability: Decimal = Field(default=Decimal("0.5"))

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.upper()


class PlaceBetResponse(BaseModel):
    bet_id: UUID
    status: str
