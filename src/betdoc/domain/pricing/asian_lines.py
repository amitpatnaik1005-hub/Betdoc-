"""Asian handicap and totals settlement, evaluated in exact integer arithmetic.

The loophole this module closes
-------------------------------
Asian lines are multiples of 0.25, and 0.25 is one of the few decimals that is
exactly representable in binary floating point. The danger is not 0.25 itself:
it is ``-0.75``, ``2.25``, and the *sums* formed while adjusting a scoreline,
where accumulated error can flip a comparison and settle a HALF_WIN as a PUSH.

The fix is to leave floating point entirely before any win state is decided.
Every line is converted once into **integer quarter-units** (``line * 4``),
validated with :func:`math.isclose`, and every subsequent comparison is integer.
There is no epsilon in the settlement path because there is no float in the
settlement path.

Quarter-line decomposition
--------------------------
A quarter line is two half-stake bets on the two adjacent half-or-whole lines.
In quarter-units, a line ``q`` that is odd decomposes into ``q - 1`` and
``q + 1``, both even. Settling each half and summing gives a score in
``{-2, -1, 0, +1, +2}``, which maps one-to-one onto :class:`PayoutResult`.

Worked examples, all verified by the unified mapping below:

===================  ==========  ==============
Bet                  Scoreline   Result
===================  ==========  ==============
Home -0.25           0-0 draw    HALF_LOSS
Home +0.25           0-0 draw    HALF_WIN
Home -0.75           1-0 win     HALF_WIN
Home -0.25           1-0 win     FULL_WIN
Over 2.25            2 goals     HALF_LOSS
Over 2.25            3 goals     FULL_WIN
Over 2.50            2 goals     FULL_LOSS
Under 3.00           3 goals     PUSH
===================  ==========  ==============
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN, localcontext
from enum import StrEnum
from typing import Final

__all__ = [
    "AsianLineError",
    "InvalidLineError",
    "InvalidScoreError",
    "MAX_ABS_LINE",
    "PayoutResult",
    "evaluate_handicap",
    "evaluate_totals",
    "settle",
]

_QUARTER_UNITS: Final[int] = 4
_LINE_TOLERANCE: Final[float] = 1e-6
"""Tolerance used ONCE, when crossing from float input to integer units."""

MAX_ABS_LINE: Final[float] = 100.0
"""Sanity ceiling. No real handicap or total exceeds this."""

MAX_SCORE: Final[int] = 1_000
"""Sanity ceiling on a single side's score, guarding against parsing errors."""

_MONEY: Final[Decimal] = Decimal("0.01")
_INTERNAL_PRECISION: Final[int] = 28


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class AsianLineError(ValueError):
    """Base class for every rejected Asian line evaluation."""


class InvalidLineError(AsianLineError):
    """The line is non-finite, out of range, or not a multiple of 0.25."""


class InvalidScoreError(AsianLineError):
    """A score is negative, non-integral, a bool, or implausibly large."""


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


