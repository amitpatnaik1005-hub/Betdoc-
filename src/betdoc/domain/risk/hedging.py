from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from typing import Literal, Optional

from betdoc.domain.math.errors import DomainMathError
from betdoc.domain.math.money import MAX_PAISE, from_paise, to_paise


def _decimal(value: Decimal, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainMathError(
            "Expected a finite Decimal", field=label, value=value
        )
    return value


def _money(value: Decimal, label: str) -> int:
    _decimal(value, label)
    try:
        paise = to_paise(value, label=label)
    except (ValueError, ArithmeticError) as exc:
        raise DomainMathError(
            "Invalid monetary amount", field=label, value=value
        ) from exc
    with localcontext() as context:
        context.prec = 50
        if from_paise(paise) != value:
            raise DomainMathError(
                "Money must have whole-paise precision", field=label, value=value
            )
    return paise


def _inr(paise: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        amount = from_paise(abs(paise))
        return -amount if paise < 0 else amount


def _odds(value: Decimal, label: str) -> Decimal:
    _decimal(value, label)
    if not Decimal("1") < value <= Decimal("1000000"):
        raise DomainMathError(
            "Decimal odds must be in (1, 1000000]",
            field=label,
            value=value,
        )
    return value


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_DOWN))


@dataclass(frozen=True)
class Position:
    position_id: str
    selection: str
    original_stake: Decimal
    locked_odds: Decimal
    potential_payout: Decimal


@dataclass(frozen=True)
class HedgeEvaluation:
    should_hedge: bool
    hedge_selection: str
    hedge_stake: Decimal
    guaranteed_net_profit: Decimal
    roi_percentage: float
    strategy: Literal["LOCK_PROFIT", "STOP_LOSS", "HOLD"]
    fair_cashout_value: Decimal
    draw_hedge_stake: Decimal = Decimal("0.00")
    total_hedge_stake: Decimal = Decimal("0.00")
    original_win_net_profit: Decimal = Decimal("0.00")
    opposing_win_net_profit: Decimal = Decimal("0.00")
    draw_net_profit: Optional[Decimal] = None
    cashout_offer_difference: Optional[Decimal] = None
    probability_source: Literal[
        "MODEL", "COMPLEMENT_PRICE_PROXY"
    ] = "COMPLEMENT_PRICE_PROXY"


