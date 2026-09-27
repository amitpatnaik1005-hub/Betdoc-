from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer
from pydantic.alias_generators import to_camel

# Decimal is kept for validation, but sent as a JSON number on the wire. By default Pydantic
# sends Decimal as a string ("2.10"). The Group 15 frontend store checks
# Number.isFinite(tick.odds), which is false for a string, so every market would be
# auto-suspended.
WireDecimal = Annotated[
    Decimal,
    PlainSerializer(float, return_type=float, when_used="json"),
]


class MarketTick(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        frozen=True,
    )

    match_id: str = Field(min_length=1, max_length=128)
    home_team: str = Field(min_length=1, max_length=128)
    away_team: str = Field(min_length=1, max_length=128)
    market_type: Literal["Match Odds", "Over/Under 2.5", "Asian Handicap"]
    selection: str = Field(min_length=1)
    # allow_inf_nan=False: Decimal accepts NaN/Infinity by default, and those aren't valid JSON
    odds: Annotated[WireDecimal, Field(ge=0, allow_inf_nan=False)]
    true_probability: Annotated[WireDecimal, Field(ge=0, le=1, allow_inf_nan=False)]
    is_suspended: bool
