"""Multi-leg and conditional bet structures across the nine sports (Group 79). Developed for Amit Ashok Kumar Patnaik.

Each leg settles through its sport's adapter (``settle_market``) or carries a known status; the structure then
pays in exact decimal arithmetic (the bet engine's ``to_dec`` / ``money``):

* **Parlay**: the product of leg multipliers (WON: odds, HALF_WON: (odds + 1) / 2, VOID: 1, HALF_LOST: 1/2, LOST: 0).
* **Teaser**: every spread / total leg moved ``points`` in the bettor's favour (6.0, 6.5, 7.0 in American football;
  4.0, 4.5, 5.0 in basketball), paid from a fixed table by the number of legs. A pushed leg is removed and the
  teaser pays at the smaller size; fewer than two legs left refunds the stake.
* **Pleaser**: the reverse (legs moved against the bettor), a much larger table, and a push loses.
* **Round robin**: every parlay of the chosen sizes, one unit stake each.
* **If-bet**: singles in order; each later bet has action only when the one before it won (a half win counts).
  **Action reverse** is the if-bet run in both orders. **Double action** continues on a win, a push or a cancellation.
* **Futures / outrights**: a selection from a field; a dead heat between k winners divides the stake by k; a
  cancelled competition refunds.

Teaser and pleaser tables are American odds by (points, legs); quarter lines are not teasable.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.bet_types.calculator import ONE, ZERO, money, to_dec
from app.domain.sports.registry import SportAdapterRegistry
from app.domain.sports.settlement import SettlementInputError, is_win
from app.schemas.bet_types import LegStatus

TWO = Decimal(2)

TEASER_SPORTS: dict[str, tuple[float, ...]] = {"american_football": (6.0, 6.5, 7.0), "basketball": (4.0, 4.5, 5.0)}
TEASABLE_MARKETS = frozenset({"spread", "total", "point_spread", "game_total_over_under", "alt_spread", "alt_total", "team_total",
                              "team_totals", "team_total_points", "first_half_spread", "period_spread", "period_total"})
# American odds by point tier (index 0/1/2 = the sport's small/medium/large teaser) and number of legs
TEASER_TABLE: dict[int, dict[int, int]] = {
    0: {2: -110, 3: 180, 4: 300, 5: 450, 6: 600, 7: 800, 8: 1000},
    1: {2: -120, 3: 160, 4: 250, 5: 400, 6: 500, 7: 700, 8: 900},
    2: {2: -130, 3: 150, 4: 200, 5: 350, 6: 450, 7: 600, 8: 800},
}
PLEASER_TABLE: dict[int, dict[int, int]] = {
    0: {2: 600, 3: 1700, 4: 4000, 5: 9000, 6: 17500},
    1: {2: 700, 3: 2000, 4: 4500, 5: 10000, 6: 20000},
    2: {2: 800, 3: 2400, 4: 5000, 5: 11000, 6: 22500},
}
MAX_ROUND_ROBIN_LINES = 1013  # 10 legs, every size 2..10


class StructureError(ValueError):
    """The structure cannot be settled as requested."""


def american_to_decimal(american: int) -> Decimal:
    if american == 0:
        raise StructureError("American odds cannot be 0")
    a = Decimal(american)
    return ONE + (a / Decimal(100) if american > 0 else Decimal(100) / -a)


@dataclass
class LegSpec:
    leg_id: str
    odds: float = 1.0
    sport_key: str | None = None
    market_type: str | None = None
    selection: str | None = None
    line: float | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    match_result: Mapping[str, Any] | None = None
    status: LegStatus | None = None  # known status (wins over match_result)

    def settle(self, *, line_shift: float = 0.0) -> LegStatus:
        """The leg's status, after moving its line by ``line_shift`` (positive: towards the bettor)."""
        if self.status is not None and line_shift == 0.0:
            return self.status
        if self.match_result is None or not self.sport_key or not self.market_type or self.selection is None:
            if self.status is not None:
                raise StructureError(f"leg {self.leg_id!r} has a fixed status: it cannot be moved {line_shift:+g} points without its result")
            return LegStatus.PENDING
        adapter = SportAdapterRegistry.get_adapter(self.sport_key)
        line = self.line
        if line_shift:
            if line is None:
                raise StructureError(f"leg {self.leg_id!r} has no line to move")
            line = shifted_line(self.selection, line, line_shift)
        try:
            return adapter.settle_market(self.market_type, self.match_result, self.selection, line, dict(self.params))
        except SettlementInputError as exc:
            raise StructureError(f"leg {self.leg_id!r}: {exc}") from exc


