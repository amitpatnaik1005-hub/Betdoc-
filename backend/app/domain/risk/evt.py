import logging
import math

import numpy as np
from scipy.stats import genpareto

from app.domain.risk.common import safe_float, to_finite_array

kumbha = logging.getLogger("betdoc.kumbha")

MIN_TAIL_OBSERVATIONS = 10


def evt_tail_risk(
    losses: list[float],
    threshold_pct: float = 0.95,
    tail_quantile: float = 0.99,
) -> dict:
    """
    Peaks-over-threshold EVT. Fits a Generalized Pareto Distribution to loss
    exceedances above the threshold_pct quantile and returns tail VaR / ES at
    tail_quantile. Losses are positive magnitudes.
    """
    result: dict = {
        "fitted": False,
        "threshold": 0.0,
        "shape": 0.0,
        "scale": 0.0,
        "n_exceedances": 0,
        "tail_quantile": float(tail_quantile),
        "tail_var": 0.0,
        "tail_es": 0.0,
    }
    arr = to_finite_array(losses)
    arr = arr[arr > 0.0]
    if arr.size == 0 or not 0.0 < threshold_pct < 1.0 or not 0.0 < tail_quantile < 1.0:
        return result

    u = float(np.quantile(arr, threshold_pct))
    exceedances = arr[arr > u] - u
    result["threshold"] = safe_float(u)
    result["n_exceedances"] = int(exceedances.size)
    if exceedances.size < MIN_TAIL_OBSERVATIONS:
        return result

    try:
        with np.errstate(all="ignore"):
            xi, _loc, beta = genpareto.fit(exceedances, floc=0.0)
        xi, beta = float(xi), float(beta)
        if not (math.isfinite(xi) and math.isfinite(beta)) or beta <= 0.0:
            return result

        ratio = (arr.size / exceedances.size) * (1.0 - tail_quantile)
        if ratio <= 0.0:
            return result
        if abs(xi) < 1e-9:
            tail_var = u - beta * math.log(ratio)
        else:
            tail_var = u + (beta / xi) * (ratio ** (-xi) - 1.0)
        tail_es = (tail_var + beta - xi * u) / (1.0 - xi) if xi < 1.0 else 0.0
    except Exception:  # noqa: BLE001 - any fit/overflow failure falls back to safe defaults
        kumbha.warning("EVT GPD fit failed; returning safe defaults", exc_info=True)
        return result

    result.update(
        fitted=True,
        shape=safe_float(xi),
        scale=safe_float(beta),
        tail_var=safe_float(max(0.0, tail_var)),
        tail_es=safe_float(max(0.0, tail_es)),
    )
    return result
