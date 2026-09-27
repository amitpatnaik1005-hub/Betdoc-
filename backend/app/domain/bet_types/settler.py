from decimal import Decimal

from app.domain.bet_types.calculator import (
    ONE,
    ZERO,
    dprod,
    lay_liability,
    legs_of,
    line_stake,
    money,
    safe_odds,
    structure_combinations,
    to_dec,
)
from app.schemas.bet_types import (
    AnyBetStructure,
    BaseLeg,
    EachWayBet,
    EachWayLeg,
    LayBet,
    LegSettlementContext,
    LegStatus,
    ParlayBet,
    SettleRequest,
    SettlementResult,
    SingleBet,
)

TWO = Decimal(2)
_EPS = Decimal("0.000001")

_LAY_STATUS_MAP: dict[LegStatus, LegStatus] = {
    LegStatus.WON: LegStatus.LOST,
    LegStatus.LOST: LegStatus.WON,
    LegStatus.HALF_WON: LegStatus.HALF_LOST,
    LegStatus.HALF_LOST: LegStatus.HALF_WON,
    LegStatus.VOID: LegStatus.VOID,
}


class SettlementError(ValueError):
    """Incomplete or invalid settlement input."""


# ---------- Leg math ----------

def resolved_status(context: LegSettlementContext) -> LegStatus:
    """PLAYER SCRATCH RULE: overrides any reported outcome, including PENDING."""
    return LegStatus.VOID if context.player_did_not_participate else context.status


def _rule4(context: LegSettlementContext) -> Decimal:
    r4 = to_dec(context.rule4_deduction_pct)
    return min(max(r4, ZERO), ONE)


def _divisor(context: LegSettlementContext) -> Decimal:
    return Decimal(max(1, int(context.dead_heat_divisor)))


def effective_win_odds(odds: Decimal, context: LegSettlementContext) -> Decimal:
    """Rule 4 on profit first, then dead heat divides the whole stake."""
    if context.player_did_not_participate:
        return ONE
    profit = (safe_odds(odds) - ONE) * (ONE - _rule4(context))
    return (ONE + profit) / _divisor(context)


def effective_place_odds(odds: Decimal, num: int, den: int, context: LegSettlementContext) -> Decimal:
    """RULE 4 PLACE CASCADE: deducted win profit x (num/den), then dead heat."""
    if context.player_did_not_participate:
        return ONE
    if den <= 0 or num < 0:
        raise SettlementError("Invalid place terms")
    win_profit = (safe_odds(odds) - ONE) * (ONE - _rule4(context))
    place_profit = win_profit * Decimal(num) / Decimal(den)
    return (ONE + place_profit) / _divisor(context)


def _win_multiplier(leg: BaseLeg, context: LegSettlementContext) -> Decimal:
    status = resolved_status(context)
    odds = to_dec(leg.odds)
    match status:
        case LegStatus.VOID:
            return ONE
        case LegStatus.LOST:
            return ZERO
        case LegStatus.WON:
            return effective_win_odds(odds, context)
        case LegStatus.HALF_WON:
            return (effective_win_odds(odds, context) + ONE) / TWO
        case LegStatus.HALF_LOST:
            return ONE / TWO
    raise SettlementError(f"Leg '{leg.leg_id}' is not settled")


def _place_multiplier(leg: EachWayLeg, context: LegSettlementContext) -> Decimal:
    status = resolved_status(context)
    if status == LegStatus.VOID:
        return ONE
    if status in (LegStatus.HALF_WON, LegStatus.HALF_LOST):
        raise SettlementError(f"Half results are invalid for each-way leg '{leg.leg_id}'")
    if status == LegStatus.PENDING:
        raise SettlementError(f"Leg '{leg.leg_id}' is not settled")
    placed = status == LegStatus.WON or (
        context.finishing_position is not None and context.finishing_position <= leg.place_places
    )
    if not placed:
        return ZERO
    return effective_place_odds(to_dec(leg.odds), leg.place_numerator, leg.place_denominator, context)


# ---------- Helpers ----------

def _get_context(leg_id: str, contexts: dict[str, LegSettlementContext]) -> LegSettlementContext:
    ctx = contexts.get(leg_id)
    if ctx is None:
        raise SettlementError(f"Missing settlement context for leg '{leg_id}'")
    return ctx


def _derive_status(gross: Decimal, total: Decimal) -> LegStatus:
    if gross <= _EPS:
        return LegStatus.LOST
    diff = gross - total
    if abs(diff) <= _EPS:
        return LegStatus.VOID
    return LegStatus.WON if diff > 0 else LegStatus.HALF_LOST


def _finalize(status: LegStatus, total: Decimal, gross: Decimal, commission_pct: float) -> SettlementResult:
    net = gross - total
    rate = min(max(to_dec(commission_pct), ZERO), ONE)
    commission = net * rate if net > ZERO else ZERO
    payout = gross - commission
    return SettlementResult(
        status=status,
        total_stake=money(total),
        gross_payout=money(gross),
        commission=money(commission),
        payout=money(payout),
        profit=money(payout - total),
    )


