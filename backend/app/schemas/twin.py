"""The twin's evidence and request bodies (Group 72).

Evidence ("intel") is what the fortress's context pillars read, per fixture, one section per kind:
weather, travel, injuries, lineups, referee, motivation, public splits, liquidity. Every section says
where it came from (``source``: the feed, or who typed it) and when it was observed; a section older than
``TWIN_INTEL_MAX_AGE_MINUTES[section]`` counts as missing, and a pillar without evidence is UNVERIFIED.
Nothing here is ever estimated: a section is written by a feed or by an administrator, or it is absent.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.user_bets_ledger import PlacedBookmaker

Side = Literal["HOME", "AWAY"]
INTEL_SECTIONS = ("weather", "travel", "injuries", "lineups", "referee", "motivation", "public_splits", "liquidity")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Section(_Body):
    source: str = Field(min_length=1, max_length=64)  # "openweathermap", "admin:<name>", "club statement"
    observed_at: datetime


class WeatherIntel(_Section):
    indoor: bool = False  # a roof (or an indoor sport): the weather cannot touch the match
    wind_kmh: float | None = Field(default=None, ge=0, le=400)
    precipitation_mmh: float | None = Field(default=None, ge=0, le=500)
    temperature_c: float | None = Field(default=None, ge=-60, le=60)
    altitude_m: float | None = Field(default=None, ge=-500, le=6000)
    dew_expected: bool | None = None

    @model_validator(mode="after")
    def _outdoor(self) -> WeatherIntel:
        if not self.indoor and (self.wind_kmh is None or self.precipitation_mmh is None):
            raise ValueError("an outdoor reading needs wind_kmh and precipitation_mmh")
        return self


class TeamTravel(_Body):
    flight_delay_hours: float = Field(default=0.0, ge=0, le=72)
    rest_hours: float = Field(ge=0, le=24 * 60)  # since the side's previous fixture
    timezones_crossed: int = Field(default=0, ge=0, le=12)


class TravelIntel(_Section):
    home: TeamTravel
    away: TeamTravel


class Absence(_Body):
    side: Side
    player: str = Field(min_length=1, max_length=96)
    impact: float = Field(ge=0, le=1)  # the player impact score: 1 is irreplaceable
    status: Literal["OUT", "DOUBTFUL"]


class InjuryIntel(_Section):
    absences: list[Absence] = Field(default_factory=list, max_length=60)
    manager_changed_at: dict[Side, datetime] = Field(default_factory=dict)  # a sacking or appointment, per side


class LineupIntel(_Section):
    home_confirmed: bool
    away_confirmed: bool
    published_at: datetime | None = None


class RefereeIntel(_Section):
    name: str = Field(min_length=1, max_length=96)
    cards_per_game: float = Field(ge=0, le=20)
    penalties_per_90: float = Field(ge=0, le=5)
    matches: int | None = Field(default=None, ge=0)


class MotivationIntel(_Section):
    home: float = Field(ge=0, le=1)  # what the result is worth to each side (relegation fight 1, dead rubber 0)
    away: float = Field(ge=0, le=1)
    derby: bool = False
    notes: str | None = Field(default=None, max_length=300)


class SelectionSplit(_Body):
    tickets_pct: float = Field(ge=0, le=1)
    money_pct: float | None = Field(default=None, ge=0, le=1)


class PublicSplitsIntel(_Section):
    """Measured ticket and money shares (a named feed's figures, never an estimate), and opening prices."""

    splits: dict[str, dict[str, SelectionSplit]] = Field(default_factory=dict)  # market key -> selection -> split
    opening_odds: dict[str, dict[str, float]] = Field(default_factory=dict)  # market key -> selection -> opening decimal odds

    @field_validator("opening_odds")
    @classmethod
    def _odds(cls, value: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        if any(price <= 1 for prices in value.values() for price in prices.values()):
            raise ValueError("opening odds are decimal odds above 1")
        return value


class LiquidityIntel(_Section):
    max_stake_inr: dict[str, Decimal] = Field(default_factory=dict)  # canonical bookmaker -> the most it takes on this fixture

    @field_validator("max_stake_inr")
    @classmethod
    def _positive(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        if any(v <= 0 for v in value.values()):
            raise ValueError("a maximum stake is positive")
        return {k.strip().casefold(): v for k, v in value.items()}


class FixtureIntel(_Body):
    """Any subset of the sections: a write replaces only the sections it carries."""

    weather: WeatherIntel | None = None
    travel: TravelIntel | None = None
    injuries: InjuryIntel | None = None
    lineups: LineupIntel | None = None
    referee: RefereeIntel | None = None
    motivation: MotivationIntel | None = None
    public_splits: PublicSplitsIntel | None = None
    liquidity: LiquidityIntel | None = None


class VetRequest(_Body):
    leg_ids: list[str] = Field(min_length=1, max_length=20)  # "<fixture>|<market>|<selection>", as Ashoka's slips carry them
    kind: str | None = None
    bankroll_inr: Decimal | None = Field(default=None, gt=0)


class LedgerFromAudit(_Body):
    """The user placed the audited slip: where, for how much, at what price, under which booking code."""

    bookmaker: PlacedBookmaker
    stake_inr: Decimal = Field(gt=0, le=Decimal("10000000"))
    placed_odds: Decimal | None = Field(default=None, ge=Decimal("1.01"), le=Decimal("1000000"))
    booking_code: str | None = Field(default=None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    placed_at: datetime | None = None
    watch: bool = True  # start the in-play monitor at once
    target_profit_pct: float | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)  # Group 77: clamped to TWIN_STOP_LOSS_MIN/MAX_PCT; None: TWIN_STOP_LOSS_PCT

    @field_validator("stake_inr")
    @classmethod
    def _paise(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class MonitorStart(_Body):
    target_profit_pct: float | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1)  # Group 77


class MonitorOffer(_Body):
    cashout_offer_inr: Decimal | None = Field(default=None, ge=0)  # the book's current offer as shown; None clears it
