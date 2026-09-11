from __future__ import annotations

import math
from typing import Final

BPS_DENOMINATOR: Final[int] = 10_000

def apply_bps(amount_paise: int, bps: int) -> int:
    if amount_paise < 0:
        raise ValueError(f"amount_paise must be non-negative, got {amount_paise}")
    if not 0 <= bps <= BPS_DENOMINATOR:
        raise ValueError(f"bps must fall within [0, {BPS_DENOMINATOR}], got {bps}")
    return (amount_paise * bps) // BPS_DENOMINATOR

def clamp_non_negative(value_paise: int) -> int:
    return value_paise if value_paise > 0 else 0

def ratio_to_bps_floor(ratio: float) -> int:
    if not math.isfinite(ratio):
        raise ValueError(f"ratio must be finite, got {ratio!r}")
    return math.floor(ratio * BPS_DENOMINATOR)
