import math
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DECIMAL_ODDS = 1000.0
MAX_UNIT_STAKE = 1_000_000.0
MAX_LEGS = 20
MAX_SYSTEM_COMBINATIONS = 5000


class MarketType(StrEnum):
    MATCH_WINNER_1X2 = "MATCH_WINNER_1X2"
    ASIAN_HANDICAP = "ASIAN_HANDICAP"
    ASIAN_OVER_UNDER = "ASIAN_OVER_UNDER"
    OVER_UNDER_GOALS = "OVER_UNDER_GOALS"
    BTTS = "BTTS"
    DOUBLE_CHANCE = "DOUBLE_CHANCE"
    DRAW_NO_BET = "DRAW_NO_BET"
    CORRECT_SCORE = "CORRECT_SCORE"
    GOALSCORER = "GOALSCORER"
    HALF_TIME_FULL_TIME = "HALF_TIME_FULL_TIME"
    CORNERS_OVER_UNDER = "CORNERS_OVER_UNDER"
    CARDS_OVER_UNDER = "CARDS_OVER_UNDER"
    PLAYER_PROPS = "PLAYER_PROPS"


class LegStatus(StrEnum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    HALF_WON = "HALF_WON"
    HALF_LOST = "HALF_LOST"


def _check_step(value: float, step: float, label: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    scaled = value / step
    if abs(scaled - round(scaled)) > 1e-9:
        raise ValueError(f"{label} must be a multiple of {step}")
    return value


# =====================================================================
# LEGS
# =====================================================================

class BaseLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leg_id: str = Field(..., min_length=1, max_length=64)
    match_id: str = Field(..., min_length=1, max_length=128)  # event / race id (correlation guard)
    odds: float = Field(..., ge=1.0, le=MAX_DECIMAL_ODDS, allow_inf_nan=False)  # DECIMAL odds only
    true_probability: float = Field(default=0.5, gt=0.0, le=1.0, allow_inf_nan=False)
    leg_type: str


class SimpleLeg(BaseLeg):
    leg_type: Literal["SIMPLE"] = "SIMPLE"
    market_type: Literal["MATCH_WINNER_1X2", "BTTS", "DOUBLE_CHANCE", "DRAW_NO_BET"] = "MATCH_WINNER_1X2"
    selection: str = Field(..., min_length=1, max_length=128)


class AsianHandicapLeg(BaseLeg):
    leg_type: Literal["ASIAN_HANDICAP"] = "ASIAN_HANDICAP"
    line: float = Field(..., ge=-100.0, le=100.0, allow_inf_nan=False)
    is_home: bool

    @field_validator("line")
    @classmethod
    def _quarter(cls, v: float) -> float:
        return _check_step(v, 0.25, "Asian handicap line")


class AsianOverUnderLeg(BaseLeg):
    leg_type: Literal["ASIAN_OVER_UNDER"] = "ASIAN_OVER_UNDER"
    line: float = Field(..., ge=0.0, le=1000.0, allow_inf_nan=False)
    is_over: bool

    @field_validator("line")
    @classmethod
    def _quarter(cls, v: float) -> float:
        return _check_step(v, 0.25, "Asian total line")


class CorrectScoreLeg(BaseLeg):
    leg_type: Literal["CORRECT_SCORE"] = "CORRECT_SCORE"
    home_score: int = Field(..., ge=0, le=300)
    away_score: int = Field(..., ge=0, le=300)


class GoalscorerLeg(BaseLeg):
    leg_type: Literal["GOALSCORER"] = "GOALSCORER"
    player_id: str = Field(..., min_length=1, max_length=128)
    market_type: Literal["FIRST", "LAST", "ANYTIME"]


class PlayerPropLeg(BaseLeg):
    leg_type: Literal["PLAYER_PROP"] = "PLAYER_PROP"
    player_id: str = Field(..., min_length=1, max_length=128)
    stat_type: str = Field(..., min_length=1, max_length=64)
    line: float = Field(..., ge=0.0, le=100_000.0, allow_inf_nan=False)
    is_over: bool


class EachWayLeg(BaseLeg):
    leg_type: Literal["EACH_WAY"] = "EACH_WAY"
    selection: str = Field(..., min_length=1, max_length=128)
    place_numerator: int = Field(..., ge=1, le=100)
    place_denominator: int = Field(..., ge=1, le=100)
    place_places: int = Field(..., ge=1, le=10)

    @model_validator(mode="after")
    def _fraction(self):
        if self.place_numerator > self.place_denominator:
            raise ValueError("Place fraction cannot exceed 1 (numerator > denominator)")
        return self


AnyLeg = Annotated[
    Union[
        SimpleLeg, AsianHandicapLeg, AsianOverUnderLeg, CorrectScoreLeg,
        GoalscorerLeg, PlayerPropLeg, EachWayLeg,
    ],
    Field(discriminator="leg_type"),
]


# =====================================================================
# SETTLEMENT CONTEXT & RESULTS
# =====================================================================

class LegSettlementContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LegStatus
    dead_heat_divisor: int = Field(default=1, ge=1, le=100)
    rule4_deduction_pct: float = Field(default=0.0, ge=0.0, lt=1.0, allow_inf_nan=False)  # 0.15 = 15p/£
    player_did_not_participate: bool = False
    finishing_position: int | None = Field(default=None, ge=1)  # each-way place settlement


class CalculatorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cost: float
    ledger_stake: float
    potential_return: float
    potential_profit: float
    number_of_bets: int


class SettlementResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LegStatus
    total_stake: float
    gross_payout: float
    commission: float
    payout: float
    profit: float


class ExposureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capital_at_risk: float
    max_loss: float
    max_win: float


# =====================================================================
# STRUCTURES
# =====================================================================

class BetStructure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structure_type: str


class _MultiLegBet(BetStructure):
    legs: list[AnyLeg]

    @model_validator(mode="after")
    def check_related_contingencies(self):
        if len(set(leg.match_id for leg in self.legs)) != len(self.legs):
            raise ValueError("Related contingencies not permitted in standard combinations.")
        return self

    @model_validator(mode="after")
    def check_unique_leg_ids(self):
        if len(set(leg.leg_id for leg in self.legs)) != len(self.legs):
            raise ValueError("Every leg must have a unique leg_id.")
        return self


class SingleBet(BetStructure):
    structure_type: Literal["SINGLE"] = "SINGLE"
    stake: float = Field(..., gt=0.0, le=MAX_UNIT_STAKE, allow_inf_nan=False)
    leg: AnyLeg


class ParlayBet(_MultiLegBet):
    structure_type: Literal["PARLAY"] = "PARLAY"
    stake: float = Field(..., gt=0.0, le=MAX_UNIT_STAKE, allow_inf_nan=False)
    legs: list[AnyLeg] = Field(..., min_length=2, max_length=MAX_LEGS)


class SystemBetBase(_MultiLegBet):
    """UNIT STAKE MANDATE: every line is staked at unit_stake."""
    unit_stake: float = Field(..., gt=0.0, le=MAX_UNIT_STAKE, allow_inf_nan=False)


class SystemBet(SystemBetBase):
    structure_type: Literal["SYSTEM"] = "SYSTEM"
    legs: list[AnyLeg] = Field(..., min_length=2, max_length=MAX_LEGS)
    fold_sizes: set[int] = Field(..., min_length=1)

    @model_validator(mode="after")
    def check_fold_sizes(self):
        n = len(self.legs)
        invalid = sorted(k for k in self.fold_sizes if k < 1 or k > n)
        if invalid:
            raise ValueError(f"fold_sizes {invalid} are outside 1..{n}")
        total = sum(math.comb(n, k) for k in self.fold_sizes)
        if total > MAX_SYSTEM_COMBINATIONS:
            raise ValueError(f"System produces {total} bets; maximum is {MAX_SYSTEM_COMBINATIONS}")
        return self


class NamedSystemBet(SystemBetBase):
    REQUIRED_LEGS: ClassVar[int]
    FOLD_SIZES: ClassVar[tuple[int, ...]]
    EXPECTED_BETS: ClassVar[int]

    @model_validator(mode="after")
    def check_leg_count(self):
        required = type(self).REQUIRED_LEGS
        if len(self.legs) != required:
            raise ValueError(f"{self.structure_type} requires exactly {required} legs, got {len(self.legs)}")
        return self


class TrixieBet(NamedSystemBet):
    structure_type: Literal["TRIXIE"] = "TRIXIE"
    REQUIRED_LEGS: ClassVar[int] = 3
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (2, 3)
    EXPECTED_BETS: ClassVar[int] = 4


class PatentBet(NamedSystemBet):
    structure_type: Literal["PATENT"] = "PATENT"
    REQUIRED_LEGS: ClassVar[int] = 3
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (1, 2, 3)
    EXPECTED_BETS: ClassVar[int] = 7


class YankeeBet(NamedSystemBet):
    structure_type: Literal["YANKEE"] = "YANKEE"
    REQUIRED_LEGS: ClassVar[int] = 4
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (2, 3, 4)
    EXPECTED_BETS: ClassVar[int] = 11


class Lucky15Bet(NamedSystemBet):
    structure_type: Literal["LUCKY_15"] = "LUCKY_15"
    REQUIRED_LEGS: ClassVar[int] = 4
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (1, 2, 3, 4)
    EXPECTED_BETS: ClassVar[int] = 15


class CanadianBet(NamedSystemBet):
    structure_type: Literal["CANADIAN"] = "CANADIAN"
    REQUIRED_LEGS: ClassVar[int] = 5
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (2, 3, 4, 5)
    EXPECTED_BETS: ClassVar[int] = 26


class Lucky31Bet(NamedSystemBet):
    structure_type: Literal["LUCKY_31"] = "LUCKY_31"
    REQUIRED_LEGS: ClassVar[int] = 5
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (1, 2, 3, 4, 5)
    EXPECTED_BETS: ClassVar[int] = 31


class HeinzBet(NamedSystemBet):
    structure_type: Literal["HEINZ"] = "HEINZ"
    REQUIRED_LEGS: ClassVar[int] = 6
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (2, 3, 4, 5, 6)
    EXPECTED_BETS: ClassVar[int] = 57


class Lucky63Bet(NamedSystemBet):
    structure_type: Literal["LUCKY_63"] = "LUCKY_63"
    REQUIRED_LEGS: ClassVar[int] = 6
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (1, 2, 3, 4, 5, 6)
    EXPECTED_BETS: ClassVar[int] = 63


class SuperHeinzBet(NamedSystemBet):
    structure_type: Literal["SUPER_HEINZ"] = "SUPER_HEINZ"
    REQUIRED_LEGS: ClassVar[int] = 7
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (2, 3, 4, 5, 6, 7)
    EXPECTED_BETS: ClassVar[int] = 120


class GoliathBet(NamedSystemBet):
    structure_type: Literal["GOLIATH"] = "GOLIATH"
    REQUIRED_LEGS: ClassVar[int] = 8
    FOLD_SIZES: ClassVar[tuple[int, ...]] = (2, 3, 4, 5, 6, 7, 8)
    EXPECTED_BETS: ClassVar[int] = 247


NAMED_SYSTEM_BETS: tuple[type[NamedSystemBet], ...] = (
    TrixieBet, PatentBet, YankeeBet, Lucky15Bet, CanadianBet,
    Lucky31Bet, HeinzBet, Lucky63Bet, SuperHeinzBet, GoliathBet,
)

for _cls in NAMED_SYSTEM_BETS:  # import-time integrity check
    _count = sum(math.comb(_cls.REQUIRED_LEGS, k) for k in _cls.FOLD_SIZES)
    if _count != _cls.EXPECTED_BETS:
        raise RuntimeError(f"{_cls.__name__} yields {_count} bets, expected {_cls.EXPECTED_BETS}")


EachWayInner = Annotated[
    Union[
        SingleBet, ParlayBet, SystemBet, TrixieBet, PatentBet, YankeeBet, Lucky15Bet,
        CanadianBet, Lucky31Bet, HeinzBet, Lucky63Bet, SuperHeinzBet, GoliathBet,
    ],
    Field(discriminator="structure_type"),
]


class EachWayBet(BetStructure):
    """Win line = wrapped structure at win odds; place line = same structure at place odds."""
    structure_type: Literal["EACH_WAY"] = "EACH_WAY"
    bet: EachWayInner

    @model_validator(mode="after")
    def check_each_way_legs(self):
        inner = self.bet
        legs = [inner.leg] if isinstance(inner, SingleBet) else list(inner.legs)
        bad = [leg.leg_id for leg in legs if not isinstance(leg, EachWayLeg)]
        if bad:
            raise ValueError(f"Each-way bets require EACH_WAY legs; invalid legs: {bad}")
        return self


class LayBet(BetStructure):
    structure_type: Literal["LAY"] = "LAY"
    leg_id: str = Field(..., min_length=1, max_length=64)
    match_id: str = Field(..., min_length=1, max_length=128)
    odds: float = Field(..., ge=1.0, le=MAX_DECIMAL_ODDS, allow_inf_nan=False)
    backer_stake: float = Field(..., gt=0.0, le=MAX_UNIT_STAKE, allow_inf_nan=False)


BetStructureUnion = Union[
    SingleBet, ParlayBet, SystemBet, TrixieBet, PatentBet, YankeeBet, Lucky15Bet,
    CanadianBet, Lucky31Bet, HeinzBet, Lucky63Bet, SuperHeinzBet, GoliathBet,
    EachWayBet, LayBet,
]
AnyBetStructure = Annotated[BetStructureUnion, Field(discriminator="structure_type")]


class SettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structure: AnyBetStructure
    leg_contexts: dict[str, LegSettlementContext]
    commission_pct: float = Field(default=0.0, ge=0.0, lt=1.0, allow_inf_nan=False)  # fraction
