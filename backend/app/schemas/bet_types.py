import math
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, field_validator


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


class BetStructure(StrEnum):
    SINGLE = "SINGLE"
    PARLAY = "PARLAY"
    SYSTEM = "SYSTEM"
    TRIXIE = "TRIXIE"
    YANKEE = "YANKEE"
    PATENT = "PATENT"
    LUCKY_15 = "LUCKY_15"
    LUCKY_31 = "LUCKY_31"
    LUCKY_63 = "LUCKY_63"
    CANADIAN = "CANADIAN"
    HEINZ = "HEINZ"
    SUPER_HEINZ = "SUPER_HEINZ"
    GOLIATH = "GOLIATH"
    EACH_WAY = "EACH_WAY"
    LAY = "LAY"


class LegStatus(StrEnum):
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    HALF_WON = "HALF_WON"
    HALF_LOST = "HALF_LOST"
    PENDING = "PENDING"


# ---------- Return / context schemas ----------

class LegSettlementContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: LegStatus
    dead_heat_denominator: int = Field(default=1, ge=1, le=100)
    rule4_deduction_pct: float = Field(default=0.0, ge=0.0, lt=1.0)  # fraction: 0.15 = 15p in the pound
    finishing_position: int | None = Field(default=None, ge=1)       # each-way place settlement


class CalculatorResult(BaseModel):
    total_cost: float
    ledger_stake: float
    potential_return: float
    potential_profit: float
    number_of_bets: int


class SettlementResult(BaseModel):
    status: LegStatus
    payout: float
    profit: float


class ExposureResult(BaseModel):
    capital_at_risk: float
    max_loss: float
    max_win: float


# ---------- Legs ----------

def _check_step(value: float, step: float, label: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    scaled = value / step
    if abs(scaled - round(scaled)) > 1e-9:
        raise ValueError(f"{label} must be a multiple of {step}")
    return value


class BaseLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leg_id: str = Field(..., min_length=1, max_length=64)
    match_id: str = Field(..., min_length=1, max_length=128)
    market_type: MarketType
    selection: str = Field(..., min_length=1, max_length=128)
    odds: float = Field(..., gt=1.0, le=100_000.0)
    true_probability: float = Field(default=0.5, gt=0.0, le=1.0)


class SimpleLeg(BaseLeg):
    market_type: Literal[  # type: ignore[assignment]
        "MATCH_WINNER_1X2", "BTTS", "DOUBLE_CHANCE", "DRAW_NO_BET", "PLAYER_PROPS"
    ]


class AsianHandicapLeg(BaseLeg):
    market_type: Literal["ASIAN_HANDICAP"]  # type: ignore[assignment]
    line: float = Field(..., ge=-20.0, le=20.0)

    @field_validator("line")
    @classmethod
    def _quarter_line(cls, v: float) -> float:
        return _check_step(v, 0.25, "Asian handicap line")


class AsianOverUnderLeg(BaseLeg):
    market_type: Literal["ASIAN_OVER_UNDER"]  # type: ignore[assignment]
    line: float = Field(..., ge=0.0, le=50.0)
    is_over: bool

    @field_validator("line")
    @classmethod
    def _quarter_line(cls, v: float) -> float:
        return _check_step(v, 0.25, "Asian total line")


class OverUnderLeg(BaseLeg):
    market_type: Literal[  # type: ignore[assignment]
        "OVER_UNDER_GOALS", "CORNERS_OVER_UNDER", "CARDS_OVER_UNDER"
    ]
    line: float = Field(..., ge=0.0, le=200.0)
    is_over: bool

    @field_validator("line")
    @classmethod
    def _half_line(cls, v: float) -> float:
        return _check_step(v, 0.5, "Over/Under line")


class CorrectScoreLeg(BaseLeg):
    market_type: Literal["CORRECT_SCORE"]  # type: ignore[assignment]
    home_goals: int = Field(..., ge=0, le=30)
    away_goals: int = Field(..., ge=0, le=30)


class GoalscorerLeg(BaseLeg):
    market_type: Literal["GOALSCORER"]  # type: ignore[assignment]
    player_name: str = Field(..., min_length=1, max_length=128)
    scorer_type: Literal["FIRST", "LAST", "ANYTIME"]


class HalfTimeFullTimeLeg(BaseLeg):
    market_type: Literal["HALF_TIME_FULL_TIME"]  # type: ignore[assignment]
    ht_result: Literal["HOME", "DRAW", "AWAY"]
    ft_result: Literal["HOME", "DRAW", "AWAY"]


class EachWayLeg(BaseLeg):
    place_fraction: float = Field(..., gt=0.0, le=1.0)
    place_terms: int = Field(..., ge=1, le=10)


AnyLeg = Annotated[
    Union[
        SimpleLeg,
        AsianHandicapLeg,
        AsianOverUnderLeg,
        OverUnderLeg,
        CorrectScoreLeg,
        GoalscorerLeg,
        HalfTimeFullTimeLeg,
    ],
    Discriminator("market_type"),
]
