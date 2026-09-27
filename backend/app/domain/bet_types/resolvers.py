import math

from app.schemas.bet_types import LegStatus

_EPS = 1e-9


def split_asian_line(line: float) -> list[float]:
    """Whole/half -> [line]; quarter -> two adjacent lines (-0.25 -> [-0.5, 0.0], 2.25 -> [2.0, 2.5])."""
    if not math.isfinite(line):
        raise ValueError("Line must be finite")
    quarters = line * 4.0
    q = round(quarters)
    if abs(quarters - q) > _EPS:
        raise ValueError("Asian lines must be multiples of 0.25")
    if q % 2 == 0:
        return [q / 4.0]
    return [(q - 1) / 4.0, (q + 1) / 4.0]


def _sign(value: float) -> int:
    if value > _EPS:
        return 1
    if value < -_EPS:
        return -1
    return 0


def _combine(outcomes: list[int]) -> LegStatus:
    wins = outcomes.count(1)
    pushes = outcomes.count(0)
    losses = outcomes.count(-1)
    n = len(outcomes)
    if wins == n:
        return LegStatus.WON
    if losses == n:
        return LegStatus.LOST
    if pushes == n:
        return LegStatus.VOID
    if wins and pushes and not losses:
        return LegStatus.HALF_WON
    if losses and pushes and not wins:
        return LegStatus.HALF_LOST
    raise ValueError("Split outcome (win + loss) is not representable; quarter lines require integer results")


def settle_asian_handicap(line: float, home_score: int, away_score: int, is_home: bool) -> LegStatus:
    if home_score < 0 or away_score < 0:
        raise ValueError("Scores cannot be negative")
    margin = (home_score - away_score) if is_home else (away_score - home_score)
    return _combine([_sign(margin + component) for component in split_asian_line(line)])


def settle_asian_total(line: float, total_goals: float, is_over: bool) -> LegStatus:
    if not math.isfinite(total_goals) or total_goals < 0:
        raise ValueError("Total must be a non-negative finite number")
    if line < 0:
        raise ValueError("Total line cannot be negative")
    return _combine([
        _sign((total_goals - component) if is_over else (component - total_goals))
        for component in split_asian_line(line)
    ])


def settle_player_prop(line: float, actual_stat: float, is_over: bool, did_participate: bool) -> LegStatus:
    if not did_participate:
        return LegStatus.VOID  # PLAYER SCRATCH RULE
    if not (math.isfinite(line) and math.isfinite(actual_stat)):
        raise ValueError("Line and stat must be finite")
    diff = (actual_stat - line) if is_over else (line - actual_stat)
    outcome = _sign(diff)
    if outcome > 0:
        return LegStatus.WON
    if outcome < 0:
        return LegStatus.LOST
    return LegStatus.VOID  # push on whole lines


def settle_correct_score(pred_home: int, pred_away: int, act_home: int, act_away: int) -> LegStatus:
    if min(pred_home, pred_away, act_home, act_away) < 0:
        raise ValueError("Scores cannot be negative")
    return LegStatus.WON if (pred_home == act_home and pred_away == act_away) else LegStatus.LOST
