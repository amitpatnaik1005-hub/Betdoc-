import math

import numpy as np

from app.domain.risk.common import safe_float, to_finite_array

RATIO_CAP = 99.99


def _cap(value: float) -> float:
    return safe_float(max(-RATIO_CAP, min(RATIO_CAP, value)))


def sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    arr = to_finite_array(returns)
    if arr.size < 2:
        return 0.0
    excess = arr - safe_float(risk_free_rate)
    std = float(np.std(excess, ddof=1))
    if not math.isfinite(std) or std == 0.0:
        return 0.0
    return _cap(float(np.mean(excess)) / std)


def sortino_ratio(returns: list[float], risk_free_rate: float = 0.0) -> float:
    arr = to_finite_array(returns)
    if arr.size == 0:
        return 0.0
    excess = arr - safe_float(risk_free_rate)
    mean_excess = float(np.mean(excess))
    downside = np.minimum(excess, 0.0)
    downside_dev = math.sqrt(float(np.mean(downside ** 2)))
    if not math.isfinite(downside_dev) or downside_dev == 0.0:
        return RATIO_CAP if mean_excess > 0.0 else 0.0
    return _cap(mean_excess / downside_dev)


def calmar_ratio(annualized_return: float, max_dd: float) -> float:
    ann = safe_float(annualized_return)
    dd = safe_float(max_dd)
    if dd <= 0.0:
        return 0.0
    return _cap(ann / dd)


def omega_ratio(returns: list[float], threshold: float = 0.0) -> float:
    arr = to_finite_array(returns)
    if arr.size == 0:
        return 0.0
    excess = arr - safe_float(threshold)
    gains = float(np.sum(np.maximum(excess, 0.0)))
    losses = float(np.sum(np.maximum(-excess, 0.0)))
    if losses == 0.0:
        return RATIO_CAP if gains > 0.0 else 0.0
    return safe_float(min(RATIO_CAP, gains / losses))
