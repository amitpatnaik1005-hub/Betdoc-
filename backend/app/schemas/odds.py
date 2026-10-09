from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OddsSelection(BaseModel):
    model_config = ConfigDict(populate_by_name=True, strict=False)

    name: str = Field(alias="name")
    price: float = Field(alias="price")
    point: float | None = Field(default=None, alias="point")  # totals: the goal line; spreads: that side's handicap


class OddsMarket(BaseModel):
    model_config = ConfigDict(populate_by_name=True, strict=False)

    key: str = Field(alias="key")
    # Optional: some bookmakers omit market-level last_update. A required field
    # would reject the entire event.
    last_update: datetime | None = Field(default=None, alias="last_update")
    selections: list[OddsSelection] = Field(alias="outcomes")


class OddsBookmaker(BaseModel):
    model_config = ConfigDict(populate_by_name=True, strict=False)

    key: str = Field(alias="key")
    title: str = Field(alias="title")
    last_update: datetime = Field(alias="last_update")
    markets: list[OddsMarket] = Field(alias="markets")


class NormalizedMatchOdds(BaseModel):
    model_config = ConfigDict(populate_by_name=True, strict=False)

    id: str = Field(alias="id")
    sport_key: str = Field(alias="sport_key")
    commence_time: datetime = Field(alias="commence_time")
    home_team: str = Field(alias="home_team")
    away_team: str = Field(alias="away_team")
    bookmakers: list[OddsBookmaker] = Field(alias="bookmakers")


class OddsMovement(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bookmaker: str
    market_type: str
    selection: str
    odds: float
    timestamp: datetime
