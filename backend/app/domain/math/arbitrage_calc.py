"""Commission- and FX-adjusted arbitrage and hedging maths, in ``Decimal`` throughout.

Every price is reduced to what one rupee staked actually returns, in rupees:

* Commission: an exchange takes its rate from net winnings, so a raw price ``O`` at commission
  ``c`` pays ``True_Odds = (O - 1) * (1 - c) + 1``.
* FX: a leg staked in another currency costs ``stake_ccy * rate`` rupees at the live rate, and its
  payout comes home through a haircut ``h`` (the rate can move before settlement), so in rupee
  terms the leg pays ``True_Odds * (1 - h)``. INR legs carry no haircut.

With those rupee odds ``E_i``:

* Arbitrage: a market is one when ``sum(1 / E_i) < 1``; staking ``T * (1 / E_i) / B`` on each
  outcome returns ``T / B`` whichever wins.
* Hedging a book of open bets (``P_k`` = what the book makes if outcome ``k`` wins): keep the
  upside on one outcome (the anchor ``a``) and hedge every other outcome ``k`` in ``S`` so that it
  ends at a target ``T``: ``h_k = (T + H - P_k) / E_k`` with ``H = (T * b - c) / (1 - b)``,
  ``b = sum_S 1 / E_k``, ``c = sum_S P_k / E_k``.

  - Free bet (unbalanced): ``T = 0``, exactly nothing lost if the anchor fails, all profit on it.
  - Balanced (equal profit): the ``T`` at which the anchor ends at ``T`` too:
    ``T = P_a * (1 - b) + c``. For one back bet (stake ``S``, odds ``E_x``) this is the familiar
    ``S * (E_x * (1 - B) - 1)`` with stakes ``S * E_x / E_j``.
  - The slider in between: ``T = fraction * T_balanced``.

  An outcome already above the target needs no stake (a negative ``h_k`` is a lay we don't have),
  so it leaves ``S`` and the system is solved again.

Rounding never works against the guarantee: hedge stakes round *up* to the stake unit after
solving for ``T`` plus the largest rounding error the other legs could add, and every figure
reported (profits per outcome, guaranteed profit) is recomputed from the rounded stakes.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

ZERO, ONE = Decimal(0), Decimal(1)
PAISA = Decimal("0.01")
ODDS_QUANTUM = Decimal("0.0001")
HOME_CURRENCY = "INR"
MAX_COMMISSION = Decimal("0.5")  # nobody legitimately takes half of net winnings or more
_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])


def _exact(func: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with localcontext(_CONTEXT):
            return func(*args, **kwargs)

    return wrapper


class ArbitrageMathError(ValueError):
    """An input that cannot be priced (bad odds, commission, rate) or a hedge that cannot exist."""


def to_decimal(value: object, name: str = "value") -> Decimal:
    if value is None or isinstance(value, bool):
        raise ArbitrageMathError(f"{name} is missing")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArbitrageMathError(f"{name} must be finite")
        value = repr(value)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ArbitrageMathError(f"{name} must be a number") from exc
    if not number.is_finite():
        raise ArbitrageMathError(f"{name} must be finite")
    return number


# ---------------------------------------------------------------- commission + FX
@_exact
def commission_adjusted_odds(raw_odds: object, commission_rate: object = ZERO) -> Decimal:
    """``True_Odds = (Raw_Odds - 1) * (1 - Commission_Rate) + 1``: what one unit returns after the
    exchange takes its cut of the net win."""
    odds = to_decimal(raw_odds, "raw_odds")
    rate = to_decimal(commission_rate, "commission_rate")
    if odds <= ONE:
        raise ArbitrageMathError("decimal odds must be greater than 1")
    if not ZERO <= rate < MAX_COMMISSION:
        raise ArbitrageMathError("commission rate must be in [0, 0.5)")
    return (odds - ONE) * (ONE - rate) + ONE


@_exact
def raw_odds_for(rupee_odds: Decimal, commission_rate: Decimal, haircut: Decimal = ZERO) -> Decimal:
    """The inverse: the raw price a book must offer for a leg to return ``rupee_odds``. Rounded up
    to the odds quantum, so a floor built from it never admits a price that loses."""
    true = rupee_odds / (ONE - haircut)
    if true <= ONE:
        return ONE + ODDS_QUANTUM
    return ((true - ONE) / (ONE - commission_rate) + ONE).quantize(ODDS_QUANTUM, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class FxQuote:
    """``inr_per_unit`` rupees buy one unit of ``currency``; ``haircut`` is the conversion safety margin."""

    currency: str
    inr_per_unit: Decimal
    haircut: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.currency != HOME_CURRENCY:
            if not (self.inr_per_unit.is_finite() and self.inr_per_unit > ZERO):
                raise ArbitrageMathError(f"no usable {self.currency}/INR rate")
            if not ZERO <= self.haircut < ONE:
                raise ArbitrageMathError("FX haircut must be in [0, 1)")


def _foreign(fx: FxQuote | None) -> bool:
    return fx is not None and fx.currency != HOME_CURRENCY


@_exact
def inr_odds(true_odds: Decimal, fx: FxQuote | None) -> Decimal:
    """Rupee return per rupee staked: the haircut applies to a foreign leg's payout coming home."""
    return true_odds * (ONE - fx.haircut) if _foreign(fx) else true_odds  # type: ignore[union-attr]


