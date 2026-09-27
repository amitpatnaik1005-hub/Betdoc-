import math
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, model_validator

from app.schemas.bet_types import AnyLeg, EachWayLeg, LegSettlementContext

MAX_LEGS = 20
MAX_SYSTEM_COMBINATIONS = 5000
MAX_UNIT_STAKE = 1_000_000.0


class _StructureBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_stake: float = Field(..., gt=0.0, le=MAX_UNIT_STAKE)


class _MultiLegBase(_StructureBase):
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


# ---------- Base structures ----------

class SingleBet(_StructureBase):
    structure_type: Literal["SINGLE"] = "SINGLE"
    leg: AnyLeg


class ParlayBet(_MultiLegBase):
    structure_type: Literal["PARLAY"] = "PARLAY"
    legs: list[AnyLeg] = Field(..., min_length=2, max_length=MAX_LEGS)


class SystemBet(_MultiLegBase):
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


# ---------- Named system bets ----------

class NamedSystemBet(_MultiLegBase):
    REQUIRED_LEGS: ClassVar[int]
    FOLD_SIZES: ClassVar[tuple[int, ...]]
    EXPECTED_BETS: ClassVar[int]

    @model_validator(mode="after")
    def check_leg_count(self):
        required = type(self).REQUIRED_LEGS
        if len(self.legs) != required:
            name = getattr(self, "structure_type", type(self).__name__)
            raise ValueError(f"{name} requires exactly {required} legs, got {len(self.legs)}")
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

# Import-time integrity check: fold definitions must produce the advertised bet counts.
for _cls in NAMED_SYSTEM_BETS:
    _count = sum(math.comb(_cls.REQUIRED_LEGS, k) for k in _cls.FOLD_SIZES)
    if _count != _cls.EXPECTED_BETS:
        raise RuntimeError(f"{_cls.__name__} fold definition yields {_count}, expected {_cls.EXPECTED_BETS}")


# ---------- Special structures ----------

class EachWayBet(_StructureBase):
    structure_type: Literal["EACH_WAY"] = "EACH_WAY"
    leg: EachWayLeg


class LayBet(_StructureBase):
    structure_type: Literal["LAY"] = "LAY"
    leg: AnyLeg  # unit_stake = BACKER's stake


AnyBetStructure = Annotated[
    Union[
        SingleBet, ParlayBet, SystemBet, TrixieBet, PatentBet, YankeeBet, Lucky15Bet,
        CanadianBet, Lucky31Bet, HeinzBet, Lucky63Bet, SuperHeinzBet, GoliathBet,
        EachWayBet, LayBet,
    ],
    Discriminator("structure_type"),
]


class SettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structure: AnyBetStructure
    leg_contexts: dict[str, LegSettlementContext]
