"""Counterparty expected loss."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float

_FAILSAFE = (ArithmeticError, ValueError, TypeError, MemoryError)


def expected_shortfall_counterparty(
    exposures: Any, 
    pd: Any, 
    lgd: Any,
    nan_fill: float = 0.0,
    posinf_fill: float = 0.0,
    neginf_fill: float = 0.0,
) -> float:
    r""":math:`\mathrm{EL} = \sum_i \max(E_i, 0)\cdot \mathrm{PD}_i\cdot \mathrm{LGD}_i`, with PD and LGD clipped to [0, 1]."""
    try:
        e = np.nan_to_num(np.asarray(exposures, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        p = np.nan_to_num(np.asarray(pd, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        l = np.nan_to_num(np.asarray(lgd, dtype=np.float64), nan=nan_fill, posinf=posinf_fill, neginf=neginf_fill)
        min_len = min(e.size, p.size, l.size)
        if min_len == 0:
            return 0.0
        loss = np.maximum(e[:min_len], 0.0) * np.clip(p[:min_len], 0.0, 1.0) * np.clip(l[:min_len], 0.0, 1.0)
        total = float(loss.sum())
        return total if math.isfinite(total) else 0.0
    except _FAILSAFE:
        return 0.0