@_exact
def to_inr(amount: Decimal, fx: FxQuote | None) -> Decimal:
    return amount * fx.inr_per_unit if _foreign(fx) else amount  # type: ignore[union-attr]


@_exact
def leg_cost_inr(stake_ccy: Decimal, fx: FxQuote | None) -> Decimal:
    """What a leg's stake costs in rupees, as the ledger reserves it: rounded up to the paisa, so a
    foreign stake (£0.17 at 117.60 = ₹19.992) is never under-reserved (₹20.00). Payouts are always
    computed from the exact amount, so the rounding only ever costs, never pays."""
    return to_inr(stake_ccy, fx).quantize(PAISA, rounding=ROUND_UP)


@_exact
def stake_unit_inr(fx: FxQuote | None) -> Decimal:
    """The rupee value of one stake unit (one paisa, or one cent of the leg's currency)."""
    return to_inr(PAISA, fx)


@_exact
def from_inr(amount_inr: Decimal, fx: FxQuote | None, *, rounding: str = ROUND_DOWN) -> Decimal:
    """A rupee amount in the leg's own currency, rounded to its stake unit (down unless told otherwise)."""
    amount = amount_inr / fx.inr_per_unit if _foreign(fx) else amount_inr  # type: ignore[union-attr]
    return amount.quantize(PAISA, rounding=rounding)


# ---------------------------------------------------------------- one priced outcome
@dataclass(frozen=True, slots=True)
class Offer:
    """A price for one outcome at one provider."""

    selection: str
    provider: str
    raw_odds: Decimal
    commission: Decimal = ZERO
    fx: FxQuote | None = None

    @property
    def currency(self) -> str:
        return self.fx.currency if self.fx else HOME_CURRENCY

    @property
    def haircut(self) -> Decimal:
        return self.fx.haircut if _foreign(self.fx) else ZERO  # type: ignore[union-attr]

    def true_odds(self) -> Decimal:
        return commission_adjusted_odds(self.raw_odds, self.commission)

    def rupee_odds(self) -> Decimal:
        return inr_odds(self.true_odds(), self.fx)

    def floor_for(self, rupee_odds: Decimal) -> Decimal:
        """The lowest raw price this provider may fill at for the leg to still return ``rupee_odds``."""
        with localcontext(_CONTEXT):
            return raw_odds_for(rupee_odds, self.commission, self.haircut)


def best_offers(offers: Sequence[Offer]) -> dict[str, Offer]:
    """Per outcome, the offer that returns the most rupees (commission and FX applied). Ties go to
    the provider that sorts first, so the choice is stable from one tick to the next."""
    best: dict[str, tuple[Decimal, Offer]] = {}
    for offer in offers:
        try:
            value = offer.rupee_odds()
        except ArbitrageMathError:
            continue
        current = best.get(offer.selection)
        if current is None or value > current[0] or (value == current[0] and offer.provider < current[1].provider):
            best[offer.selection] = (value, offer)
    return {selection: pair[1] for selection, pair in best.items()}


# ---------------------------------------------------------------- arbitrage
@dataclass(frozen=True, slots=True)
class ArbLeg:
    offer: Offer
    rupee_odds: Decimal
    stake_inr: Decimal
    stake_ccy: Decimal  # in the leg's own currency, rounded down to the stake unit
    payout_inr: Decimal  # what this leg returns, in rupees, if its outcome wins (from the exact stake)


