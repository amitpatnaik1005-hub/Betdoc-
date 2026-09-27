import math

from app.domain.bet_types.calculator import money
from app.schemas.bet_types import LegStatus, SettlementResult

_EPS = 1e-9


def split_line(line: float) -> list[float]:
    """Whole/half lines -> [line]. Quarter lines -> two adjacent lines (e.g. -0.25 -> [-0.5, 0.0])."""
    if not math.isfinite(line):
        raise ValueError("Line must be finite")
    quarters = line * 4.0
    q = round(quarters)
    if abs(quarters - q) > _EPS:
        raise ValueError("Asian lines must be multiples of 0.25")
    if q % 2 == 0:
        return [q / 4.0]
    return [(q - 1) / 4.0, (q + 1) / 4.0]


def _validate(stake: float, odds: float) -> None:
    if not math.isfinite(stake) or stake <= 0.0:
        raise ValueError("Stake must be positive and finite")
    if not math.isfinite(odds) or odds <= 1.0:
        raise ValueError("Odds must be greater than 1.0")


def _settle_components(adjusted: list[float], stake: float, odds: float) -> SettlementResult:
    _validate(stake, odds)
    part = stake / len(adjusted)
    wins = pushes = losses = 0
    payout = 0.0
    for adj in adjusted:
        if adj > _EPS:
            wins += 1
            payout += part * odds
        elif adj < -_EPS:
            losses += 1
        else:
            pushes += 1
            payout += part

    n = len(adjusted)
    if wins == n:
        status = LegStatus.WON
    elif losses == n:
        status = LegStatus.LOST
    elif pushes == n:
        status = LegStatus.VOID
    elif wins and pushes:
        status = LegStatus.HALF_WON
    elif losses and pushes:
        status = LegStatus.HALF_LOST
    else:
        status = LegStatus.HALF_WON if payout > stake else LegStatus.HALF_LOST

    return SettlementResult(status=status, payout=money(payout), profit=money(payout - stake))


def settle_asian_handicap(
    line: float, home_goals: int, away_goals: int, is_home: bool, stake: float, odds: float
) -> SettlementResult:
    if home_goals < 0 or away_goals < 0:
        raise ValueError("Goals cannot be negative")
    margin = (home_goals - away_goals) if is_home else (away_goals - home_goals)
    adjusted = [margin + component for component in split_line(line)]
    return _settle_components(adjusted, stake, odds)


def settle_asian_total(
    line: float, total_goals: int, is_over: bool, stake: float, odds: float
) -> SettlementResult:
    if line < 0:
        raise ValueError("Total line cannot be negative")
    if total_goals < 0:
        raise ValueError("Total goals cannot be negative")
    adjusted = [
        (total_goals - component) if is_over else (component - total_goals)
        for component in split_line(line)
    ]
    return _settle_components(adjusted, stake, odds)
