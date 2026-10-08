"""ARYABHATA: the quant engine. Overround, tiered de-vig, consensus, EV and capped fractional Kelly.

Everything here is pure and runs on ``decimal.Decimal`` inside one fixed context (34 significant
digits, banker's rounding, every arithmetic fault trapped), so the result never depends on the
caller's global decimal context and a bad number raises instead of turning into NaN. Inputs pass
through ``to_decimal``: NaN, infinities, booleans and garbage become ``None`` before any maths runs.

Pipeline for one market (``evaluate_market``):

1. Overround: each book's implied probabilities ``1/odds`` are summed (the booksum); the margin is
   ``booksum - 1``.
2. De-vig each book in tiers: Shin (models the favourite/longshot bias), then margin proportional
   to odds (MPO), then multiplicative, which always works for a valid book. A method whose
   preconditions fail hands over to the next; ``DevigResult.fallbacks`` records why.
3. Consensus: per selection, the median of every fresh book's fair probability after Tukey-fence
   outlier rejection, renormalised to sum to exactly 1.
4. Edge: the best fresh price per selection against the consensus. ``EV = p * odds - 1``; anything
   under ``MIN_EV`` is noise, anything over ``MAX_PLAUSIBLE_EV`` is a bad quote, not an edge.
5. Stake (``recommend_stake``, per user): ``kelly_multiplier * f*`` of the bankroll, never above
   the bankroll % cap from the Control Panel or its absolute max bet, rounded down to the paisa.
"""

from __future__ import annotations

import functools
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import ParamSpec, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from app.schemas.aryabhata import SIGNAL_TTL, DevigMethod, EdgeSignal, StakeBinding

# ---------------------------------------------------------------- hardcoded safety rails
MIN_EV = Decimal("0.005")  # below +0.5% EV a line is variance noise: discarded, never staked
MAX_PLAUSIBLE_EV = Decimal("0.25")  # above +25% it is a stale or palpable-error quote, not an edge
MIN_BOOKS = 2  # a consensus needs at least two independent books
MIN_STAKE_PCT = Decimal("1")  # the Control Panel cap slider's range, enforced here too
MAX_STAKE_PCT = Decimal("10")
PAISA = Decimal("0.01")
CLOCK_SKEW = timedelta(seconds=5)  # quotes stamped slightly in the future are still fresh

ARYABHATA_NAMESPACE = uuid5(NAMESPACE_URL, "betdoc:aryabhata")
DEVIG_ORDER: dict[DevigMethod, tuple[DevigMethod, ...]] = {
    "shin": ("shin", "mpo", "multiplicative"),
    "mpo": ("mpo", "multiplicative"),
    "multiplicative": ("multiplicative",),
}
LABEL_ORDER = {"HOME": 0, "DRAW": 1, "AWAY": 2}

_CONTEXT = Context(
    prec=34,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)
_ZERO, _ONE, _TWO, _FOUR = Decimal(0), Decimal(1), Decimal(2), Decimal(4)
_HUNDRED = Decimal(100)
_SHIN_TOLERANCE = Decimal("1e-26")
_SHIN_MAX_ITERATIONS = 200
_TUKEY_K = Decimal("1.5")
_OUTLIER_SLACK = Decimal("0.02")  # within 2% of the median is never an outlier, whatever the IQR
_PROB_QUANTUM = Decimal("1e-10")
_EV_PCT_QUANTUM = Decimal("0.0001")
_FRACTION_QUANTUM = Decimal("1e-8")

P = ParamSpec("P")
R = TypeVar("R")


def _exact(func: Callable[P, R]) -> Callable[P, R]:
    """Run under the engine's own decimal context, whatever context the caller set."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with localcontext(_CONTEXT):
            return func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------- errors
class AryabhataError(ValueError):
    """Base for every engine rejection."""


class InvalidMarketError(AryabhataError):
    """The book cannot be priced at all: too few outcomes, or odds missing, non-finite or <= 1."""


class DevigFailure(AryabhataError):
    """One de-vig method's preconditions do not hold for this book; the next tier takes over."""


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Sum without intermediate rounding (the context's 34 digits would round 0.5000..3 + 0.5000..3,
    and a normalised book would then miss 1 in its last digit). The next operation rounds once."""
    with localcontext(_CONTEXT) as ctx:
        ctx.prec = _CONTEXT.prec + 30
        return sum(values, _ZERO)


# ---------------------------------------------------------------- inputs
def to_decimal(value: object) -> Decimal | None:
    """A finite Decimal, or None for anything unusable (NaN, +/-inf, bool, None, garbage)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        number = Decimal(repr(value))  # the shortest repr: 2.1 -> Decimal("2.1"), not its binary expansion
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return number if number.is_finite() else None