@dataclass(frozen=True, slots=True)
class Arbitrage:
    selections: tuple[str, ...]
    legs: tuple[ArbLeg, ...]
    booksum: Decimal  # sum(1 / rupee odds): < 1 is an arbitrage
    total_stake_inr: Decimal
    guaranteed_profit_inr: Decimal  # the worst outcome's profit after rounding every stake down
    roi: Decimal  # guaranteed profit / total stake

    @property
    def margin(self) -> Decimal:
        return ONE - self.booksum


@_exact
def booksum(rupee_odds: Sequence[Decimal]) -> Decimal:
    if len(rupee_odds) < 2 or any(o <= ONE for o in rupee_odds):
        raise ArbitrageMathError("a market needs two or more outcomes priced above 1")
    return sum((ONE / o for o in rupee_odds), ZERO)


@_exact
def is_arbitrage(offers: Sequence[Offer]) -> bool:
    """``Sum(1 / True_Odds) < 1`` over one offer per outcome, every price commission- and FX-adjusted."""
    return booksum([offer.rupee_odds() for offer in offers]) < ONE


@_exact
def stake_arbitrage(chosen: Sequence[Offer], total_stake_inr: object) -> Arbitrage | None:
    """Size an arbitrage over exactly these offers (one per outcome). ``None`` when it isn't one,
    or when rounding each stake down to its unit leaves no guaranteed profit."""
    total = to_decimal(total_stake_inr, "total_stake_inr")
    if total <= ZERO:
        raise ArbitrageMathError("total stake must be positive")
    if len({offer.selection for offer in chosen}) != len(chosen):
        raise ArbitrageMathError("one offer per outcome")
    odds = [offer.rupee_odds() for offer in chosen]
    book = booksum(odds)
    if book >= ONE:
        return None
    legs: list[ArbLeg] = []
    for offer, rupee in zip(chosen, odds, strict=True):
        stake_ccy = from_inr(total * (ONE / rupee) / book, offer.fx)
        payout = (to_inr(stake_ccy, offer.fx) * rupee).quantize(PAISA, rounding=ROUND_DOWN)
        legs.append(ArbLeg(offer, rupee, leg_cost_inr(stake_ccy, offer.fx), stake_ccy, payout))
    staked = sum((leg.stake_inr for leg in legs), ZERO)
    if staked <= ZERO or any(leg.stake_ccy <= ZERO for leg in legs):
        return None
    guaranteed = min(leg.payout_inr for leg in legs) - staked
    if guaranteed <= ZERO:
        return None  # rounding (or a tiny bank) ate the margin
    return Arbitrage(
        tuple(offer.selection for offer in chosen),
        tuple(legs),
        book,
        staked,
        guaranteed,
        (guaranteed / staked).quantize(ODDS_QUANTUM, rounding=ROUND_DOWN),
    )


@_exact
def find_arbitrage(offers: Sequence[Offer], outcomes: Sequence[str], total_stake_inr: object) -> Arbitrage | None:
    """The best commission/FX-adjusted price for every outcome; an arbitrage only if all of
    ``outcomes`` are covered and ``sum(1 / E) < 1``."""
    if len(outcomes) < 2 or len(set(outcomes)) != len(outcomes):
        return None
    best = best_offers(offers)
    if set(outcomes) - set(best):
        return None  # an uncovered outcome loses every leg: never an arbitrage
    return stake_arbitrage([best[o] for o in outcomes], total_stake_inr)


@_exact
def rescale_after_fill(planned: Mapping[str, Decimal], filled_selection: str, filled_stake: object) -> dict[str, Decimal]:
    """Partial fill on one leg: scale every other leg by ``filled / planned`` (rounded down), so
    each outcome still pays the same. Returns the new stakes for the legs not yet fired."""
    fill = to_decimal(filled_stake, "filled_stake")
    plan = planned.get(filled_selection)
    if plan is None or plan <= ZERO:
        raise ArbitrageMathError("the filled leg is not in the plan")
    if not ZERO <= fill <= plan:
        raise ArbitrageMathError("a fill cannot exceed the stake asked for")
    ratio = fill / plan
    return {sel: (stake * ratio).quantize(PAISA, rounding=ROUND_DOWN) for sel, stake in planned.items() if sel != filled_selection}


@_exact
def break_even_odds(total_staked_inr: Decimal, leg_stake_inr: Decimal) -> Decimal:
    """Rupee odds at which this leg's outcome returns exactly everything staked across the legs."""
    if leg_stake_inr <= ZERO:
        raise ArbitrageMathError("the leg needs a positive stake")
    return total_staked_inr / leg_stake_inr


