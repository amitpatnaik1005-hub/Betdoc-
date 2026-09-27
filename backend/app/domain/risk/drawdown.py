import numpy as np

from app.domain.risk.common import safe_float, to_finite_array


def calculate_max_drawdown(equity_curve: list[float]) -> float:
    """Largest peak-to-trough decline as a fraction of the running peak."""
    arr = to_finite_array(equity_curve)
    if arr.size < 2:
        return 0.0
    peaks = np.maximum.accumulate(arr)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(peaks > 0.0, (peaks - arr) / peaks, 0.0)
    drawdowns = drawdowns[np.isfinite(drawdowns)]
    if drawdowns.size == 0:
        return 0.0
    return safe_float(max(0.0, float(np.max(drawdowns))))


def calculate_current_drawdown(equity_curve: list[float]) -> float:
    """Decline of the latest point from the highest peak, as a fraction."""
    arr = to_finite_array(equity_curve)
    if arr.size == 0:
        return 0.0
    peak = float(np.max(arr))
    if peak <= 0.0:
        return 0.0
    return safe_float(max(0.0, (peak - float(arr[-1])) / peak))