def shifted_line(selection: str, line: float, shift: float) -> float:
    """Moving a line ``shift`` points towards the bettor: a handicap grows, an over's threshold falls, an under's rises."""
    tokens = str(selection).casefold().replace("_", " ").replace(":", " ").split()
    if "over" in tokens:
        return line - shift
    if "under" in tokens:
        return line + shift
    return line + shift


def leg_multiplier(odds: float, status: LegStatus) -> Decimal:
    o = max(to_dec(odds), ONE)
    match status:
        case LegStatus.WON:
            return o
        case LegStatus.HALF_WON:
            return (o + ONE) / TWO
        case LegStatus.VOID:
            return ONE
        case LegStatus.HALF_LOST:
            return ONE / TWO
        case LegStatus.LOST:
            return ZERO
    raise StructureError("a pending leg has no multiplier")


@dataclass(frozen=True)
class StructureSettlement:
    structure: str
    status: LegStatus
    total_stake: float
    payout: float
    profit: float
    legs: dict[str, str]
    lines: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"structure": self.structure, "status": self.status.value, "total_stake": self.total_stake, "payout": self.payout,
                "profit": self.profit, "legs": self.legs, "lines": self.lines, "detail": self.detail}


def _derive(total: Decimal, payout: Decimal) -> LegStatus:
    if payout <= ZERO:
        return LegStatus.LOST
    if payout == total:
        return LegStatus.VOID
    return LegStatus.WON if payout > total else LegStatus.HALF_LOST


def _finish(name: str, total: Decimal, payout: Decimal, statuses: Mapping[str, LegStatus], *, status: LegStatus | None = None,
            lines: list[dict[str, Any]] | None = None, detail: dict[str, Any] | None = None) -> StructureSettlement:
    final = status if status is not None else _derive(total, payout)
    return StructureSettlement(name, final, money(total), money(payout), money(payout - total), {k: v.value for k, v in statuses.items()},
                               lines or [], detail or {})


def _pending(name: str, total: Decimal, statuses: Mapping[str, LegStatus]) -> StructureSettlement:
    return StructureSettlement(name, LegStatus.PENDING, money(total), 0.0, 0.0, {k: v.value for k, v in statuses.items()})


def _unique(legs: Sequence[LegSpec]) -> None:
    ids = [leg.leg_id for leg in legs]
    if len(set(ids)) != len(ids):
        raise StructureError("every leg needs a unique leg_id")


# ================================================================ parlay & round robin
def settle_parlay(legs: Sequence[LegSpec], stake: float) -> StructureSettlement:
    if len(legs) < 2:
        raise StructureError("a parlay needs at least two legs")
    _unique(legs)
    statuses = {leg.leg_id: leg.settle() for leg in legs}
    total = to_dec(stake)
    if LegStatus.LOST in statuses.values():
        return _finish("parlay", total, ZERO, statuses, status=LegStatus.LOST)
    if LegStatus.PENDING in statuses.values():
        return _pending("parlay", total, statuses)
    product = ONE
    for leg in legs:
        product *= leg_multiplier(leg.odds, statuses[leg.leg_id])
    payout = total * product
    status = LegStatus.VOID if all(s is LegStatus.VOID for s in statuses.values()) else None
    return _finish("parlay", total, payout, statuses, status=status, detail={"combined_odds": float(product)})


