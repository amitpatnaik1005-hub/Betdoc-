import math
from collections.abc import Iterable
from decimal import Decimal

import numpy as np

SETTLED_STATUSES: tuple[str, ...] = ("WON", "LOST", "VOID", "HALF_WON", "HALF_LOST", "CASH_OUT")
ACTIVE_STATUSES: tuple[str, ...] = ("PENDING", "PENDING_NETWORK", "ACCEPTED", "UNKNOWN")
_PNL_STATUSES: frozenset[str] = frozenset({"WON", "LOST", "HALF_WON", "HALF_LOST", "CASH_OUT"})


def safe_float(value: object) -> float:
    """Cast anything (Decimal, np.float64, int) to a finite native float, else 0.0."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def to_finite_array(values: Iterable[object] | None) -> np.ndarray:
    """Float64 array with NaN/inf removed. Decimals are cast via float() first."""
    if not values:
        return np.empty(0, dtype=np.float64)
    cleaned: list[float] = []
    for v in values:
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(f):
            cleaned.append(f)
    return np.asarray(cleaned, dtype=np.float64)


def bet_profit(status: str | None, stake: Decimal | float | None, payout: Decimal | float | None) -> float:
    """Payout NULL Guard. VOID = 0; any P&L status = payout - stake, or -stake if payout is NULL."""
    normalized = (status or "").upper()
    if normalized == "VOID" or normalized not in _PNL_STATUSES:
        return 0.0
    stake_f = safe_float(stake)
    if payout is None:
        return -stake_f
    return safe_float(safe_float(payout) - stake_f)