@_exact
def implied_probabilities(odds: Sequence[object]) -> tuple[Decimal, ...]:
    """``1/odds`` per outcome. Raises ``InvalidMarketError`` unless every price is a finite odds > 1."""
    if len(odds) < 2:
        raise InvalidMarketError("a market needs at least two mutually exclusive outcomes")
    implied: list[Decimal] = []
    for raw in odds:
        price = to_decimal(raw)
        if price is None:
            raise InvalidMarketError("odds must be finite numbers")
        if price <= _ONE:
            raise InvalidMarketError("decimal odds must be greater than 1")
        implied.append(_ONE / price)
    return tuple(implied)


@_exact
def booksum(odds: Sequence[object]) -> Decimal:
    """Sum of implied probabilities over every mutually exclusive outcome (1 = a fair book)."""
    return _exact_sum(implied_probabilities(odds))


@_exact
def overround(odds: Sequence[object]) -> Decimal:
    """The bookmaker margin: ``booksum - 1`` (0.05 = a 5% overround; negative = an arbitrage book)."""
    return booksum(odds) - _ONE


# ---------------------------------------------------------------- de-vig methods
def _normalise(probabilities: Sequence[Decimal]) -> tuple[Decimal, ...]:
    total = _exact_sum(probabilities)
    if total <= _ZERO:
        raise DevigFailure("probabilities do not sum to a positive total")
    return tuple(p / total for p in probabilities)


def _check_open_unit(probabilities: Sequence[Decimal], method: str) -> None:
    if any(not _ZERO < p < _ONE for p in probabilities):
        raise DevigFailure(f"{method}: produced a probability outside (0, 1)")


def _shin_sum(z: Decimal, implied: Sequence[Decimal], total: Decimal) -> Decimal:
    """Sum of Shin's fair probabilities at insider share ``z`` (1 at the solution)."""
    one_minus_z = _ONE - z
    denominator = _TWO * one_minus_z
    return _exact_sum(((z * z + _FOUR * one_minus_z * q * q / total).sqrt() - z) / denominator for q in implied)


@_exact
def shin_probabilities(odds: Sequence[object]) -> tuple[tuple[Decimal, ...], Decimal]:
    """Shin (1993): fair probabilities and the insider-trading share ``z``.

    ``p_i = (sqrt(z^2 + 4(1-z) q_i^2 / B) - z) / (2(1-z))`` with ``q_i = 1/odds_i`` and ``B`` the
    booksum; ``z`` in [0, 1) solves ``sum(p_i) = 1``. Longshots lose more probability than
    favourites, which is the favourite/longshot bias the method models. The sum falls monotonically
    from ``sqrt(B) > 1`` at ``z = 0``, so the root is bracketed and found by Illinois regula falsi.
    """
    implied = implied_probabilities(odds)
    total = _exact_sum(implied)
    if total <= _ONE:
        raise DevigFailure("shin: needs an overround (booksum > 1)")

    def excess(z: Decimal) -> Decimal:
        return _shin_sum(z, implied, total) - _ONE

    a, fa = _ZERO, excess(_ZERO)
    b = Decimal("0.5")
    fb = excess(b)
    for _ in range(64):  # walk the upper bracket towards 1 until the sum drops below 1
        if fb <= _ZERO:
            break
        a, fa = b, fb
        b = (b + _ONE) / _TWO
        fb = excess(b)
    else:
        raise DevigFailure("shin: could not bracket the insider share")

    z = b
    if fb != _ZERO:
        for _ in range(_SHIN_MAX_ITERATIONS):
            z = b - fb * (b - a) / (fb - fa)
            fz = excess(z)
            if abs(fz) <= _SHIN_TOLERANCE or abs(b - a) <= _SHIN_TOLERANCE:
                break
            if (fz < _ZERO) != (fb < _ZERO):
                a, fa = b, fb  # the root now lies between b and z
            else:
                fa = fa / _TWO  # Illinois step: stop the stale end from stalling convergence
            b, fb = z, fz
        else:
            raise DevigFailure("shin: did not converge")

    if not _ZERO <= z < _ONE:
        raise DevigFailure("shin: insider share outside [0, 1)")
    one_minus_z = _ONE - z
    fair = [((z * z + _FOUR * one_minus_z * q * q / total).sqrt() - z) / (_TWO * one_minus_z) for q in implied]
    _check_open_unit(fair, "shin")
    return _normalise(fair), z