class PayoutResult(StrEnum):
    """The five, and only five, settlement states of an Asian line bet.

    A strict enum rather than a float multiplier, so an unhandled branch fails
    loudly at the ``match`` statement instead of silently settling at 0.0.

    Two independent fractions fully describe the payout:

    * :attr:`stake_refund_fraction` - portion of stake returned unstaked.
    * :attr:`win_fraction` - portion of stake that settled at full odds.

    Total return is therefore
    ``stake * (refund + win_fraction * odds)``, which reduces correctly for all
    five states.
    """

    FULL_WIN = "full_win"
    HALF_WIN = "half_win"
    PUSH = "push"
    HALF_LOSS = "half_loss"
    FULL_LOSS = "full_loss"

    @property
    def win_fraction(self) -> Decimal:
        """Fraction of the stake that settled as a winner at full odds."""
        return {
            PayoutResult.FULL_WIN: Decimal("1.0"),
            PayoutResult.HALF_WIN: Decimal("0.5"),
            PayoutResult.PUSH: Decimal("0.0"),
            PayoutResult.HALF_LOSS: Decimal("0.0"),
            PayoutResult.FULL_LOSS: Decimal("0.0"),
        }[self]

    @property
    def stake_refund_fraction(self) -> Decimal:
        """Fraction of the stake returned without profit (the pushed portion)."""
        return {
            PayoutResult.FULL_WIN: Decimal("0.0"),
            PayoutResult.HALF_WIN: Decimal("0.5"),
            PayoutResult.PUSH: Decimal("1.0"),
            PayoutResult.HALF_LOSS: Decimal("0.5"),
            PayoutResult.FULL_LOSS: Decimal("0.0"),
        }[self]

    @property
    def is_void_in_part(self) -> bool:
        """True when any portion of the stake was returned rather than settled."""
        return self in (PayoutResult.HALF_WIN, PayoutResult.PUSH, PayoutResult.HALF_LOSS)

    @property
    def inverse(self) -> PayoutResult:
        """The result the opposing side of the same market receives.

        Asian markets are zero-sum in settlement state, so this is an exact
        involution and is the cheapest available consistency check.
        """
        return {
            PayoutResult.FULL_WIN: PayoutResult.FULL_LOSS,
            PayoutResult.HALF_WIN: PayoutResult.HALF_LOSS,
            PayoutResult.PUSH: PayoutResult.PUSH,
            PayoutResult.HALF_LOSS: PayoutResult.HALF_WIN,
            PayoutResult.FULL_LOSS: PayoutResult.FULL_WIN,
        }[self]


#: Score in half-stake units -> settlement state. The single source of truth.
_SCORE_TO_RESULT: Final[dict[int, PayoutResult]] = {
    2: PayoutResult.FULL_WIN,
    1: PayoutResult.HALF_WIN,
    0: PayoutResult.PUSH,
    -1: PayoutResult.HALF_LOSS,
    -2: PayoutResult.FULL_LOSS,
}


# --------------------------------------------------------------------------- #
# Exact conversion and guards
# --------------------------------------------------------------------------- #


def _to_quarter_units(line: float, *, label: str = "line") -> int:
    """Convert a line to exact integer quarter-units, or reject it.

    This is the ONLY place a float touches the settlement path. Everything
    downstream is integer arithmetic and therefore exact.

    Args:
        line: The line, expected to be a multiple of 0.25.
        label: Field name used in error messages.

    Returns:
        ``round(line * 4)`` as an ``int``.

    Raises:
        InvalidLineError: Not finite, out of range, or not a multiple of 0.25.
    """
    if isinstance(line, bool):
        msg = f"{label} must be numeric, got bool"
        raise InvalidLineError(msg)
    value = float(line)
    if not math.isfinite(value):
        msg = f"{label} must be finite, got {line!r}"
        raise InvalidLineError(msg)
    if abs(value) > MAX_ABS_LINE:
        msg = f"{label} {value!r} exceeds the plausible maximum {MAX_ABS_LINE}"
        raise InvalidLineError(msg)

    scaled = value * _QUARTER_UNITS
    units = round(scaled)
    if not math.isclose(scaled, units, rel_tol=0.0, abs_tol=_LINE_TOLERANCE):
        msg = (
            f"{label} {value!r} is not a multiple of 0.25 "
            f"(line * 4 = {scaled!r}, nearest integer {units})"
        )
        raise InvalidLineError(msg)
    return int(units)


def _validate_score(score: int, *, label: str) -> int:
    """Reject a score that is not a plausible non-negative integer.

    Raises:
        InvalidScoreError: Bool, non-integral, negative, or above :data:`MAX_SCORE`.
    """
    if isinstance(score, bool):
        msg = f"{label} must be an int, got bool"
        raise InvalidScoreError(msg)
    if not isinstance(score, int):
        msg = f"{label} must be an int, got {type(score).__name__}"
        raise InvalidScoreError(msg)
    if score < 0:
        msg = f"{label} must be non-negative, got {score}"
        raise InvalidScoreError(msg)
    if score > MAX_SCORE:
        msg = f"{label} {score} exceeds the plausible maximum {MAX_SCORE}"
        raise InvalidScoreError(msg)
    return score


