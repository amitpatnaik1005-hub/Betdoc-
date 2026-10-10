"""Request bodies of Ashoka's API (Group 69): placed bets, scores, cashout and odds checks."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.oracle.markets import parse_market
from app.models.user_bets_ledger import PlacedBookmaker, PlacedStructure, ScoreStatus

LEGS_FOR = {
    PlacedStructure.SINGLE: (1, 1),
    PlacedStructure.DOUBLE: (2, 2),
    PlacedStructure.TREBLE: (3, 3),
    PlacedStructure.ACCUMULATOR: (4, 20),
    PlacedStructure.TRIXIE: (3, 3),
    PlacedStructure.YANKEE: (4, 4),
    PlacedStructure.CANADIAN: (5, 5),
    PlacedStructure.HEINZ: (6, 6),
    PlacedStructure.PATENT: (3, 3),
    PlacedStructure.SUPER_HEINZ: (7, 7),
    PlacedStructure.GOLIATH: (8, 8),
}
SYSTEM_STRUCTURES = frozenset({PlacedStructure.TRIXIE, PlacedStructure.YANKEE, PlacedStructure.CANADIAN, PlacedStructure.HEINZ, PlacedStructure.PATENT,
                               PlacedStructure.SUPER_HEINZ, PlacedStructure.GOLIATH})


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlacedLegIn(_Body):
    fixture_id: str | None = Field(default=None, max_length=128)  # Ashoka's id; omitted for a hand-typed leg
    home: str = Field(min_length=1, max_length=128)
    away: str = Field(min_length=1, max_length=128)
    sport_key: str | None = Field(default=None, max_length=64)
    league: str | None = Field(default=None, max_length=64)
    kickoff: datetime | None = None
    market: str = Field(min_length=1, max_length=64)  # "Match Odds", "Totals 2.5", "BTTS", "Asian Handicap -0.25"
    selection: str = Field(min_length=1, max_length=16)
    odds: Decimal = Field(ge=Decimal("1.01"), le=Decimal("1000"))
    fair_probability: float | None = Field(default=None, gt=0, lt=1)

    @model_validator(mode="after")
    def _market(self) -> PlacedLegIn:
        ref = parse_market(self.market)
        if ref is None:
            raise ValueError(f"market {self.market!r} is not one Ashoka settles (1X2, totals, BTTS, Asian handicap, double chance, draw no bet)")
        self.market = ref.key
        self.selection = self.selection.strip().upper()
        if self.selection not in ref.selections:
            raise ValueError(f"{self.selection!r} is not a selection of {ref.key} ({', '.join(ref.selections)})")
        return self


class PlaceBetRequest(_Body):
    slip_id: str | None = Field(default=None, max_length=32)
    source: Literal["ASHOKA", "MANUAL"] = "ASHOKA"
    bookmaker: PlacedBookmaker
    bookmaker_name: str | None = Field(default=None, max_length=64)
    structure: PlacedStructure
    stake_inr: Decimal = Field(gt=0, le=Decimal("10000000"))  # the total; a system's unit stake is derived
    placed_odds: Decimal | None = Field(default=None, ge=Decimal("1.01"), le=Decimal("1000000"))
    placed_at: datetime | None = None
    legs: list[PlacedLegIn] = Field(min_length=1, max_length=20)
    notes: str | None = Field(default=None, max_length=300)

    @field_validator("stake_inr")
    @classmethod
    def _paise(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))

    @model_validator(mode="after")
    def _shape(self) -> PlaceBetRequest:
        low, high = LEGS_FOR[self.structure]
        if not low <= len(self.legs) <= high:
            raise ValueError(f"a {self.structure.lower()} has {low if low == high else f'{low} to {high}'} leg(s), got {len(self.legs)}")
        if self.structure in SYSTEM_STRUCTURES and self.placed_odds is not None:
            raise ValueError("a system bet has no single price: give the legs' odds only")
        if self.bookmaker is PlacedBookmaker.OTHER and not self.bookmaker_name:
            raise ValueError("name the bookmaker when it is OTHER")
        return self


class ScoreIn(_Body):
    fixture_id: str | None = Field(default=None, max_length=128)
    home: str = Field(min_length=1, max_length=128)
    away: str = Field(min_length=1, max_length=128)
    sport_key: str | None = Field(default=None, max_length=64)
    kickoff: datetime | None = None
    home_goals: int | None = Field(default=None, ge=0, le=2000)  # goals, points or runs (Group 70: every sport)
    away_goals: int | None = Field(default=None, ge=0, le=2000)
    status: ScoreStatus = ScoreStatus.FINAL

    @model_validator(mode="after")
    def _goals(self) -> ScoreIn:
        if self.status is ScoreStatus.FINAL and (self.home_goals is None or self.away_goals is None):
            raise ValueError("a final score needs both teams' goals")
        return self


class CashoutRequest(_Body):
    offer_inr: Decimal | None = Field(default=None, ge=0)  # the bookmaker's cashout offer, as shown on the slip
    bankroll_inr: Decimal | None = Field(default=None, gt=0)
    # per open leg (by position): the current probability, if the feeds do not price it
    probabilities: dict[int, float] = Field(default_factory=dict)
    lay_odds: dict[int, float] = Field(default_factory=dict)  # Betfair lay price of an open leg, if known


class CashoutRecord(_Body):
    cashout_inr: Decimal = Field(ge=0)  # the user took the bookmaker's cashout for this much


class OddsCheckLeg(_Body):
    home: str = Field(min_length=1, max_length=128)
    away: str = Field(min_length=1, max_length=128)
    market: str = Field(min_length=1, max_length=64)
    selection: str = Field(min_length=1, max_length=16)
    prices: dict[str, float] = Field(min_length=1)  # bookmaker -> the odds the user sees on the site


class OddsCheckRequest(_Body):
    stake_inr: Decimal = Field(default=Decimal("1000"), gt=0, le=Decimal("10000000"))
    legs: list[OddsCheckLeg] = Field(min_length=1, max_length=20)


class SlipLegsRequest(_Body):
    """A slip Ashoka produced, sent back to be re-priced against the latest feeds."""

    leg_ids: list[str] = Field(min_length=1, max_length=20)  # "<fixture>|<market>|<selection>"
    kind: str | None = None
    bankroll_inr: Decimal | None = Field(default=None, gt=0)
