from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

MarketType = Literal["Match Odds", "Over/Under 2.5", "Asian Handicap"]


class MarketTick(BaseModel):
    # camelCase on the wire matches the backend MarketTick aliases and the frontend store.
    # The same limits as the backend mean a bad tick fails here, not as a 422 on the server
    # that rejects the whole batch.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        frozen=True,
    )

    match_id: str = Field(min_length=1, max_length=128)
    home_team: str = Field(min_length=1, max_length=128)
    away_team: str = Field(min_length=1, max_length=128)
    market_type: MarketType
    odds: float = Field(ge=0, allow_inf_nan=False)
    true_probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    is_suspended: bool