def _sign(value: int) -> int:
    """Integer sign: ``+1`` above the line, ``0`` exactly on it, ``-1`` below."""
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _settle_quarter_units(value_units: int, line_units: int) -> PayoutResult:
    """Settle a bet from its margin-plus-line value, in quarter-units.

    Args:
        value_units: ``margin * 4 + line * 4`` for the side being settled.
            Strictly positive means the bet is beating its line.
        line_units: The side's own line in quarter-units, used only to detect
            whether the bet must be split into two halves.

    Returns:
        The exact :class:`PayoutResult`.

    Notes:
        For a whole line (``line_units % 4 == 0``) a value of exactly zero is a
        PUSH. For a half line (``line_units % 4 == 2``) the value is congruent
        to 2 mod 4 and can never be zero, so no push is reachable. Both cases
        are handled by the same doubled-sign expression, which keeps one code
        path for all three line families.
    """
    if line_units % 2 == 0:
        # Whole or half line: a single, undivided bet.
        score = 2 * _sign(value_units)
    else:
        # Quarter line: two half-stake bets on the adjacent even lines,
        # which shift the value by exactly one quarter-unit either way.
        score = _sign(value_units - 1) + _sign(value_units + 1)

    result = _SCORE_TO_RESULT.get(score)
    if result is None:  # pragma: no cover - unreachable, score is in [-2, 2]
        msg = f"settlement produced an impossible score {score} for value {value_units}"
        raise AsianLineError(msg)
    return result


# --------------------------------------------------------------------------- #
# Public settlement API
# --------------------------------------------------------------------------- #


def evaluate_handicap(
    line: float, home_score: int, away_score: int, bet_on_home: bool
) -> PayoutResult:
    """Settle an Asian handicap bet.

    Convention:
        ``line`` is the handicap applied to the **HOME** side, matching the
        market convention where "Home -1.5" is expressed as ``line = -1.5``.
        The away side therefore plays to ``-line``, which makes the two sides'
        values exact negations of each other and guarantees that
        ``evaluate_handicap(..., True).inverse == evaluate_handicap(..., False)``
        for every input. That identity is worth asserting in a property test.

    Args:
        line: Handicap granted to the home team, a multiple of 0.25.
        home_score: Final home goals, a non-negative int.
        away_score: Final away goals, a non-negative int.
        bet_on_home: True if the stake is on the home side, False for away.

    Returns:
        The exact :class:`PayoutResult` for the side staked.

    Raises:
        InvalidLineError: ``line`` is not a valid Asian line.
        InvalidScoreError: Either score is not a plausible non-negative int.

    Examples:
        >>> evaluate_handicap(-0.25, 0, 0, bet_on_home=True)
        <PayoutResult.HALF_LOSS: 'half_loss'>
        >>> evaluate_handicap(-0.75, 1, 0, bet_on_home=True)
        <PayoutResult.HALF_WIN: 'half_win'>
        >>> evaluate_handicap(-1.0, 2, 1, bet_on_home=True)
        <PayoutResult.PUSH: 'push'>
        >>> evaluate_handicap(0.5, 1, 1, bet_on_home=False)
        <PayoutResult.FULL_LOSS: 'full_loss'>
    """
    if not isinstance(bet_on_home, bool):
        msg = f"bet_on_home must be a bool, got {type(bet_on_home).__name__}"
        raise AsianLineError(msg)

    home_line_units = _to_quarter_units(line, label="line")
    home = _validate_score(home_score, label="home_score")
    away = _validate_score(away_score, label="away_score")

    # Goal margin in quarter-units. Exact: scores are ints.
    margin_units = (home - away) * _QUARTER_UNITS

    if bet_on_home:
        side_line_units = home_line_units
        value_units = margin_units + home_line_units
    else:
        # The away side receives the mirrored handicap, so both the value and
        # the line negate. Parity is preserved, so quarter lines stay quarter.
        side_line_units = -home_line_units
        value_units = -(margin_units + home_line_units)

    return _settle_quarter_units(value_units, side_line_units)