def settle_round_robin(legs: Sequence[LegSpec], unit_stake: float, sizes: Sequence[int]) -> StructureSettlement:
    _unique(legs)
    n = len(legs)
    if n < 3:
        raise StructureError("a round robin needs at least three legs")
    chosen = sorted(set(int(k) for k in sizes))
    if not chosen or any(k < 2 or k > n for k in chosen):
        raise StructureError(f"round robin sizes must lie in 2..{n}")
    count = sum(math.comb(n, k) for k in chosen)
    if count > MAX_ROUND_ROBIN_LINES:
        raise StructureError(f"{count} parlays exceed the {MAX_ROUND_ROBIN_LINES} limit")
    statuses = {leg.leg_id: leg.settle() for leg in legs}
    unit = to_dec(unit_stake)
    total = unit * Decimal(count)
    lines: list[dict[str, Any]] = []
    gross = ZERO
    pending = False
    for k in chosen:
        for combo in itertools.combinations(legs, k):
            ids = [leg.leg_id for leg in combo]
            st = [statuses[i] for i in ids]
            if LegStatus.LOST in st:
                lines.append({"legs": ids, "status": LegStatus.LOST.value, "payout": 0.0})
                continue
            if LegStatus.PENDING in st:
                pending = True
                lines.append({"legs": ids, "status": LegStatus.PENDING.value, "payout": 0.0})
                continue
            product = ONE
            for leg in combo:
                product *= leg_multiplier(leg.odds, statuses[leg.leg_id])
            line_payout = unit * product
            gross += line_payout
            lines.append({"legs": ids, "status": _derive(unit, line_payout).value, "payout": money(line_payout)})
    if pending:
        return StructureSettlement("round_robin", LegStatus.PENDING, money(total), 0.0, 0.0, {k: v.value for k, v in statuses.items()}, lines)
    return _finish("round_robin", total, gross, statuses, lines=lines, detail={"parlays": count, "sizes": chosen})


# ================================================================ teaser & pleaser
def _tier(sport_key: str, points: float) -> int:
    tiers = TEASER_SPORTS.get(sport_key)
    if tiers is None:
        raise StructureError(f"{sport_key} is not teasable (American football and basketball only)")
    for i, p in enumerate(tiers):
        if abs(p - points) < 1e-9:
            return i
    raise StructureError(f"{sport_key} teasers move {', '.join(f'{p:g}' for p in tiers)} points, not {points:g}")


def _teaser_legs(legs: Sequence[LegSpec], points: float) -> int:
    _unique(legs)
    if len(legs) < 2:
        raise StructureError("a teaser needs at least two legs")
    sports = {leg.sport_key for leg in legs}
    tiers = {_tier(str(s), points) for s in sports}
    if len(tiers) != 1:
        raise StructureError("every teaser leg must take the same point tier")
    for leg in legs:
        if str(leg.market_type or "").casefold() not in TEASABLE_MARKETS:
            raise StructureError(f"leg {leg.leg_id!r}: only spreads and totals can be teased")
        if leg.line is None or abs(leg.line * 2 - round(leg.line * 2)) > 1e-9:
            raise StructureError(f"leg {leg.leg_id!r}: teaser lines are whole or half points")
    return tiers.pop()


