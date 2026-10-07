"""Liquidity-adjusted Value-at-Risk."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float, to_finite_array

_FAILSAFE = (ArithmeticError, ValueError, TypeError)


def liquidity_adjusted_var(
    var: float,
    position_size: float,
    bid_ask_spread: float,
    daily_volume: float = 0.0,
    volume_penalty_c: float = 0.0,
    volume_floor: float = 1e-8,
    max_var_multiple: float = 10.0,
) -> float:
    r""":math:`\text{L-VaR} = \text{VaR} + \tfrac12|s|\cdot\text{spread} + c\cdot\text{spread}\sqrt{\frac{|s|}{\max(V, 10^{-8})}}`,
    capped at :math:`\text{VaR}\times 10`. ``var`` must be a positive loss; otherwise returns 0.0.
    """
    try:
        v = safe_float(var)
        size = abs(safe_float(position_size))
        spread = max(safe_float(bid_ask_spread), 0.0)
        volume = max(safe_float(daily_volume), safe_float(volume_floor))
        c = max(safe_float(volume_penalty_c), 0.0)
        multiple = safe_float(max_var_multiple)
        if v <= 0.0 or multiple <= 0.0 or volume <= 0.0:
            return 0.0
        lvar = v + 0.5 * size * spread + c * spread * math.sqrt(size / volume)
        result = min(lvar, v * multiple)
        return result if math.isfinite(result) else 0.0
    except _FAILSAFE:
        return 0.0
