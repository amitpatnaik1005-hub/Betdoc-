from app.domain.bet_types.calculator import calculate_potential_return, money
from app.schemas.bet_structures import AnyBetStructure, LayBet
from app.schemas.bet_types import ExposureResult


def calculate_bet_exposure(structure: AnyBetStructure) -> ExposureResult:
    calc = calculate_potential_return(structure)
    if isinstance(structure, LayBet):
        liability = calc.ledger_stake
        return ExposureResult(
            capital_at_risk=money(liability),
            max_loss=money(liability),
            max_win=money(structure.unit_stake),
        )
    return ExposureResult(
        capital_at_risk=money(calc.total_cost),
        max_loss=money(calc.total_cost),
        max_win=money(max(0.0, calc.potential_profit)),
    )