@_exact
def mpo_probabilities(odds: Sequence[object]) -> tuple[Decimal, ...]:
    """Margin proportional to odds (Buchdahl): ``fair_odds_i = n * o_i / (n - M * o_i)``.

    ``M`` is the margin and ``n`` the number of outcomes. A longshot priced at or beyond ``n / M``
    has no fair odds under this model (its share of the margin would exceed its probability), so
    such a book falls through to the multiplicative method.
    """
    implied = implied_probabilities(odds)
    prices = [_ONE / q for q in implied]
    margin = _exact_sum(implied) - _ONE
    if margin <= _ZERO:
        raise DevigFailure("mpo: needs an overround (booksum > 1)")
    n = Decimal(len(prices))
    fair: list[Decimal] = []
    for price in prices:
        denominator = n - margin * price
        if denominator <= _ZERO:
            raise DevigFailure("mpo: a longshot's odds exceed n / margin")
        fair.append(denominator / (n * price))  # 1 / fair_odds
    _check_open_unit(fair, "mpo")
    return _normalise(fair)


@_exact
def multiplicative_probabilities(odds: Sequence[object]) -> tuple[Decimal, ...]:
    """Basic multiplicative (proportional) method: ``p_i = q_i / B``. Defined for every valid book."""
    implied = implied_probabilities(odds)
    fair = _normalise(implied)
    _check_open_unit(fair, "multiplicative")
    return fair


@dataclass(frozen=True, slots=True)
class DevigResult:
    probabilities: tuple[Decimal, ...]
    method: DevigMethod
    booksum: Decimal
    overround: Decimal
    shin_z: Decimal | None = None
    fallbacks: tuple[str, ...] = ()  # why each preferred method was skipped


@_exact
def devig(odds: Sequence[object], preferred: DevigMethod = "shin") -> DevigResult:
    """Tiered de-vig: Shin, then MPO, then multiplicative. Raises only ``InvalidMarketError``."""
    total = booksum(odds)  # validates the whole book first
    fallbacks: list[str] = []
    for method in DEVIG_ORDER[preferred]:
        try:
            if method == "shin":
                fair, z = shin_probabilities(odds)
                return DevigResult(fair, "shin", total, total - _ONE, z, tuple(fallbacks))
            if method == "mpo":
                return DevigResult(mpo_probabilities(odds), "mpo", total, total - _ONE, None, tuple(fallbacks))
            return DevigResult(multiplicative_probabilities(odds), "multiplicative", total, total - _ONE, None, tuple(fallbacks))
        except DevigFailure as exc:
            fallbacks.append(str(exc))
        except ArithmeticError as exc:  # DecimalException, ZeroDivisionError, Overflow
            fallbacks.append(f"{method}: {type(exc).__name__}")
    raise InvalidMarketError("no de-vig method could price this book: " + "; ".join(fallbacks))


# ---------------------------------------------------------------- consensus
def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / _TWO


def _quantile(ordered: Sequence[Decimal], fraction: Decimal) -> Decimal:
    """Linear interpolation between closest ranks (the 'inclusive' definition)."""
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    if lower >= len(ordered) - 1:
        return ordered[-1]
    weight = position - lower
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * weight


@_exact
def robust_center(values: Sequence[Decimal]) -> Decimal:
    """Median after Tukey-fence rejection (1.5 x IQR). Needs four values to judge an outlier."""
    if not values:
        raise AryabhataError("no values to take a consensus of")
    if len(values) < 4:
        return _median(values)
    ordered = sorted(values)
    q1, q3 = _quantile(ordered, Decimal("0.25")), _quantile(ordered, Decimal("0.75"))
    spread = q3 - q1
    low, high = q1 - _TUKEY_K * spread, q3 + _TUKEY_K * spread
    median = _median(ordered)
    kept = [v for v in ordered if low <= v <= high or abs(v - median) <= _OUTLIER_SLACK * abs(median)]
    return _median(kept or ordered)