def settle_teaser(legs: Sequence[LegSpec], stake: float, points: float) -> StructureSettlement:
    tier = _teaser_legs(legs, points)
    statuses = {leg.leg_id: leg.settle(line_shift=points) for leg in legs}
    total = to_dec(stake)
    detail: dict[str, Any] = {"points": points, "teased_lines": {leg.leg_id: shifted_line(str(leg.selection), float(leg.line), points) for leg in legs}}  # type: ignore[arg-type]
    if any(s in (LegStatus.LOST, LegStatus.HALF_LOST) for s in statuses.values()):
        return _finish("teaser", total, ZERO, statuses, status=LegStatus.LOST, detail=detail)
    if LegStatus.PENDING in statuses.values():
        return _pending("teaser", total, statuses)
    live = [i for i, s in statuses.items() if s is not LegStatus.VOID]
    if len(live) < 2:
        return _finish("teaser", total, total, statuses, status=LegStatus.VOID, detail={**detail, "reason": "fewer than two legs left after pushes"})
    american = TEASER_TABLE[tier].get(len(live))
    if american is None:
        raise StructureError(f"no teaser price for {len(live)} legs")
    odds = american_to_decimal(american)
    detail.update({"legs_paid": len(live), "american_odds": american, "decimal_odds": float(odds)})
    return _finish("teaser", total, total * odds, statuses, status=LegStatus.WON, detail=detail)


def settle_pleaser(legs: Sequence[LegSpec], stake: float, points: float) -> StructureSettlement:
    tier = _teaser_legs(legs, points)
    statuses = {leg.leg_id: leg.settle(line_shift=-points) for leg in legs}
    total = to_dec(stake)
    detail: dict[str, Any] = {"points": points, "pleased_lines": {leg.leg_id: shifted_line(str(leg.selection), float(leg.line), -points) for leg in legs}}  # type: ignore[arg-type]
    if any(s not in (LegStatus.WON, LegStatus.PENDING) for s in statuses.values()):
        return _finish("pleaser", total, ZERO, statuses, status=LegStatus.LOST, detail={**detail, "rule": "a push or a loss loses a pleaser"})
    if LegStatus.PENDING in statuses.values():
        return _pending("pleaser", total, statuses)
    american = PLEASER_TABLE[tier].get(len(legs))
    if american is None:
        raise StructureError(f"no pleaser price for {len(legs)} legs")
    odds = american_to_decimal(american)
    detail.update({"american_odds": american, "decimal_odds": float(odds)})
    return _finish("pleaser", total, total * odds, statuses, status=LegStatus.WON, detail=detail)


# ================================================================ if-bets
CONTINUE_ON_WIN = frozenset({LegStatus.WON, LegStatus.HALF_WON})
CONTINUE_ON_WIN_TIE_CANCEL = frozenset({LegStatus.WON, LegStatus.HALF_WON, LegStatus.VOID})


def _if_chain(legs: Sequence[LegSpec], stakes: Sequence[Decimal], proceed: frozenset[LegStatus]) -> tuple[Decimal, Decimal, list[dict[str, Any]], bool]:
    """Returns (risked, gross returned, steps, pending). Only the first stake is risked up front; later stakes are placed
    from the chain as it proceeds, so their losses count against the return."""
    risked = stakes[0]
    gross = ZERO
    steps: list[dict[str, Any]] = []
    for i, (leg, stake) in enumerate(zip(legs, stakes, strict=True)):
        status = leg.settle()
        if status is LegStatus.PENDING:
            steps.append({"leg_id": leg.leg_id, "status": status.value, "action": True})
            return risked, gross, steps, True
        back = stake * leg_multiplier(leg.odds, status)
        if i == 0:
            gross += back
        else:
            gross += back - stake  # funded from the chain: its net result
        steps.append({"leg_id": leg.leg_id, "status": status.value, "action": True, "stake": money(stake), "return": money(back)})
        if status not in proceed:
            for rest in legs[i + 1:]:
                steps.append({"leg_id": rest.leg_id, "status": "NO_ACTION", "action": False})
            break
    return risked, max(gross, ZERO), steps, False


