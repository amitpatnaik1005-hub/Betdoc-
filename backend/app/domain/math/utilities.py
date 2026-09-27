import math


def implied_probability(odds: float) -> float:
    if odds is None or not math.isfinite(odds) or odds <= 1.0:
        return 0.0
    return float(1.0 / odds)


def calculate_overround(odds_list: list[float]) -> float:
    """Bookmaker margin, e.g. 0.05 means a 105% book."""
    return float(sum(implied_probability(o) for o in odds_list) - 1.0)


def remove_vig_proportional(odds_list: list[float]) -> list[float]:
    """Return fair (vig-free) probabilities that sum to 1.0."""
    implied = [implied_probability(o) for o in odds_list]
    total = sum(implied)
    if total <= 0.0:
        return [0.0 for _ in odds_list]
    return [float(p / total) for p in implied]


def expected_value(true_prob: float, decimal_odds: float) -> float:
    """EV per 1 unit staked."""
    if decimal_odds <= 1.0:
        return -1.0
    return float(true_prob * decimal_odds - 1.0)


def kelly_criterion(true_prob: float, decimal_odds: float, fraction: float = 1.0) -> float:
    """Kelly stake as a fraction of bankroll, clamped to [0, 1]."""
    b = decimal_odds - 1.0
    if b <= 0.0 or not 0.0 <= true_prob <= 1.0:
        return 0.0
    q = 1.0 - true_prob
    full_kelly = (b * true_prob - q) / b
    return float(max(0.0, min(1.0, full_kelly * fraction)))
