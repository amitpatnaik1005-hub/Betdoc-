from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

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

QuorumState = Literal["single_source", "consensus", "quarantined"]


class MarketTick(BaseModel):
    """One price on the live board (``/ws/live-odds``).

    The first eight fields are the original wire contract. The optional ones are filled by the
    Omni ingestion fleet: canonical BetDoc ids from the alias dictionary, where the price came
    from, and whether the probability is a cross-source consensus.
    """

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

    sport_key: str | None = Field(default=None, max_length=64)
    home_team_id: UUID | None = None
    away_team_id: UUID | None = None
    commence_time: datetime | None = None
    # Fleet source id ("odds_api", "polymarket", "ingest"), or "consensus" for a merged board tick
    source: str | None = Field(default=None, max_length=64)
    sources: tuple[str, ...] = ()
    source_event_id: str | None = Field(default=None, max_length=128)
    confidence: float | None = Field(default=None, ge=0, le=1)
    observed_at: datetime | None = None
    quorum: QuorumState | None = None

    @property
    def board_key(self) -> str:
        """One board cell: the same match/market/selection from every source lands here."""
        return f"{self.match_id}|{self.market_type}|{self.selection}"
