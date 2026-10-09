"""The live cashout and hedge advisor for a running multiple (Group 69).

A placed slip with some legs already won and the rest still to play is worth, right now:

    fair value = stake x (settled legs' payout factors) x (open legs' prices) x P(every open leg wins)

with P from the current market (de-vigged) or the scoreline models. Against that:

* the bookmaker's cashout offer. Books build a margin into it; a margin beyond the usual one is a
  penalty. Under ``hold_ratio`` (85%) of fair value the offer is penalised: HOLD.
* a hedge, when exactly one leg is left: back every other outcome of it (at Pinnacle or the best book)
  or lay it on Betfair, so the slip pays the same whatever happens. The exact stakes are computed; the
  locked profit is what it guarantees after the original stake.
* variance. To a bankroll, a sure amount is worth more than the same amount on average: the
  certainty equivalent of holding, under log utility, is ``exp(E[log(bankroll + payoff)]) - bankroll``.
  An offer at or above it beats holding for the bankroll's long-run growth: CASH OUT.

Decision, in order: an offer at or above fair value is free money (CASH OUT); a hedge that locks more
than the offer pays (HEDGE LEG); an offer under the hold line is penalised (HOLD); an offer at or above
the certainty equivalent buys real variance protection (CASH OUT); otherwise the slip is worth more
held (HOLD).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.domain.oracle.markets import LegResult, payout_factor

PAISA = Decimal("0.01")


class Advice(StrEnum):
    HOLD = "HOLD"
    CASH_OUT = "CASH_OUT"
    HEDGE_LEG = "HEDGE_LEG"


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class OpenLeg:
    label: str
    odds: float  # the price this leg carries in the slip
    probability: float  # its chance of winning now
    hedge_back: Sequence[tuple[str, float]] = ()  # every other outcome: (outcome, best back price), to dutch
    hedge_book: str | None = None  # where those backs are priced
    lay_odds: float | None = None  # Betfair lay price of this leg, if known
    lay_commission: float = 0.05


@dataclass(frozen=True, slots=True)
class SettledLeg:
    label: str
    odds: float
    result: LegResult


@dataclass(frozen=True, slots=True)
class HedgePlan:
    kind: str  # BACK_OTHERS | LAY
    book: str
    stakes: tuple[tuple[str, Decimal], ...]  # (outcome or "LAY <leg>", stake)
    total_outlay: Decimal  # money put up now (a lay: the liability)
    locked_profit: Decimal  # guaranteed, after the original stake
    instruction: str


@dataclass(frozen=True, slots=True)
class CashoutAdvice:
    advice: Advice
    fair_value: Decimal
    potential_payout: Decimal
    win_probability: float
    offer: Decimal | None
    offer_ratio: float | None  # offer / fair value
    implied_margin: float | None  # 1 - offer_ratio: the book's cut of the slip's value
    certainty_equivalent: Decimal | None
    hedge: HedgePlan | None
    reasons: tuple[str, ...] = field(default=())


def _hedge_back(payout: float, stake: float, leg: OpenLeg) -> HedgePlan | None:
    """Dutch every other outcome so each pays ``payout``: stakes payout / price, total payout x sum(1/price)."""
    prices = [(name, price) for name, price in leg.hedge_back if price > 1.0]
    if not prices:
        return None
    stakes = [(name, payout / price) for name, price in prices]
    outlay = sum(s for _, s in stakes)
    locked = payout - outlay - stake
    book = leg.hedge_book or "the best book"
    lines = ", ".join(f"₹{_money(s):,} on {name} @ {price:g}" for (name, s), (_, price) in zip(stakes, prices, strict=True))
    return HedgePlan(
        "BACK_OTHERS", book, tuple((name, _money(s)) for name, s in stakes), _money(outlay), _money(locked),
        f"Back {lines} at {book}: every result then returns at least ₹{_money(payout):,}",
    )


def _hedge_lay(payout: float, stake: float, leg: OpenLeg) -> HedgePlan | None:
    """Lay the leg for payout / (lay - commission): win or lose, the same money comes back."""
    if leg.lay_odds is None or leg.lay_odds <= 1.0:
        return None
    c = leg.lay_commission
    lay_stake = payout / (leg.lay_odds - c)
    liability = lay_stake * (leg.lay_odds - 1.0)
    locked = lay_stake * (1.0 - c) - stake
    return HedgePlan(
        "LAY", "Betfair", ((f"LAY {leg.label}", _money(lay_stake)),), _money(liability), _money(locked),
        f"Lay {leg.label} on Betfair: ₹{_money(lay_stake):,} @ {leg.lay_odds:g} (liability ₹{_money(liability):,})",
    )


def certainty_equivalent(bankroll: float, payout: float, probability: float) -> float:
    """The sure amount worth as much as holding (log utility; the stake is already spent)."""
    if bankroll <= 0:
        return payout * probability
    expected_log = probability * math.log(bankroll + payout) + (1 - probability) * math.log(bankroll)
    return math.exp(expected_log) - bankroll


def advise(
    stake: float,
    settled: Sequence[SettledLeg],
    open_legs: Sequence[OpenLeg],
    *,
    offer: float | None = None,
    bankroll: float | None = None,
    hold_ratio: float = 0.85,
    total_odds: float | None = None,
) -> CashoutAdvice:
    """``total_odds``: the slip's placed odds, when the book rounded them (scales every payout)."""
    if stake <= 0:
        raise ValueError("stake must be positive")
    if not open_legs:
        raise ValueError("nothing left to play: the slip settles, it does not cash out")
    if any(leg.result is LegResult.LOST for leg in settled):
        raise ValueError("a leg has lost: the slip is settled at nothing")
    settled_factor = math.prod(payout_factor(leg.result, leg.odds) for leg in settled)
    open_odds = math.prod(leg.odds for leg in open_legs)
    rounding = 1.0
    if total_odds is not None:
        nominal = math.prod(leg.odds for leg in settled) * open_odds
        rounding = total_odds / nominal if nominal > 0 else 1.0
    payout = stake * settled_factor * open_odds * rounding
    p_all = math.prod(min(max(leg.probability, 0.0), 1.0) for leg in open_legs)
    fair = payout * p_all
    reasons: list[str] = [f"fair value {_money(fair)}: {p_all:.1%} chance the open leg(s) win the full {_money(payout)}"]

    hedge = None
    if len(open_legs) == 1:
        options = [h for h in (_hedge_back(payout, stake, open_legs[0]), _hedge_lay(payout, stake, open_legs[0])) if h is not None]
        hedge = max(options, key=lambda h: h.locked_profit, default=None)
    ce = certainty_equivalent(bankroll, payout, p_all) if bankroll is not None else None
    ratio = (offer / fair) if offer is not None and fair > 0 else None

    if offer is not None and fair > 0 and offer >= fair - 0.005:  # at fair value to the paisa
        advice = Advice.CASH_OUT
        reasons.append(f"the offer {_money(offer)} is at or above fair value: the bookmaker is paying more than the slip is worth")
    elif hedge is not None and float(hedge.locked_profit) > (offer - stake if offer is not None else 0.0) and hedge.locked_profit > 0:
        advice = Advice.HEDGE_LEG
        reasons.append(f"a hedge locks {_money(hedge.locked_profit)} profit whatever happens" + (f", more than the offer's {_money(offer - stake)}" if offer is not None else ""))
    elif ratio is not None and ratio < hold_ratio:
        advice = Advice.HOLD
        reasons.append(f"the offer is {ratio:.1%} of fair value: a {1 - ratio:.1%} cut, beyond the {1 - hold_ratio:.0%} line. The cashout is penalised")
    elif offer is not None and ce is not None and offer >= ce:
        advice = Advice.CASH_OUT
        reasons.append(f"the offer {_money(offer)} beats holding's certainty equivalent {_money(ce)} for this bankroll: worth taking for the variance it removes")
    else:
        advice = Advice.HOLD
        reasons.append("holding is worth more than the offer" if offer is not None else "no cashout offer: hold, or hedge if a lock appears")
    return CashoutAdvice(
        advice, _money(fair), _money(payout), p_all, None if offer is None else _money(offer), ratio,
        None if ratio is None else 1 - ratio, None if ce is None else _money(ce), hedge, tuple(reasons),
    )
