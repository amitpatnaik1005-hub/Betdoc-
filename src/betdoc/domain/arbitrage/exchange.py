"""Betting exchange mathematics: commission-adjusted back and lay pricing.

An exchange is not a bookmaker. Three structural differences change every
calculation downstream:

#. **Commission is charged on net winnings**, not on turnover, so the quoted
   price is never the price you actually receive.
#. **You can lay**, which means accepting a backer's stake and carrying their
   liability. Your capital at risk is the liability, not the "stake".
#. **A lay is economically a back of the complement**, at odds
   ``(O - c) / (O - 1)``. Any arbitrage or Kelly engine that compares a lay
   price against a back price without this conversion is comparing two
   different units and will manufacture phantom edges.

Commission model
----------------
This module applies commission to the **gross winnings of the winning bet**,
which is the standard per-bet approximation. Betfair-style venues net
commission across a whole market at settlement, so realised commission on a
hedged position can be lower than the sum of these per-bet figures. That makes
every number here **conservative**, which is the only acceptable direction of
error in a pricing library.

Money is ``Decimal`` with directional rounding: liabilities round UP (never
under-reserve capital) and profits round DOWN (never over-promise P&L). Odds
and probabilities stay ``float``, since they are estimates rather than
obligations, but every comparison against a boundary uses an explicit epsilon.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from enum import StrEnum
from typing import Final

__all__ = [
    "BetSide",
    "CommissionError",
    "ExchangeMathError",
    "InvalidOddsError",
    "MAX_DECIMAL_ODDS",
    "MIN_DECIMAL_ODDS",
    "backer_stake_for_liability",
    "effective_back_odds",
    "effective_lay_odds",
    "effective_odds",
    "hedge_lay_backer_stake",
    "implied_probability",
    "lay_liability",
    "lay_liability_exact",
    "lay_profit_exact",
]

# --------------------------------------------------------------------------- #
# Domain constants
# --------------------------------------------------------------------------- #

MIN_DECIMAL_ODDS: Final[float] = 1.01
"""Tightest price any major exchange accepts. Below this, treat as unpriceable."""

MAX_DECIMAL_ODDS: Final[float] = 1_000.0
"""Widest price any major exchange accepts."""

MAX_COMMISSION_RATE: Final[float] = 0.20
"""Sanity ceiling. A quoted rate above 20% is a units error (5 instead of 0.05)."""

_EPS: Final[float] = 1e-12
"""Comparison epsilon. Never compare odds or rates with bare ``==``."""

_MONEY: Final[Decimal] = Decimal("0.01")
_INTERNAL_PRECISION: Final[int] = 28


class BetSide(StrEnum):
    """Which side of the book we are on. Strict enum: no boolean flags.

    A boolean ``is_lay`` inverts silently when a caller passes the wrong
    argument position. An enum raises.
    """

    BACK = "back"
    LAY = "lay"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ExchangeMathError(ValueError):
    """Base class for every rejected exchange calculation."""


class InvalidOddsError(ExchangeMathError):
    """Odds are non-finite, outside the tradable range, or economically absurd."""


class CommissionError(ExchangeMathError):
    """Commission rate is non-finite, negative, or not expressed as a fraction."""


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


def _validate_odds(raw_odds: float, *, label: str = "odds") -> float:
    """Reject any price that cannot be traded or would divide by zero.

    Raises:
        InvalidOddsError: Price is NaN, infinite, a bool, or out of range.
    """
    if isinstance(raw_odds, bool):
        msg = f"{label} must be numeric, got bool"
        raise InvalidOddsError(msg)
    value = float(raw_odds)
    if not math.isfinite(value):
        msg = f"{label} must be finite, got {raw_odds!r}"
        raise InvalidOddsError(msg)
    if value < MIN_DECIMAL_ODDS - _EPS:
        msg = f"{label} {value!r} is below the tradable minimum {MIN_DECIMAL_ODDS}"
        raise InvalidOddsError(msg)
    if value > MAX_DECIMAL_ODDS + _EPS:
        msg = f"{label} {value!r} exceeds the tradable maximum {MAX_DECIMAL_ODDS}"
        raise InvalidOddsError(msg)
    # Guard the (O - 1) denominator explicitly rather than trusting the range.
    if math.isclose(value, 1.0, rel_tol=0.0, abs_tol=_EPS):
        msg = f"{label} of exactly 1.0 has no risk and no payout"
        raise InvalidOddsError(msg)
    return value


def _validate_commission(commission_rate: float) -> float:
    """Reject a commission rate that is not a fraction in ``[0, 0.20]``.

    Raises:
        CommissionError: Rate is NaN, negative, a bool, or above the ceiling.
    """
    if isinstance(commission_rate, bool):
        msg = "commission_rate must be numeric, got bool"
        raise CommissionError(msg)
    rate = float(commission_rate)
    if not math.isfinite(rate):
        msg = f"commission_rate must be finite, got {commission_rate!r}"
        raise CommissionError(msg)
    if rate < -_EPS:
        msg = f"commission_rate must be non-negative, got {rate!r}"
        raise CommissionError(msg)
    if rate > MAX_COMMISSION_RATE + _EPS:
        msg = (
            f"commission_rate {rate!r} exceeds {MAX_COMMISSION_RATE}. "
            "Express commission as a fraction (0.02), not a percentage (2)."
        )
        raise CommissionError(msg)
    return max(rate, 0.0)


def _validate_money(amount: Decimal | float | int | str, *, label: str) -> Decimal:
    """Coerce to ``Decimal`` via ``str`` so binary float error never enters money.

    Raises:
        ExchangeMathError: Amount is negative or not a finite number.
    """
    if isinstance(amount, bool):
        msg = f"{label} must be numeric, got bool"
        raise ExchangeMathError(msg)
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (ArithmeticError, ValueError) as exc:
        msg = f"{label} is not a valid decimal amount: {amount!r}"
        raise ExchangeMathError(msg) from exc
    if not value.is_finite():
        msg = f"{label} must be finite, got {amount!r}"
        raise ExchangeMathError(msg)
    if value < 0:
        msg = f"{label} must be non-negative, got {value}"
        raise ExchangeMathError(msg)
    return value


# --------------------------------------------------------------------------- #
# Core pricing
# --------------------------------------------------------------------------- #


def effective_back_odds(raw_odds: float, commission_rate: float) -> float:
    """True decimal odds of a BACK bet after commission on net winnings.

    Commission is levied on profit, not on the returned stake, so the stake
    portion of the payout is untaxed::

        O_eff = 1 + (O - 1) * (1 - c)

    Args:
        raw_odds: Quoted decimal odds, in ``[1.01, 1000]``.
        commission_rate: Commission as a fraction, e.g. ``0.02`` for 2%.

    Returns:
        Commission-adjusted decimal odds, always in ``(1.0, raw_odds]``.

    Raises:
        InvalidOddsError: ``raw_odds`` is untradable.
        CommissionError: ``commission_rate`` is not a valid fraction.

    Examples:
        >>> round(effective_back_odds(3.0, 0.02), 6)
        2.96
        >>> effective_back_odds(2.5, 0.0)
        2.5
    """
    odds = _validate_odds(raw_odds, label="raw_odds")
    rate = _validate_commission(commission_rate)

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        gross = Decimal(str(odds)) - Decimal(1)
        net = gross * (Decimal(1) - Decimal(str(rate)))
        result = float(Decimal(1) + net)

    # Structural invariant: commission can only ever shorten a price.
    if result > odds + _EPS or result <= 1.0:
        msg = f"effective_back_odds produced an incoherent price {result!r}"
        raise ExchangeMathError(msg)
    return result


def lay_liability(lay_odds: float, backer_stake: float) -> float:
    """Capital at risk when we act as the bookmaker.

    If the outcome we laid wins, we owe the backer their net winnings::

        L = S_backer * (O_lay - 1)

    Commission is deliberately absent: commission applies to *winnings*, and a
    losing lay produces none. Liability is the gross, untaxed exposure and is
    the number the risk engine must reserve against.

    Args:
        lay_odds: Quoted decimal odds we are laying at.
        backer_stake: The stake we are accepting from the backer.

    Returns:
        Liability as a float, rounded UP to the cent so we never under-reserve.

    Raises:
        InvalidOddsError: ``lay_odds`` is untradable.
        ExchangeMathError: ``backer_stake`` is negative or non-finite.

    Examples:
        >>> lay_liability(3.0, 100.0)
        200.0
        >>> lay_liability(1.91, 33.33)
        30.34
    """
    return float(lay_liability_exact(lay_odds, backer_stake))


def lay_liability_exact(
    lay_odds: float, backer_stake: Decimal | float | int | str
) -> Decimal:
    """Ledger-grade :func:`lay_liability`, rounded UP to the cent.

    Returns:
        Exact ``Decimal`` liability quantised to ``0.01`` with ``ROUND_UP``.
    """
    odds = _validate_odds(lay_odds, label="lay_odds")
    stake = _validate_money(backer_stake, label="backer_stake")

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        liability = stake * (Decimal(str(odds)) - Decimal(1))
        return liability.quantize(_MONEY, rounding=ROUND_UP)


def effective_lay_odds(raw_odds: float, commission_rate: float) -> float:
    """True decimal odds obtained by LAYING an outcome, net of commission.

    Laying is backing the complement. Our capital at risk is the liability
    ``S(O - 1)``; our net winnings when the outcome loses are ``S(1 - c)``,
    because commission is charged on the backer's forfeited stake, which is our
    profit. Therefore::

        O_lay_eff = (L + profit) / L
                  = (S(O - 1) + S(1 - c)) / (S(O - 1))
                  = (O - c) / (O - 1)

    The backer's stake ``S`` cancels, so the effective price is scale-free. With
    ``c = 0`` this collapses to the familiar ``O / (O - 1)``.

    Once converted, a lay price is directly comparable to a back price on the
    opposite outcome and can be fed to an arbitrage LP or a Kelly optimiser
    without any further adjustment.

    Args:
        raw_odds: Quoted decimal lay odds, in ``[1.01, 1000]``.
        commission_rate: Commission as a fraction, e.g. ``0.05`` for 5%.

    Returns:
        Commission-adjusted decimal odds for the complementary outcome.

    Raises:
        InvalidOddsError: ``raw_odds`` is untradable.
        CommissionError: ``commission_rate`` is not a valid fraction.

    Examples:
        >>> effective_lay_odds(3.0, 0.05)
        1.475
        >>> effective_lay_odds(3.0, 0.0)
        1.5
        >>> round(effective_lay_odds(1.5, 0.02), 6)
        2.96
    """
    odds = _validate_odds(raw_odds, label="raw_odds")
    rate = _validate_commission(commission_rate)

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        numerator = Decimal(str(odds)) - Decimal(str(rate))
        denominator = Decimal(str(odds)) - Decimal(1)
        if denominator <= Decimal(str(_EPS)):
            msg = f"lay odds {odds!r} leave no risk capital to price against"
            raise InvalidOddsError(msg)
        result = float(numerator / denominator)

    if result <= 1.0:
        # Occurs only if commission >= 1, which the guards already exclude.
        msg = f"effective_lay_odds produced a non-viable price {result!r}"
        raise ExchangeMathError(msg)
    return result


def effective_odds(raw_odds: float, commission_rate: float, side: BetSide) -> float:
    """Dispatch to the correct commission adjustment for ``side``.

    Use this at every call site instead of choosing by hand: a lay priced with
    the back formula looks like a 3% edge that does not exist.

    Args:
        raw_odds: Quoted decimal odds.
        commission_rate: Commission as a fraction.
        side: :class:`BetSide.BACK` or :class:`BetSide.LAY`.

    Returns:
        Commission-adjusted decimal odds in the units of the risk taken.

    Raises:
        ExchangeMathError: ``side`` is not a recognised :class:`BetSide`.
    """
    match side:
        case BetSide.BACK:
            return effective_back_odds(raw_odds, commission_rate)
        case BetSide.LAY:
            return effective_lay_odds(raw_odds, commission_rate)
        case _:  # pragma: no cover - unreachable while BetSide is closed
            msg = f"unsupported bet side {side!r}"
            raise ExchangeMathError(msg)


def implied_probability(raw_odds: float, commission_rate: float, side: BetSide) -> float:
    """Commission-adjusted implied probability of the risk actually taken.

    Warning:
        This is a *gross* implied probability that still contains the venue's
        margin. Never treat it as a fair probability. Devig a complete book
        with Shin or the power method first.

    Returns:
        ``1 / effective_odds``, in ``(0, 1)``.
    """
    return 1.0 / effective_odds(raw_odds, commission_rate, side)


# --------------------------------------------------------------------------- #
# Position sizing
# --------------------------------------------------------------------------- #


def backer_stake_for_liability(
    lay_odds: float, target_liability: Decimal | float | int | str
) -> Decimal:
    """Invert :func:`lay_liability`: the backer stake for a liability budget.

    Used by the risk engine to size a lay against a hard exposure cap::

        S_backer = L_target / (O_lay - 1)

    Returns:
        Backer stake quantised DOWN to the cent, so the resulting liability can
        never exceed ``target_liability``.

    Raises:
        InvalidOddsError: ``lay_odds`` is untradable.
        ExchangeMathError: ``target_liability`` is negative or non-finite.
    """
    odds = _validate_odds(lay_odds, label="lay_odds")
    liability = _validate_money(target_liability, label="target_liability")

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        stake = liability / (Decimal(str(odds)) - Decimal(1))
        return stake.quantize(_MONEY, rounding=ROUND_DOWN)


def lay_profit_exact(
    lay_odds: float,
    backer_stake: Decimal | float | int | str,
    commission_rate: float,
) -> Decimal:
    """Net profit when the laid outcome LOSES, after commission.

    Our winnings are the backer's forfeited stake, taxed at ``c``::

        profit = S_backer * (1 - c)

    Returns:
        Profit quantised DOWN to the cent, so P&L is never overstated.
    """
    _validate_odds(lay_odds, label="lay_odds")
    stake = _validate_money(backer_stake, label="backer_stake")
    rate = _validate_commission(commission_rate)

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        profit = stake * (Decimal(1) - Decimal(str(rate)))
        return profit.quantize(_MONEY, rounding=ROUND_DOWN)


def hedge_lay_backer_stake(
    *,
    back_stake: Decimal | float | int | str,
    back_odds: float,
    back_commission_rate: float,
    lay_odds: float,
    lay_commission_rate: float,
) -> Decimal:
    """Backer stake that equalises P&L across both outcomes ("greening up").

    Solve for ``S_l`` such that the profit is identical whether the selection
    wins or loses::

        win:  S_b (O_b - 1)(1 - c_b) - S_l (O_l - 1)
        lose: -S_b + S_l (1 - c_l)

        =>  S_l = S_b * [1 + (O_b - 1)(1 - c_b)] / (O_l - c_l)
                = S_b * O_b_eff / (O_l - c_l)

    Note that ``O_l - c_l = O_lay_eff * (O_l - 1)``, which ties this directly
    back to :func:`effective_lay_odds`.

    Args:
        back_stake: Stake already matched on the back side.
        back_odds: Decimal odds of the matched back bet.
        back_commission_rate: Commission at the venue holding the back bet.
        lay_odds: Decimal odds currently available to lay.
        lay_commission_rate: Commission at the exchange we are laying on.

    Returns:
        Backer stake to accept, quantised DOWN to the cent. Rounding down
        slightly under-hedges, which leaves residual exposure on the back side
        rather than creating a new naked lay position.

    Raises:
        InvalidOddsError: Either price is untradable.
        CommissionError: Either commission rate is invalid.
    """
    stake = _validate_money(back_stake, label="back_stake")
    lay = _validate_odds(lay_odds, label="lay_odds")
    lay_rate = _validate_commission(lay_commission_rate)
    back_eff = effective_back_odds(back_odds, back_commission_rate)

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        denominator = Decimal(str(lay)) - Decimal(str(lay_rate))
        if denominator <= Decimal(str(_EPS)):
            msg = f"lay odds {lay!r} net of commission leave no hedge capacity"
            raise InvalidOddsError(msg)
        hedge = stake * Decimal(str(back_eff)) / denominator
        return hedge.quantize(_MONEY, rounding=ROUND_DOWN)
