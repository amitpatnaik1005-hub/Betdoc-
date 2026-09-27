import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class BetRead(BaseModel):
    # Decimals serialize as JSON strings (e.g. "2.1000") so no precision is lost in transit
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    idempotency_key: str
    exchange_account_id: uuid.UUID
    exchange_bet_id: str | None
    match_id: str
    market_type: str
    selection: str
    currency: str
    odds: Decimal
    stake: Decimal
    true_probability: Decimal
    status: str
    placed_at: datetime
    resolved_at: datetime | None
    payout: Decimal | None
