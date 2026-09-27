from itertools import combinations
from math import prod

from app.domain.risk.common import safe_float
from app.schemas.bet_structures import (
    AnyBetStructure,
    EachWayBet,
    LayBet,
    NamedSystemBet,
    ParlayBet,
    SingleBet,
    SystemBet,
)
from app.schemas.bet_types import BaseLeg, CalculatorResult


def money(value: float) -> float:
    """Finite, rounded to Numeric(16,4) precision."""
    return round(safe_float(value), 4)


def safe_odds(odds: float) -> float:
    """Odds <= 1.0 (or non-finite) contribute a neutral multiplier."""
    o = safe_float(odds)
    return o if o > 1.0 else 1.0


def structure_fold_sizes(structure: AnyBetStructure) -> list[int]:
    if isinstance(structure, (SingleBet, LayBet, EachWayBet)):
        return [1]
    if isinstance(structure, ParlayBet):
        return [len(structure.legs)]
    if isinstance(structure, SystemBet):
        return sorted(structure.fold_sizes)
    if isinstance(structure, NamedSystemBet):
        return list(type(structure).FOLD_SIZES)
    raise TypeError(f"Unsupported structure: {type(structure).__name__}")


def structure_combinations(structure: AnyBetStructure) -> list[tuple[BaseLeg, ...]]:
    """Every individual bet line in the structure, in deterministic order."""
    if isinstance(structure, (SingleBet, LayBet, EachWayBet)):
        return [(structure.leg,)]
    legs = list(structure.legs)
    combos: list[tuple[BaseLeg, ...]] = []
    for k in structure_fold_sizes(structure):
        combos.extend(combinations(legs, k))
    return combos


def _calc_lay(structure: LayBet) -> CalculatorResult:
    stake = safe_float(structure.unit_stake)
    liability = stake * (safe_odds(structure.leg.odds) - 1.0)
    return CalculatorResult(
        total_cost=money(liability),
        ledger_stake=money(liability),
        potential_return=money(stake + liability),
        potential_profit=money(stake),
        number_of_bets=1,
    )


def _calc_each_way(structure: EachWayBet) -> CalculatorResult:
    stake = safe_float(structure.unit_stake)          # stake PER part (win, place)
    odds = safe_odds(structure.leg.odds)
    place_odds = 1.0 + (odds - 1.0) * safe_float(structure.leg.place_fraction)
    total_cost = stake * 2
    potential_return = stake * odds + stake * place_odds
    return CalculatorResult(
        total_cost=money(total_cost),
        ledger_stake=money(total_cost),
        potential_return=money(potential_return),
        potential_profit=money(potential_return - total_cost),
        number_of_bets=2,
    )


def calculate_potential_return(structure: AnyBetStructure) -> CalculatorResult:
    if isinstance(structure, LayBet):
        return _calc_lay(structure)
    if isinstance(structure, EachWayBet):
        return _calc_each_way(structure)

    combos = structure_combinations(structure)
    n = len(combos)
    stake = safe_float(structure.unit_stake)
    if n == 0 or stake <= 0.0:
        return CalculatorResult(
            total_cost=0.0, ledger_stake=0.0, potential_return=0.0, potential_profit=0.0, number_of_bets=0
        )

    total_cost = stake * n
    potential_return = sum(stake * prod(safe_odds(leg.odds) for leg in combo) for combo in combos)
    return CalculatorResult(
        total_cost=money(total_cost),
        ledger_stake=money(total_cost),
        potential_return=money(potential_return),
        potential_profit=money(potential_return - total_cost),
        number_of_bets=n,
    )