# ---------------------------------------------------------------- the book of open bets
@dataclass(frozen=True, slots=True)
class HeldBet:
    selection: str
    stake_inr: Decimal
    rupee_odds: Decimal  # the bet's own odds after its book's commission (and FX haircut)


@_exact
def book_profits(bets: Sequence[HeldBet], outcomes: Sequence[str]) -> dict[str, Decimal]:
    """What the book makes (rupees) if each outcome wins. Every bet must be on a listed outcome:
    an unlisted one means the outcome list is incomplete, and a guarantee built on it is false."""
    listed = set(outcomes)
    stray = sorted({b.selection for b in bets} - listed)
    if stray:
        raise ArbitrageMathError(f"bets on outcomes the market does not list: {', '.join(stray)}")
    staked = sum((b.stake_inr for b in bets), ZERO)
    return {o: sum((b.stake_inr * b.rupee_odds for b in bets if b.selection == o), ZERO) - staked for o in outcomes}


# ---------------------------------------------------------------- hedging
@dataclass(frozen=True, slots=True)
class HedgeLeg:
    offer: Offer
    rupee_odds: Decimal
    stake_ccy: Decimal  # in the leg's own currency, rounded up to its unit
    stake_inr: Decimal  # what it costs, rounded up to the paisa (leg_cost_inr)


@dataclass(frozen=True, slots=True)
class HedgePlan:
    kind: str  # free_bet | balanced | partial
    fraction: Decimal  # 0 = free bet ... 1 = balanced
    anchor: str  # the outcome that keeps the upside
    target: Decimal  # the profit every hedged outcome was solved for
    legs: tuple[HedgeLeg, ...]
    hedge_stake_inr: Decimal
    before: dict[str, Decimal]  # outcome -> profit now
    after: dict[str, Decimal]  # outcome -> profit with this hedge, from the rounded stakes

    @property
    def worst_before(self) -> Decimal:
        return min(self.before.values())

    @property
    def worst_after(self) -> Decimal:
        return min(self.after.values())

    @property
    def best_after(self) -> Decimal:
        return max(self.after.values())

    @property
    def locks_profit(self) -> bool:
        return self.worst_after > ZERO


@dataclass(frozen=True, slots=True)
class _Solution:
    stakes: dict[str, Decimal]  # ideal (unrounded) rupee stakes per hedged outcome
    total: Decimal


@_exact
def _solve(profits: Mapping[str, Decimal], odds: Mapping[str, Decimal], anchor: str, target: Decimal | None) -> tuple[_Solution, Decimal]:
    """Stakes on the outcomes below the target. ``target=None`` solves for the balanced target."""
    hedged = {o for o in odds if o != anchor}
    for _ in range(len(profits) + 1):
        if not hedged:
            return _Solution({}, ZERO), (profits[anchor] if target is None else target)
        b = sum((ONE / odds[o] for o in hedged), ZERO)
        if b >= ONE:
            raise ArbitrageMathError("the outcomes to hedge are overpriced together: no stake split covers them")
        c = sum((profits[o] / odds[o] for o in hedged), ZERO)
        goal = profits[anchor] * (ONE - b) + c if target is None else target
        total = (goal * b - c) / (ONE - b)
        stakes = {o: (goal + total - profits[o]) / odds[o] for o in hedged}
        above = {o for o, stake in stakes.items() if stake < ZERO}  # already past the goal: no stake there
        if not above:
            return _Solution(stakes, total), goal
        hedged -= above
    raise ArbitrageMathError("no stable hedge")  # unreachable: each pass removes an outcome


def _rounding_slack(offer: Offer) -> Decimal:
    """The most rounding one leg can cost the other outcomes: its stake unit (rounded up), and for a
    foreign leg one more paisa (its rupee cost is rounded up too)."""
    return stake_unit_inr(offer.fx) + (PAISA if _foreign(offer.fx) else ZERO)