@_exact
def consensus_probabilities(books: Sequence[Mapping[str, Decimal]], labels: Sequence[str]) -> dict[str, Decimal]:
    """Per-selection robust median across books, renormalised so the market sums to exactly 1."""
    if not books or not labels:
        raise AryabhataError("a consensus needs books and selections")
    centers = {label: robust_center([book[label] for book in books]) for label in labels}
    total = _exact_sum(centers.values())
    if total <= _ZERO:
        raise AryabhataError("consensus probabilities sum to zero")
    return {label: centers[label] / total for label in labels}


# ---------------------------------------------------------------- edge and stake
@_exact
def expected_value(true_prob: object, odds: object) -> Decimal | None:
    """EV per unit staked: ``p * odds - 1`` (0.03 = +3%). None when either input is unusable."""
    p, price = to_decimal(true_prob), to_decimal(odds)
    if p is None or price is None or not _ZERO <= p <= _ONE or price <= _ONE:
        return None
    return p * price - _ONE


@_exact
def kelly_fraction(true_prob: object, odds: object) -> Decimal:
    """Full Kelly ``f* = (b*p - q) / b = (p*odds - 1) / (odds - 1)``; 0 for no edge or bad input."""
    ev = expected_value(true_prob, odds)
    price = to_decimal(odds)
    if ev is None or price is None or ev <= _ZERO:
        return _ZERO
    return min(ev / (price - _ONE), _ONE)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """The live limits a stake is sized with (read from Redis, falling back to the Control Panel row)."""

    kelly_multiplier: Decimal  # fractional Kelly: 0.25 = quarter Kelly
    max_stake_pct: Decimal  # cap as % of bankroll, clamped to [MIN_STAKE_PCT, MAX_STAKE_PCT]
    max_bet_size: Decimal | None = None  # execution's absolute per-order ceiling, when configured
    halted: bool = False  # the emergency stop: nothing is staked


@dataclass(frozen=True, slots=True)
class StakeDecision:
    stake_inr: Decimal
    fraction: Decimal  # stake / bankroll after every limit
    binding: StakeBinding


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


@_exact
def recommend_stake(full_kelly: object, bankroll: object, limits: RiskLimits) -> StakeDecision:
    """``min(multiplier * f*, cap%) * bankroll``, then the absolute max bet, rounded down to the paisa.

    Never negative and never above either cap: every path that cannot stake returns 0 with the reason.
    """
    f_star = to_decimal(full_kelly)
    if f_star is None or f_star <= _ZERO:
        return StakeDecision(_ZERO, _ZERO, "no_edge")
    if limits.halted:
        return StakeDecision(_ZERO, _ZERO, "halted")
    wealth = to_decimal(bankroll)
    if wealth is None or wealth <= _ZERO:
        return StakeDecision(_ZERO, _ZERO, "no_bankroll")
    multiplier = _clamp(to_decimal(limits.kelly_multiplier) or _ZERO, _ZERO, _ONE)
    if multiplier <= _ZERO:
        return StakeDecision(_ZERO, _ZERO, "kelly")

    fraction = min(f_star, _ONE) * multiplier
    cap = _clamp(to_decimal(limits.max_stake_pct) or MIN_STAKE_PCT, MIN_STAKE_PCT, MAX_STAKE_PCT) / _HUNDRED
    binding: StakeBinding = "kelly"
    if fraction > cap:
        fraction, binding = cap, "pct_cap"
    stake = wealth * fraction
    ceiling = to_decimal(limits.max_bet_size)
    if ceiling is not None and stake > max(ceiling, _ZERO):
        stake, binding = max(ceiling, _ZERO), "max_bet"
    stake = max(stake.quantize(PAISA, rounding=ROUND_DOWN), _ZERO)
    applied = (stake / wealth).quantize(_FRACTION_QUANTUM, rounding=ROUND_DOWN)
    return StakeDecision(stake, applied, binding)


# ---------------------------------------------------------------- one market
@dataclass(frozen=True, slots=True)
class BookLine:
    """One book's latest prices for a market, as the stream last saw them."""

    source: str
    bookmaker_id: str
    prices: Mapping[str, Decimal]
    seen_at: datetime
    is_suspended: bool = False


@dataclass(frozen=True, slots=True)
class MarketState:
    fixture_id: str
    market_type: str
    home_team: str
    away_team: str
    books: tuple[BookLine, ...]
    sport_key: str | None = None
    commence_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class MarketEvaluation:
    labels: tuple[str, ...]
    edges: tuple[EdgeSignal, ...] = ()
    consensus: Mapping[str, Decimal] | None = None
    books_priced: int = 0
    skipped: Mapping[str, int] | None = None  # reason -> count, for diagnostics