def _pending(total: Decimal) -> SettlementResult:
    return SettlementResult(
        status=LegStatus.PENDING, total_stake=money(total),
        gross_payout=0.0, commission=0.0, payout=0.0, profit=0.0,
    )


def _line_payout(
    combo: tuple[BaseLeg, ...],
    stake: Decimal,
    statuses: dict[str, LegStatus],
    multipliers: dict[str, Decimal],
) -> Decimal:
    active = [leg for leg in combo if statuses[leg.leg_id] != LegStatus.VOID]
    if not active:
        return stake  # VOID CASCADE: fully voided line refunds its stake
    return stake * dprod(multipliers[leg.leg_id] for leg in active)


# ---------- Structure settlement ----------

def _settle_standard(structure: AnyBetStructure, contexts: dict[str, LegSettlementContext], commission: float) -> SettlementResult:
    legs = legs_of(structure)
    ctx_map = {leg.leg_id: _get_context(leg.leg_id, contexts) for leg in legs}
    statuses = {leg_id: resolved_status(ctx) for leg_id, ctx in ctx_map.items()}
    combos = structure_combinations(structure)
    stake = line_stake(structure)
    total = stake * Decimal(len(combos))

    if isinstance(structure, ParlayBet) and LegStatus.LOST in statuses.values():
        return _finalize(LegStatus.LOST, total, ZERO, commission)
    if LegStatus.PENDING in statuses.values():
        return _pending(total)

    multipliers = {leg.leg_id: _win_multiplier(leg, ctx_map[leg.leg_id]) for leg in legs}
    gross = sum((_line_payout(c, stake, statuses, multipliers) for c in combos), ZERO)

    if isinstance(structure, SingleBet):
        status = statuses[structure.leg.leg_id]
    elif all(s == LegStatus.VOID for s in statuses.values()):
        status = LegStatus.VOID
    else:
        status = _derive_status(gross, total)
    return _finalize(status, total, gross, commission)


def _settle_each_way(structure: EachWayBet, contexts: dict[str, LegSettlementContext], commission: float) -> SettlementResult:
    inner = structure.bet
    legs = legs_of(inner)
    ctx_map = {leg.leg_id: _get_context(leg.leg_id, contexts) for leg in legs}
    statuses = {leg_id: resolved_status(ctx) for leg_id, ctx in ctx_map.items()}
    combos = structure_combinations(inner)
    stake = line_stake(inner)
    total = TWO * stake * Decimal(len(combos))

    if LegStatus.PENDING in statuses.values():
        return _pending(total)

    win_mult = {leg.leg_id: _win_multiplier(leg, ctx_map[leg.leg_id]) for leg in legs}
    place_mult = {
        leg.leg_id: _place_multiplier(leg, ctx_map[leg.leg_id])  # type: ignore[arg-type]
        for leg in legs
    }
    win_payout = sum((_line_payout(c, stake, statuses, win_mult) for c in combos), ZERO)
    place_payout = sum((_line_payout(c, stake, statuses, place_mult) for c in combos), ZERO)
    gross = win_payout + place_payout

    if all(s == LegStatus.VOID for s in statuses.values()):
        status = LegStatus.VOID
    else:
        status = _derive_status(gross, total)
    return _finalize(status, total, gross, commission)


def _settle_lay(structure: LayBet, contexts: dict[str, LegSettlementContext], commission: float) -> SettlementResult:
    ctx = _get_context(structure.leg_id, contexts)
    status = resolved_status(ctx)
    liability = lay_liability(structure)
    if status == LegStatus.PENDING:
        return _pending(liability)

    backer_stake = to_dec(structure.backer_stake)
    backer_odds = effective_win_odds(to_dec(structure.odds), ctx)
    backer_profit = {
        LegStatus.WON: backer_stake * (backer_odds - ONE),
        LegStatus.HALF_WON: backer_stake * (backer_odds - ONE) / TWO,
        LegStatus.LOST: -backer_stake,
        LegStatus.HALF_LOST: -backer_stake / TWO,
        LegStatus.VOID: ZERO,
    }[status]

    layer_profit = -backer_profit
    gross = max(ZERO, liability + layer_profit)  # liability locked up front is returned + winnings
    return _finalize(_LAY_STATUS_MAP[status], liability, gross, commission)


def settle_bet(request: SettleRequest) -> SettlementResult:
    structure = request.structure
    if isinstance(structure, LayBet):
        return _settle_lay(structure, request.leg_contexts, request.commission_pct)
    if isinstance(structure, EachWayBet):
        return _settle_each_way(structure, request.leg_contexts, request.commission_pct)
    return _settle_standard(structure, request.leg_contexts, request.commission_pct)