@_exact
def hedge_book(
    profits: Mapping[str, Decimal],
    offers: Mapping[str, Offer],
    *,
    anchor: str | None = None,
    fraction: object = ONE,
) -> HedgePlan:
    """Hedge a book (``profits``: outcome -> what it makes now) with ``offers`` (the live price to
    back each outcome). ``fraction`` 0 is the free bet, 1 the balanced hedge, anything between the
    slider. ``anchor`` keeps the upside (default: the outcome the book makes most on).

    Raises when an outcome that needs covering has no live price, or the target is out of reach."""
    t = to_decimal(fraction, "fraction")
    if not ZERO <= t <= ONE:
        raise ArbitrageMathError("fraction must be between 0 (free bet) and 1 (balanced)")
    if len(profits) < 2:
        raise ArbitrageMathError("a market needs two or more outcomes")
    if anchor is None:
        anchor = max(sorted(profits), key=lambda o: profits[o])
    if anchor not in profits:
        raise ArbitrageMathError("the anchor must be one of the market's outcomes")
    priced = {o: offer for o, offer in offers.items() if o in profits and o != anchor}
    odds = {o: offer.rupee_odds() for o, offer in priced.items()}

    _, balanced = _solve(profits, odds, anchor, None)
    target = balanced * t
    # Solve for the target plus the most the rounded-up stakes could cost the other outcomes
    slack = sum((_rounding_slack(offer) for offer in priced.values()), ZERO)
    solution, _ = _solve(profits, odds, anchor, target + slack if t < ONE else None)

    def assemble(stakes: Mapping[str, Decimal]) -> tuple[list[HedgeLeg], Decimal, dict[str, Decimal]]:
        legs: list[HedgeLeg] = []
        for o in sorted(stakes, key=lambda o: (o != anchor, o)):
            stake_ccy = from_inr(stakes[o], priced[o].fx, rounding=ROUND_UP)
            if stake_ccy > ZERO:
                legs.append(HedgeLeg(priced[o], odds[o], stake_ccy, leg_cost_inr(stake_ccy, priced[o].fx)))
        spent = sum((leg.stake_inr for leg in legs), ZERO)
        payout = {leg.offer.selection: to_inr(leg.stake_ccy, leg.offer.fx) * leg.rupee_odds for leg in legs}
        return legs, spent, {o: (profits[o] + payout.get(o, ZERO) - spent).quantize(PAISA, rounding=ROUND_DOWN) for o in profits}

    legs, spent, after = assemble(solution.stakes)
    # Dust: a leg worth less than one stake unit only exists because of rounding, and rounding it up
    # to a whole unit (a euro cent is a rupee) costs more than it covers. Drop it, unless the target
    # needs it (a free bet's ₹0) or dropping it would lower the worst case.
    dust = {o for o, stake in solution.stakes.items() if stake < stake_unit_inr(priced[o].fx)}
    if dust:
        lean = assemble({o: stake for o, stake in solution.stakes.items() if o not in dust})
        holds = all(lean[2][o] >= target.quantize(PAISA, rounding=ROUND_DOWN) for o in profits if o != anchor) if t < ONE else True
        if holds and min(lean[2].values()) >= min(after.values()):
            legs, spent, after = lean

    unhedgeable = [o for o in profits if o != anchor and o not in priced and after[o] < target]
    if unhedgeable:
        raise ArbitrageMathError(f"no live price to hedge {', '.join(sorted(unhedgeable))}")
    if t < ONE and any(after[o] < target.quantize(PAISA, rounding=ROUND_DOWN) for o in profits if o != anchor):
        raise ArbitrageMathError("this hedge cannot reach its target")  # defensive: the slack covers rounding
    kind = "free_bet" if t == ZERO else "balanced" if t == ONE else "partial"
    return HedgePlan(kind, t, anchor, target, tuple(legs), spent, {o: v.quantize(PAISA, rounding=ROUND_DOWN) for o, v in profits.items()}, after)


@_exact
def hedge_after_fills(
    profits: Mapping[str, Decimal],
    offers: Mapping[str, Offer],
    *,
    anchor: str,
    target: Decimal,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, Decimal]:
    """Re-solve the legs not yet fired after earlier legs filled, some maybe partially: each stake in
    the leg's own currency, rounded up to its unit (``leg_cost_inr`` gives the rupees). ``profits``
    already includes the filled legs; ``exclude`` holds the outcomes already traded."""
    priced = {o: offer for o, offer in offers.items() if o in profits and o != anchor and o not in exclude}
    odds = {o: offer.rupee_odds() for o, offer in priced.items()}
    slack = sum((_rounding_slack(offer) for offer in priced.values()), ZERO)
    solution, _ = _solve({o: p for o, p in profits.items() if o not in exclude or o == anchor}, odds, anchor, target + slack)
    return {o: from_inr(stake, priced[o].fx, rounding=ROUND_UP) for o, stake in solution.stakes.items() if stake > ZERO}
