"""Integer-paise money primitives. The only currency representation allowed.

Rule: money crosses the domain boundary as ``Decimal`` INR and lives inside
the domain as ``int`` paise. There is no float path. Allocation rounds DOWN
so a bankroll constraint can never be violated by a rounding artefact, and
reserves round UP so exposure is never under-stated.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from typing import Final

__all__ = ["MAX_PAISE", "PAISE_PER_RUPEE", "from_paise", "to_paise", "to_paise_ceil"]

PAISE_PER_RUPEE: Final[int] = 100
MAX_PAISE: Final[int] = 10**15
"""Sanity ceiling (10 trillion INR). Anything larger is a units error."""

_INTERNAL_PRECISION: Final[int] = 34


def _coerce(amount: Decimal | int | str, *, label: str) -> Decimal:
    if isinstance(amount, bool):
        msg = f"{label} must be numeric, got bool"
        raise ValueError(msg)
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (ArithmeticError, ValueError) as exc:
        msg = f"{label} is not a valid decimal amount: {amount!r}"
        raise ValueError(msg) from exc
    if not value.is_finite():
        msg = f"{label} must be finite, got {amount!r}"
        raise ValueError(msg)
    if value < 0:
        msg = f"{label} must be non-negative, got {value}"
        raise ValueError(msg)
    return value


def to_paise(amount: Decimal | int | str, *, label: str = "amount") -> int:
    """INR to integer paise, rounding DOWN. Use for anything we allocate."""
    value = _coerce(amount, label=label)
    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        paise = (value * PAISE_PER_RUPEE).to_integral_value(rounding=ROUND_DOWN)
    result = int(paise)
    if result > MAX_PAISE:
        msg = f"{label} of {value} INR exceeds the sanity ceiling"
        raise ValueError(msg)
    return result


def to_paise_ceil(amount: Decimal | int | str, *, label: str = "amount") -> int:
    """INR to integer paise, rounding UP. Use for anything we reserve against."""
    value = _coerce(amount, label=label)
    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        paise = (value * PAISE_PER_RUPEE).to_integral_value(rounding=ROUND_UP)
    result = int(paise)
    if result > MAX_PAISE:
        msg = f"{label} of {value} INR exceeds the sanity ceiling"
        raise ValueError(msg)
    return result


def from_paise(paise: int) -> Decimal:
    """Integer paise back to exact INR ``Decimal``. Always lossless."""
    if isinstance(paise, bool) or not isinstance(paise, int):
        msg = f"paise must be an int, got {type(paise).__name__}"
        raise ValueError(msg)
    if paise < 0:
        msg = f"paise must be non-negative, got {paise}"
        raise ValueError(msg)
    return (Decimal(paise) / PAISE_PER_RUPEE).quantize(Decimal("0.01"))