def market_id(fixture_id: str, market_type: str) -> UUID:
    return uuid5(ARYABHATA_NAMESPACE, f"{fixture_id}|{market_type}")


def ordered_labels(labels: Sequence[str] | frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(labels, key=lambda label: (LABEL_ORDER.get(label, 99), label)))


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@_exact
def evaluate_market(
    state: MarketState,
    *,
    now: datetime,
    line_max_age: timedelta,
    book_max_age: timedelta,
    preferred: DevigMethod = "shin",
) -> MarketEvaluation:
    """Consensus fair probabilities from every fresh book, then every line worth staking.

    Only pre-match markets are priced: the sanctioned feeds are polled, so an in-play price is
    already stale by the time it arrives.
    """
    now = _aware(now)
    skipped: Counter[str] = Counter()
    if state.commence_time is not None and _aware(state.commence_time) <= now:
        return MarketEvaluation(labels=(), skipped={"started": 1})

    fresh: list[BookLine] = []
    for book in state.books:
        age = now - _aware(book.seen_at)
        if book.is_suspended:
            skipped["suspended"] += 1
        elif age > book_max_age or age < -CLOCK_SKEW:
            skipped["stale"] += 1
        else:
            fresh.append(book)
    shapes = Counter(frozenset(book.prices) for book in fresh if len(book.prices) >= 2)
    if not shapes:
        return MarketEvaluation(labels=(), skipped=dict(skipped))
    labels = ordered_labels(shapes.most_common(1)[0][0])  # 2-way or 3-way: what most books quote

    priced: list[tuple[BookLine, DevigResult]] = []
    for book in fresh:
        if frozenset(book.prices) != frozenset(labels):
            skipped["shape"] += 1
            continue
        try:
            priced.append((book, devig([book.prices[label] for label in labels], preferred)))
        except AryabhataError:
            skipped["invalid"] += 1
    if len(priced) < MIN_BOOKS:
        skipped["too_few_books"] += 1
        return MarketEvaluation(labels=labels, books_priced=len(priced), skipped=dict(skipped))

    fair_books = [dict(zip(labels, result.probabilities, strict=True)) for _, result in priced]
    consensus = consensus_probabilities(fair_books, labels)
    method = Counter(result.method for _, result in priced).most_common(1)[0][0]
    mid = market_id(state.fixture_id, state.market_type)
    expires_at = now + SIGNAL_TTL

    edges: list[EdgeSignal] = []
    for label in labels:
        lines = [(book, result) for book, result in priced if now - _aware(book.seen_at) <= line_max_age]
        if not lines:
            skipped["no_fresh_line"] += 1
            continue
        # Best price; ties go to the sharper book (lower margin), then a stable bookmaker order
        book, result = max(lines, key=lambda pair: (pair[0].prices[label], -pair[1].overround, pair[0].bookmaker_id))
        price = book.prices[label]
        p = consensus[label]
        ev = expected_value(p, price)
        if ev is None or ev < MIN_EV:
            continue
        if ev > MAX_PLAUSIBLE_EV:
            skipped["implausible"] += 1
            continue
        true_prob = p.quantize(_PROB_QUANTUM)
        edges.append(
            EdgeSignal(
                signal_id=uuid5(ARYABHATA_NAMESPACE, f"{mid}|{label}|{book.source}|{book.bookmaker_id}|{price}|{now.isoformat()}"),
                fixture_id=state.fixture_id,
                market_id=mid,
                market_type=state.market_type,
                selection=label,
                home_team=state.home_team,
                away_team=state.away_team,
                sport_key=state.sport_key,
                commence_time=state.commence_time,
                bookmaker_id=book.bookmaker_id,
                source=book.source,
                odds=price,
                true_prob=true_prob,
                ev=ev,
                ev_percent=(ev * _HUNDRED).quantize(_EV_PCT_QUANTUM),
                full_kelly=kelly_fraction(p, price),
                devig_method=method,
                overround=result.overround,
                books=len(priced),
                timestamp=now,
                expires_at=expires_at,
            )
        )
    return MarketEvaluation(
        labels=labels,
        edges=tuple(edges),
        consensus=consensus,
        books_priced=len(priced),
        skipped=dict(skipped),
    )