def settle_if_bet(legs: Sequence[LegSpec], stakes: Sequence[float], *, double_action: bool = False, reverse: bool = False) -> StructureSettlement:
    """``reverse`` (action reverse) adds the same chain in reverse order with the same stakes reversed."""
    _unique(legs)
    if len(legs) < 2:
        raise StructureError("an if-bet needs at least two legs")
    if len(stakes) != len(legs):
        raise StructureError("an if-bet needs one stake per leg")
    proceed = CONTINUE_ON_WIN_TIE_CANCEL if double_action else CONTINUE_ON_WIN
    dstakes = [to_dec(s) for s in stakes]
    if any(s <= ZERO for s in dstakes):
        raise StructureError("if-bet stakes must be positive")
    chains = [(list(legs), dstakes)]
    if reverse:
        chains.append((list(reversed(legs)), list(reversed(dstakes))))
    total = ZERO
    gross = ZERO
    detail_chains = []
    pending = False
    statuses: dict[str, LegStatus] = {}
    for chain_legs, chain_stakes in chains:
        risked, back, steps, is_pending = _if_chain(chain_legs, chain_stakes, proceed)
        total += risked
        gross += back
        pending = pending or is_pending
        detail_chains.append({"order": [leg.leg_id for leg in chain_legs], "steps": steps, "risked": money(risked), "return": money(back)})
        for step in steps:
            if step["status"] in LegStatus.__members__:
                statuses[step["leg_id"]] = LegStatus(step["status"])
    name = "action_reverse" if reverse else ("double_action" if double_action else "if_bet")
    if pending:
        return StructureSettlement(name, LegStatus.PENDING, money(total), 0.0, 0.0, {k: v.value for k, v in statuses.items()}, [],
                                   {"chains": detail_chains})
    return _finish(name, total, gross, statuses, detail={"chains": detail_chains,
                                                       "condition": "win, tie or cancel" if double_action else "win only"})


# ================================================================ futures / outrights
def settle_future(selection: str, odds: float, stake: float, winners: Sequence[str] | None, *, cancelled: bool = False) -> StructureSettlement:
    total = to_dec(stake)
    key = str(selection).strip().casefold()
    if cancelled:
        return _finish("futures", total, total, {"selection": LegStatus.VOID}, status=LegStatus.VOID)
    if winners is None:
        return _pending("futures", total, {"selection": LegStatus.PENDING})
    field_winners = [str(w).strip().casefold() for w in winners]
    if not field_winners:
        raise StructureError("a settled outright needs at least one winner")
    if key not in field_winners:
        return _finish("futures", total, ZERO, {"selection": LegStatus.LOST}, status=LegStatus.LOST)
    divisor = Decimal(len(field_winners))
    payout = total * max(to_dec(odds), ONE) / divisor
    return _finish("futures", total, payout, {"selection": LegStatus.WON}, detail={"dead_heat_divisor": int(divisor)})


# ================================================================ fair pricing of structures
def parlay_fair_probability(probabilities: Sequence[float]) -> float:
    """Independent legs: the product. (Same-game legs are correlated: price those with a joint model.)"""
    p = 1.0
    for q in probabilities:
        if not 0.0 < q <= 1.0:
            raise StructureError("leg probabilities must lie in (0, 1]")
        p *= q
    return p


def teaser_expected_value(cover_probabilities: Sequence[float], sport_key: str, points: float) -> dict[str, float]:
    """EV per unit of a teaser whose legs cover (at the teased lines) with the given probabilities, pushes ignored."""
    tier = _tier(sport_key, points)
    american = TEASER_TABLE[tier].get(len(cover_probabilities))
    if american is None:
        raise StructureError(f"no teaser price for {len(cover_probabilities)} legs")
    odds = float(american_to_decimal(american))
    p = parlay_fair_probability(cover_probabilities)
    return {"probability": p, "decimal_odds": odds, "expected_value": p * odds - 1.0,
            "break_even_leg_probability": (1.0 / odds) ** (1.0 / len(cover_probabilities))}


def leg_wins(status: LegStatus) -> bool:
    return is_win(status)
