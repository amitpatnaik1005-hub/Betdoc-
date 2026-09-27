from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from itertools import combinations
from typing import Iterable

from app.schemas.bet_types import (
    AnyBetStructure,
    BaseLeg,
    CalculatorResult,
    EachWayBet,
    EachWayLeg,
    LayBet,
    NamedSystemBet,
    ParlayBet,
    SingleBet,
    SystemBet,
)

ZERO = Decimal(0)
ONE = Decimal(1)
Q4 = Decimal("0.0001")


def to_dec(value: object) -> Decimal:
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return ZERO
    return d if d.is_finite() else ZERO


def money(value: Decimal) -> float:
    return float(to_dec(value).quantize(Q4, rounding=ROUND_HALF_EVEN)) + 0.0  # +0.0 kills -0.0


def dprod(values: Iterable[Decimal]) -> Decimal:
    result = ONE
    for v in values:
        result *= v
    return result


def safe_odds(odds: object) -> Decimal:
    """Odds <= 1.0 contribute a neutral multiplier."""
    o = to_dec(odds)
    return o if o > ONE else ONE


def legs_of(structure: AnyBetStructure) -> list[BaseLeg]:
    if isinstance(structure, LayBet):
        return []
    if isinstance(structure, EachWayBet):
        return legs_of(structure.bet)
    if isinstance(structure, SingleBet):
        return [structure.leg]
    return list(structure.legs)


def structure_fold_sizes(structure: AnyBetStructure) -> list[int]:
    if isinstance(structure, EachWayBet):
        return structure_fold_sizes(structure.bet)
    if isinstance(structure, (SingleBet, LayBet)):
        return [1]
    if isinstance(structure, ParlayBet):
        return [len(structure.legs)]
    if isinstance(structure, SystemBet):
        return sorted(structure.fold_sizes)
    if isinstance(structure, NamedSystemBet):
        return list(type(structure).FOLD_SIZES)
    raise TypeError(f"Unsupported structure: {type(structure).__name__}")


def structure_combinations(structure: AnyBetStructure) -> list[tuple[BaseLeg, ...]]:
    """Every staked line. Each-way returns the WIN lines (place lines mirror them). Lay has no leg objects."""
    if isinstance(structure, LayBet):
        return []
    if isinstance(structure, EachWayBet):
        return structure_combinations(structure.bet)
    if isinstance(structure, SingleBet):
        return [(structure.leg,)]
    if isinstance(structure, ParlayBet):
        return [tuple(structure.legs)]
    legs = list(structure.legs)
    combos: list[tuple[BaseLeg, ...]] = []
    for k in structure_fold_sizes(structure):
        combos.extend(combinations(legs, k))
    return combos


def line_stake(structure: AnyBetStructure) -> Decimal:
    """Stake carried by each individual line."""
    if isinstance(structure, LayBet):
        return to_dec(structure.backer_stake)
    if isinstance(structure, EachWayBet):
        return line_stake(structure.bet)
    if isinstance(structure, (SingleBet, ParlayBet)):
        return to_dec(structure.stake)
    return to_dec(structure.unit_stake)


def number_of_bets(structure: AnyBetStructure) -> int:
    if isinstance(structure, LayBet):
        return 1
    if isinstance(structure, EachWayBet):
        return 2 * len(structure_combinations(structure.bet))
    return len(structure_combinations(structure))


def lay_liability(structure: LayBet) -> Decimal:
    return to_dec(structure.backer_stake) * (safe_odds(structure.odds) - ONE)


def total_stake_dec(structure: AnyBetStructure) -> Decimal:
    if isinstance(structure, LayBet):
        return lay_liability(structure)
    if isinstance(structure, EachWayBet):
        return Decimal(2) * total_stake_dec(structure.bet)
    return line_stake(structure) * Decimal(len(structure_combinations(structure)))


def calculate_total_stake(structure: AnyBetStructure) -> float:
    return money(total_stake_dec(structure))


def place_odds(leg: EachWayLeg) -> Decimal:
    """Pre-match place odds with exact fraction math (no Rule 4 known yet)."""
    return ONE + (safe_odds(leg.odds) - ONE) * Decimal(leg.place_numerator) / Decimal(leg.place_denominator)


def potential_return_dec(structure: AnyBetStructure) -> Decimal:
    if isinstance(structure, LayBet):
        stake = to_dec(structure.backer_stake)
        return lay_liability(structure) + stake
    stake = line_stake(structure)
    combos = structure_combinations(structure)
    win_return = sum((stake * dprod(safe_odds(leg.odds) for leg in combo) for combo in combos), ZERO)
    if isinstance(structure, EachWayBet):
        place_return = sum(
            (stake * dprod(place_odds(leg) for leg in combo) for combo in combos),  # type: ignore[arg-type]
            ZERO,
        )
        return win_return + place_return
    return win_return


def calculate_potential_return(structure: AnyBetStructure) -> float:
    return money(potential_return_dec(structure))


def summarize_structure(structure: AnyBetStructure) -> CalculatorResult:
    total = total_stake_dec(structure)
    potential = potential_return_dec(structure)
    if isinstance(structure, LayBet):
        profit = to_dec(structure.backer_stake)
    else:
        profit = potential - total
    return CalculatorResult(
        total_cost=money(total),
        ledger_stake=money(total),  # lay: liability
        potential_return=money(potential),
        potential_profit=money(profit),
        number_of_bets=number_of_bets(structure),
    )
