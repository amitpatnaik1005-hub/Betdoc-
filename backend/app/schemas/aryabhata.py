"""ARYABHATA contracts: market frames in, edges on the internal channel, TradeSignals out.

Decimals stay exact strings everywhere inside the system (Redis stream, signal channel). Only the
browser-facing ``TradeSignal`` and ``RiskConfigRead`` send them as JSON numbers, the same way
``MarketTick`` does, because the frontend works in numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.market import WireDecimal

SIGNAL_TTL_SECONDS = 15
SIGNAL_TTL = timedelta(seconds=SIGNAL_TTL_SECONDS)

DevigMethod = Literal["shin", "mpo", "multiplicative"]
# Which limit set the stake: the Kelly fraction itself, the bankroll % cap, Control Panel's
# absolute max bet, or a reason there is no stake at all
StakeBinding = Literal["kelly", "pct_cap", "max_bet", "halted", "no_bankroll", "no_edge"]

ExactDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


# ---------------------------------------------------------------- frames (ingestion -> engine)
class BookQuote(BaseModel):
    """One bookmaker's complete price set for a market: ``{selection: decimal odds}``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bookmaker_id: str = Field(min_length=1, max_length=64)
    prices: dict[str, ExactDecimal]
    observed_at: datetime | None = None
    is_suspended: bool = False


class MarketQuote(BaseModel):
    """Every book one source quotes for one market, as of one fetch (an Aryabhata stream frame).

    A frame replaces everything that source said about the market before: a book missing from it
    has stopped quoting, so its old price can never be the best line again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: str = Field(min_length=1, max_length=128)
    market_type: str = Field(min_length=1, max_length=64)
    home_team: str = Field(min_length=1, max_length=128)
    away_team: str = Field(min_length=1, max_length=128)
    sport_key: str | None = Field(default=None, max_length=64)
    commence_time: datetime | None = None
    source: str = Field(min_length=1, max_length=64)
    fetched_at: datetime
    books: tuple[BookQuote, ...] = ()

    @property
    def market_key(self) -> str:
        return f"{self.match_id}|{self.market_type}"


# ---------------------------------------------------------------- edges (engine -> every API worker)
class EdgeSignal(BaseModel):
    """A +EV line before any bankroll is applied. ``full_kelly`` is the uncapped Kelly fraction;
    each user's stake is sized from it at send time with the risk limits current at that moment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: UUID
    fixture_id: str
    market_id: UUID
    market_type: str
    selection: str
    home_team: str
    away_team: str
    sport_key: str | None = None
    commence_time: datetime | None = None
    bookmaker_id: str
    source: str
    odds: ExactDecimal
    true_prob: ExactDecimal
    ev: ExactDecimal
    ev_percent: ExactDecimal
    full_kelly: ExactDecimal
    devig_method: DevigMethod
    overround: ExactDecimal
    books: int = Field(ge=1)
    timestamp: datetime
    expires_at: datetime

    @property
    def key(self) -> str:
        """The dedupe key the Arena queue updates in place: one card per fixture and selection."""
        return f"{self.fixture_id}|{self.selection}"


# ---------------------------------------------------------------- browser contracts
class TradeSignal(BaseModel):
    """One +EV opportunity on ``/ws/signals``, staked for the user receiving it."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    signal_id: UUID
    fixture_id: str = Field(min_length=1, max_length=128)
    market_id: UUID
    selection: str = Field(min_length=1, max_length=64)
    odds: Annotated[WireDecimal, Field(gt=1, allow_inf_nan=False)]
    true_prob: Annotated[WireDecimal, Field(gt=0, lt=1, allow_inf_nan=False)]
    ev_percent: Annotated[WireDecimal, Field(allow_inf_nan=False)]
    kelly_stake_inr: Annotated[WireDecimal, Field(ge=0, allow_inf_nan=False)]
    bookmaker_id: str = Field(min_length=1, max_length=64)
    timestamp: datetime
    expires_at: datetime

    # Card context
    market_type: str
    home_team: str
    away_team: str
    sport_key: str | None = None
    commence_time: datetime | None = None
    source: str
    devig_method: DevigMethod
    overround: Annotated[WireDecimal, Field(allow_inf_nan=False)]
    books: int = Field(ge=1)
    stake_fraction: Annotated[WireDecimal, Field(ge=0, le=1, allow_inf_nan=False)]
    stake_binding: StakeBinding

    @model_validator(mode="after")
    def _ttl(self) -> TradeSignal:
        lifetime = self.expires_at - self.timestamp
        if not timedelta(0) < lifetime <= SIGNAL_TTL:
            raise ValueError(f"a signal lives between 0 and {SIGNAL_TTL_SECONDS}s")
        return self


class RiskConfigRead(BaseModel):
    """The limits every stake was sized with (sent with each snapshot so the Arena can show them)."""

    model_config = ConfigDict(frozen=True)

    kelly_multiplier: WireDecimal
    max_stake_pct: WireDecimal
    max_bet_size: WireDecimal | None
    halted: bool