class DynamicHedgingEngine:
    """Evaluate equal-payout hedges across exhaustive settlement outcomes.

    Without draw odds, the opposing selection MUST be the complete complement
    of the original selection. In a three-way market, supply both opposing
    team and draw odds. Guarantees assume executed prices, sufficient liquidity,
    identical settlement rules, and no fees, taxes, cancellations or defaults.

    A partial hedge scales the equal-payout hedge by hedge_fraction. Before
    paise rounding, outcome deviations shrink by (1 - hedge_fraction), and
    variance shrinks by its square. The fraction is a caller-selected risk
    constraint, not an inferred universally optimal risk preference.

    Without a supplied model probability, cashout uses a vig-sensitive price
    proxy. It is not an independently established fair valuation.
    """

    def evaluate_live_position(
        self,
        position: Position,
        live_opposing_odds: Decimal,
        live_draw_odds: Optional[Decimal] = None,
        min_profit_threshold: Decimal = Decimal("0.00"),
        *,
        live_win_probability: Optional[Decimal] = None,
        bookmaker_cashout_offer: Optional[Decimal] = None,
        hedge_fraction: Decimal = Decimal("1"),
        opposing_selection: str = "COMPLEMENT",
        draw_selection: str = "DRAW",
    ) -> HedgeEvaluation:
        if not position.position_id.strip() or not position.selection.strip():
            raise DomainMathError("Position ID and selection must be non-empty")
        if not opposing_selection.strip() or not draw_selection.strip():
            raise DomainMathError("Hedge selections must be non-empty")

        stake = _money(position.original_stake, "original_stake")
        payout = _money(position.potential_payout, "potential_payout")
        threshold = _money(min_profit_threshold, "min_profit_threshold")
        original_odds = _odds(position.locked_odds, "locked_odds")
        opposing_odds = _odds(live_opposing_odds, "live_opposing_odds")
        draw_odds = (
            None
            if live_draw_odds is None
            else _odds(live_draw_odds, "live_draw_odds")
        )
        fraction = _decimal(hedge_fraction, "hedge_fraction")
        if not Decimal("0") <= fraction <= Decimal("1"):
            raise DomainMathError("Hedge fraction must be in [0, 1]")
        if stake <= 0 or payout <= 0:
            raise DomainMathError("Stake and potential payout must be positive")

        offer = (
            None
            if bookmaker_cashout_offer is None
            else _money(bookmaker_cashout_offer, "bookmaker_cashout_offer")
        )

        with localcontext() as context:
            context.prec = 50

            expected_payout = _floor(Decimal(stake) * original_odds)
            if expected_payout != payout:
                raise DomainMathError(
                    "Potential payout does not match stake and locked odds",
                    supplied_payout_paise=payout,
                    expected_payout_paise=expected_payout,
                )

            source: Literal["MODEL", "COMPLEMENT_PRICE_PROXY"]
            if live_win_probability is not None:
                probability = _decimal(
                    live_win_probability, "live_win_probability"
                )
                if not Decimal("0") <= probability <= Decimal("1"):
                    raise DomainMathError("Live win probability must be in [0, 1]")
                source = "MODEL"
            else:
                opposing_mass = Decimal("1") / opposing_odds
                if draw_odds is not None:
                    opposing_mass += Decimal("1") / draw_odds
                probability = Decimal("1") - opposing_mass
                if probability < 0:
                    raise DomainMathError(
                        "Complement prices cannot establish a win probability; "
                        "supply a model probability",
                        opposing_implied_mass=str(opposing_mass),
                    )
                source = "COMPLEMENT_PRICE_PROXY"

            fair_cashout_paise = _floor(Decimal(payout) * probability)
            offer_difference = (
                None
                if offer is None
                else _inr(offer - fair_cashout_paise)
            )

            # Reserves round UP; settlement payouts round DOWN.
            opposing_stake = int(
                (
                    Decimal(payout) * fraction / opposing_odds
                ).to_integral_value(rounding=ROUND_UP)
            )
            draw_stake = (
                0
                if draw_odds is None
                else int(
                    (
                        Decimal(payout) * fraction / draw_odds
                    ).to_integral_value(rounding=ROUND_UP)
                )
            )
            total_hedge = opposing_stake + draw_stake
            total_invested = stake + total_hedge

            if total_invested > MAX_PAISE:
                raise DomainMathError(
                    "Hedged exposure exceeds the monetary ceiling",
                    total_invested_paise=total_invested,
                )

            original_net = payout - total_invested
            opposing_net = (
                _floor(Decimal(opposing_stake) * opposing_odds)
                - total_invested
            )
            draw_net = (
                None
                if draw_odds is None
                else _floor(Decimal(draw_stake) * draw_odds) - total_invested
            )
            outcomes = [original_net, opposing_net]
            if draw_net is not None:
                outcomes.append(draw_net)
            worst_net = min(outcomes)

            strategy: Literal["LOCK_PROFIT", "STOP_LOSS", "HOLD"] = "HOLD"
            if total_hedge > 0 and worst_net > 0 and worst_net >= threshold:
                strategy = "LOCK_PROFIT"
            elif (
                total_hedge > 0
                and probability < Decimal("0.15")
                and Decimal(stake + worst_net) >= Decimal(stake) * Decimal("0.40")
            ):
                strategy = "STOP_LOSS"

            # HOLD reports the actual unhedged position, not an unexecuted plan.
            if strategy == "HOLD":
                return HedgeEvaluation(
                    should_hedge=False,
                    hedge_selection=opposing_selection,
                    hedge_stake=_inr(0),
                    guaranteed_net_profit=_inr(-stake),
                    roi_percentage=-100.0,
                    strategy="HOLD",
                    fair_cashout_value=_inr(fair_cashout_paise),
                    original_win_net_profit=_inr(payout - stake),
                    opposing_win_net_profit=_inr(-stake),
                    draw_net_profit=None if draw_odds is None else _inr(-stake),
                    cashout_offer_difference=offer_difference,
                    probability_source=source,
                )

            return HedgeEvaluation(
                should_hedge=True,
                hedge_selection=(
                    opposing_selection
                    if draw_odds is None
                    else f"{opposing_selection} + {draw_selection}"
                ),
                hedge_stake=_inr(opposing_stake),
                guaranteed_net_profit=_inr(worst_net),
                roi_percentage=float(
                    Decimal(worst_net) * Decimal("100") / Decimal(total_invested)
                ),
                strategy=strategy,
                fair_cashout_value=_inr(fair_cashout_paise),
                draw_hedge_stake=_inr(draw_stake),
                total_hedge_stake=_inr(total_hedge),
                original_win_net_profit=_inr(original_net),
                opposing_win_net_profit=_inr(opposing_net),
                draw_net_profit=None if draw_net is None else _inr(draw_net),
                cashout_offer_difference=offer_difference,
                probability_source=source,
            )