def evaluate_totals(line: float, total_goals: int, bet_over: bool) -> PayoutResult:
    """Settle an Asian Over/Under (totals) bet.

    Over wins when ``total_goals > line``, Under wins when ``total_goals < line``,
    and a whole line landing exactly on the total is a PUSH. Quarter lines split
    into two half-stake bets exactly as in :func:`evaluate_handicap`.

    Args:
        line: The Over/Under threshold, a multiple of 0.25 and non-negative.
        total_goals: Combined goals scored, a non-negative int.
        bet_over: True if the stake is on Over, False for Under.

    Returns:
        The exact :class:`PayoutResult` for the side staked.

    Raises:
        InvalidLineError: ``line`` is not a valid Asian line, or is negative.
        InvalidScoreError: ``total_goals`` is not a plausible non-negative int.

    Examples:
        >>> evaluate_totals(2.25, 2, bet_over=True)
        <PayoutResult.HALF_LOSS: 'half_loss'>
        >>> evaluate_totals(2.25, 3, bet_over=True)
        <PayoutResult.FULL_WIN: 'full_win'>
        >>> evaluate_totals(3.0, 3, bet_over=False)
        <PayoutResult.PUSH: 'push'>
        >>> evaluate_totals(2.75, 3, bet_over=False)
        <PayoutResult.HALF_LOSS: 'half_loss'>
    """
    if not isinstance(bet_over, bool):
        msg = f"bet_over must be a bool, got {type(bet_over).__name__}"
        raise AsianLineError(msg)

    line_units = _to_quarter_units(line, label="line")
    if line_units < 0:
        msg = f"a totals line cannot be negative, got {line!r}"
        raise InvalidLineError(msg)
    total = _validate_score(total_goals, label="total_goals")

    total_units = total * _QUARTER_UNITS

    if bet_over:
        # Over beats the line when total - line > 0.
        value_units = total_units - line_units
    else:
        # Under beats the line when line - total > 0. Parity of the line is
        # unchanged by negation, so the split logic applies identically.
        value_units = line_units - total_units

    return _settle_quarter_units(value_units, line_units)


def settle(
    result: PayoutResult,
    stake: Decimal | float | int | str,
    decimal_odds: float,
) -> Decimal:
    """Total amount returned to us for a settled bet, refund included.

    ``total_return = stake * (refund_fraction + win_fraction * odds)``

    Args:
        result: Settlement state from :func:`evaluate_handicap` or
            :func:`evaluate_totals`.
        stake: Amount staked.
        decimal_odds: Decimal odds the bet was struck at, strictly above 1.0.
            Pass commission-adjusted odds for exchange positions.

    Returns:
        Total return quantised DOWN to the cent, so realised P&L is never
        overstated. Profit is ``settle(...) - stake``.

    Raises:
        AsianLineError: Stake is negative or non-finite, or odds are invalid.

    Examples:
        >>> settle(PayoutResult.HALF_WIN, Decimal("100"), 2.0)
        Decimal('150.00')
        >>> settle(PayoutResult.HALF_LOSS, Decimal("100"), 2.0)
        Decimal('50.00')
        >>> settle(PayoutResult.PUSH, Decimal("100"), 2.0)
        Decimal('100.00')
        >>> settle(PayoutResult.FULL_LOSS, Decimal("100"), 2.0)
        Decimal('0.00')
    """
    if not isinstance(result, PayoutResult):
        msg = f"result must be a PayoutResult, got {type(result).__name__}"
        raise AsianLineError(msg)
    if isinstance(stake, bool) or isinstance(decimal_odds, bool):
        msg = "stake and decimal_odds must be numeric, got bool"
        raise AsianLineError(msg)

    try:
        stake_amount = stake if isinstance(stake, Decimal) else Decimal(str(stake))
    except (ArithmeticError, ValueError) as exc:
        msg = f"stake is not a valid decimal amount: {stake!r}"
        raise AsianLineError(msg) from exc
    if not stake_amount.is_finite() or stake_amount < 0:
        msg = f"stake must be a finite, non-negative amount, got {stake!r}"
        raise AsianLineError(msg)

    odds = float(decimal_odds)
    if not math.isfinite(odds) or odds <= 1.0:
        msg = f"decimal_odds must be finite and greater than 1.0, got {decimal_odds!r}"
        raise AsianLineError(msg)

    with localcontext() as ctx:
        ctx.prec = _INTERNAL_PRECISION
        multiplier = result.stake_refund_fraction + result.win_fraction * Decimal(str(odds))
        return (stake_amount * multiplier).quantize(_MONEY, rounding=ROUND_DOWN)
