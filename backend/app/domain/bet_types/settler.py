from math import prod

from app.domain.bet_types.calculator import money, safe_odds, structure_combinations
from app.domain.risk.common import safe_float
from app.schemas.bet_structures import AnyBetStructure, EachWayBet, LayBet, ParlayBet, SingleBet
from app.schemas.bet_types import BaseLeg, LegSettlementContext, LegStatus, SettlementResult

_EPS = 1e-6

_LAY_STATUS_MAP: dict[LegStatus, LegStatus] = {
    LegStatus.WON: LegStatus.LOST,
    LegStatus.LOST: LegStatus.WON,
    LegStatus.HALF_WON: LegStatus.HALF_LOST,
    LegStatus.HALF_LOST: LegStatus.HALF_WON,
    LegStatus.VOID: LegStatus.VOID,
}


class SettlementError(ValueError):
    """Raised when settlement input is incomplete or invalid."""


def _pending() -> SettlementResult:
    return SettlementResult(status=LegStatus.PENDING, payout=0.0, profit=0.0)


def _get_context(leg: BaseLeg, contexts: dict[str, LegSettlementContext]) -> LegSettlementContext:
    ctx = contexts.get(leg.leg_id)
    if ctx is None:
        raise SettlementError(f"Missing settlement context for leg '{leg.leg_id}'")
    return ctx


def effective_win_odds(odds: float, ctx: LegSettlementContext) -> float:
    """Rule 4 deducts from winnings only; dead heat divides the whole stake."""
    o = safe_odds(odds)
    dh = ctx.dead_heat_denominator if ctx.dead_heat_denominator >= 1 else 1
    r4 = min(max(safe_float(ctx.rule4_deduction_pct), 0.0), 1.0)
    return safe_float((1.0 + (o - 1.0) * (1.0 - r4)) / dh)


def leg_multiplier(odds: float, ctx: LegSettlementContext) -> float:
    """Return multiplier applied to the stake carried into this leg."""
    match ctx.status:
        case LegStatus.WON:
            return effective_win_odds(odds, ctx)
        case LegStatus.HALF_WON:
            return (effective_win_odds(odds, ctx) + 1.0) / 2.0
        case LegStatus.HALF_LOST:
            return 0.5
        case LegStatus.VOID:
            return 1.0
        case LegStatus.LOST:
            return 0.0
    raise SettlementError(f"Cannot compute multiplier for status {ctx.status}")


def _derive_status(payout: float, cost: float) -> LegStatus:
    if payout <= _EPS:
        return LegStatus.LOST
    diff = payout - cost
    if abs(diff) <= _EPS:
        return LegStatus.VOID
    return LegStatus.WON if diff > 0 else LegStatus.HALF_LOST


def _settle_single(structure: SingleBet, contexts: dict[str, LegSettlementContext]) -> SettlementResult:
    ctx = _get_context(structure.leg, contexts)
    if ctx.status == LegStatus.PENDING:
        return _pending()
    stake = safe_float(structure.unit_stake)
    payout = stake * leg_multiplier(structure.leg.odds, ctx)
    return SettlementResult(status=ctx.status, payout=money(payout), profit=money(payout - stake))


def _settle_combination(structure: AnyBetStructure, contexts: dict[str, LegSettlementContext]) -> SettlementResult:
    legs: list[BaseLeg] = list(structure.legs)  # type: ignore[union-attr]
    leg_ctx = {leg.leg_id: _get_context(leg, contexts) for leg in legs}
    combos = structure_combinations(structure)
    stake = safe_float(structure.unit_stake)
    total_cost = stake * len(combos)
    statuses = [c.status for c in leg_ctx.values()]

    # A parlay with any lost leg is dead regardless of pending legs.
    if isinstance(structure, ParlayBet) and LegStatus.LOST in statuses:
        return SettlementResult(status=LegStatus.LOST, payout=0.0, profit=money(-total_cost))
    if LegStatus.PENDING in statuses:
        return _pending()

    multipliers = {leg.leg_id: leg_multiplier(leg.odds, leg_ctx[leg.leg_id]) for leg in legs}

    payout = 0.0
    for combo in combos:
        active = [leg for leg in combo if leg_ctx[leg.leg_id].status != LegStatus.VOID]
        if not active:
            payout += stake  # fully voided line refunds its unit stake
            continue
        payout += stake * prod(multipliers[leg.leg_id] for leg in active)

    if all(s == LegStatus.VOID for s in statuses):
        status = LegStatus.VOID
    else:
        status = _derive_status(payout, total_cost)
    return SettlementResult(status=status, payout=money(payout), profit=money(payout - total_cost))


def _settle_each_way(structure: EachWayBet, contexts: dict[str, LegSettlementContext]) -> SettlementResult:
    leg = structure.leg
    ctx = _get_context(leg, contexts)
    stake = safe_float(structure.unit_stake)
    total_cost = stake * 2

    if ctx.status == LegStatus.PENDING:
        return _pending()
    if ctx.status in (LegStatus.HALF_WON, LegStatus.HALF_LOST):
        raise SettlementError("Half results are not valid for each-way bets")
    if ctx.status == LegStatus.VOID:
        return SettlementResult(status=LegStatus.VOID, payout=money(total_cost), profit=0.0)

    won = ctx.status == LegStatus.WON
    placed = won or (ctx.finishing_position is not None and ctx.finishing_position <= leg.place_terms)
    place_odds = 1.0 + (safe_odds(leg.odds) - 1.0) * safe_float(leg.place_fraction)

    win_payout = stake * effective_win_odds(leg.odds, ctx) if won else 0.0
    place_payout = stake * effective_win_odds(place_odds, ctx) if placed else 0.0
    payout = win_payout + place_payout

    status = LegStatus.WON if won else _derive_status(payout, total_cost)
    return SettlementResult(status=status, payout=money(payout), profit=money(payout - total_cost))


def _settle_lay(structure: LayBet, contexts: dict[str, LegSettlementContext]) -> SettlementResult:
    ctx = _get_context(structure.leg, contexts)
    if ctx.status == LegStatus.PENDING:
        return _pending()

    stake = safe_float(structure.unit_stake)
    liability = stake * (safe_odds(structure.leg.odds) - 1.0)
    backer_odds = effective_win_odds(structure.leg.odds, ctx)

    backer_profit = {
        LegStatus.WON: stake * (backer_odds - 1.0),
        LegStatus.HALF_WON: stake * (backer_odds - 1.0) / 2.0,
        LegStatus.LOST: -stake,
        LegStatus.HALF_LOST: -stake / 2.0,
        LegStatus.VOID: 0.0,
    }[ctx.status]

    layer_profit = -backer_profit
    payout = max(0.0, liability + layer_profit)  # liability is locked up front
    return SettlementResult(
        status=_LAY_STATUS_MAP[ctx.status],
        payout=money(payout),
        profit=money(layer_profit),
    )


def settle_bet(structure: AnyBetStructure, leg_contexts: dict[str, LegSettlementContext]) -> SettlementResult:
    if isinstance(structure, LayBet):
        return _settle_lay(structure, leg_contexts)
    if isinstance(structure, EachWayBet):
        return _settle_each_way(structure, leg_contexts)
    if isinstance(structure, SingleBet):
        return _settle_single(structure, leg_contexts)
    return _settle_combination(structure, leg_contexts)
