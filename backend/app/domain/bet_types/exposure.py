from app.domain.bet_types.calculator import (
    ZERO,
    money,
    potential_return_dec,
    to_dec,
    total_stake_dec,
)
from app.schemas.bet_types import AnyBetStructure, ExposureResult, LayBet


def calculate_bet_exposure(structure: AnyBetStructure) -> float:
    """Back: total staked (systems = unit_stake * lines). Lay: backer_stake * (odds - 1)."""
    return money(total_stake_dec(structure))


def build_exposure_result(structure: AnyBetStructure) -> ExposureResult:
    exposure = total_stake_dec(structure)
    if isinstance(structure, LayBet):
        max_win = to_dec(structure.backer_stake)
    else:
        max_win = max(ZERO, potential_return_dec(structure) - exposure)
    return ExposureResult(
        capital_at_risk=money(exposure),
        max_loss=money(exposure),
        max_win=money(max_win),
    )
