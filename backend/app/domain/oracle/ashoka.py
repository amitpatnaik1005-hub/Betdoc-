import logging
import math
from math import prod

from pydantic import ValidationError

from app.domain.bet_types.calculator import calculate_potential_return, structure_combinations
from app.domain.math.utilities import kelly_criterion
from app.domain.oracle.temperature import calculate_risk_temperature
from app.domain.risk.common import safe_float
from app.schemas.bet_structures import (
    MAX_UNIT_STAKE,
    AnyBetStructure,
    CanadianBet,
    GoliathBet,
    HeinzBet,
    NamedSystemBet,
    ParlayBet,
    SingleBet,
    SuperHeinzBet,
    TrixieBet,
    YankeeBet,
)
from app.schemas.bet_types import MarketType, SimpleLeg
from app.schemas.math import ValueBetFlag
from app.schemas.oracle import (
    OracleContext,
    OracleResponse,
    OracleStrategyType,
    OracleSuggestion,
)

ashoka_log = logging.getLogger("betdoc.ashoka")

# "Without singles" systems: no overlap with the core singles layer.
SYSTEMS_WITHOUT_SINGLES: dict[int, type[NamedSystemBet]] = {
    3: TrixieBet,
    4: YankeeBet,
    5: CanadianBet,
    6: HeinzBet,
    7: SuperHeinzBet,
    8: GoliathBet,
}
ABSOLUTE_MAX_SYSTEM_LEGS = max(SYSTEMS_WITHOUT_SINGLES)  # 8
CONVEXITY_STRATEGIES = frozenset({OracleStrategyType.AGGRESSIVE_GROWTH, OracleStrategyType.BALANCED})
DEFENSIVE_STRATEGIES = frozenset({OracleStrategyType.CAPITAL_PRESERVATION, OracleStrategyType.RECOVERY})


def floor_money(value: float) -> float:
    """Round DOWN to 4 dp so allocations never exceed their budget."""
    return math.floor(safe_float(value) * 10_000.0) / 10_000.0


def structure_true_ev(structure: AnyBetStructure) -> float:
    """Average fold EV across all equal-stake lines (assumes independent legs)."""
    combos = structure_combinations(structure)
    if not combos:
        return 0.0
    fold_evs: list[float] = []
    for fold in combos:
        fold_true_prob = prod(safe_float(leg.true_probability) for leg in fold)
        fold_odds = prod(safe_float(leg.odds) for leg in fold)
        fold_evs.append(fold_true_prob * fold_odds - 1.0)
    return safe_float(sum(fold_evs) / len(fold_evs))


class AshokaOracle:
    def __init__(self, core_allocation: float = 0.85, min_stake: float = 0.01) -> None:
        if not 0.0 < core_allocation <= 1.0:
            raise ValueError("core_allocation must be in (0, 1]")
        if min_stake <= 0.0:
            raise ValueError("min_stake must be positive")
        self.core_allocation = core_allocation
        self.min_stake = min_stake

    # ---------- Public API ----------

    def generate_suggestions(self, context: OracleContext) -> OracleResponse:
        params = context.strategy_params
        temp_data = calculate_risk_temperature(context.risk_metrics, params)
        temperature: float = temp_data["temperature"]
        kelly_multiplier: float = temp_data["kelly_multiplier"]
        strategy: OracleStrategyType = temp_data["strategy"]

        # PHASE 1: Capital authorization
        available_exposure_pct = safe_float(context.max_exposure_pct) - safe_float(context.risk_metrics.exposure_pct)
        if available_exposure_pct <= 0.0:
            ashoka_log.info("No exposure headroom (%.2f%%); returning empty response", available_exposure_pct)
            return self._response([], strategy, temperature, 0.0)

        bankroll = safe_float(context.bankroll)
        max_fiat_to_deploy = bankroll * (available_exposure_pct / 100.0)
        core_fiat_limit = max_fiat_to_deploy * self.core_allocation
        current_fiat_deployed = 0.0
        suggestions: list[OracleSuggestion] = []

        # PHASE 2: Uncorrelated leg extraction + strict casting
        valid_legs = self._extract_legs(context.available_value_bets, strategy, params.target_win_rate)
        if not valid_legs:
            return self._response([], strategy, temperature, 0.0)

        # PHASE 3: Core portfolio (Kelly singles)
        for leg in valid_legs:
            if core_fiat_limit - current_fiat_deployed < self.min_stake:
                break  # core budget exhausted

            raw_kelly = kelly_criterion(true_prob=leg.true_probability, decimal_odds=leg.odds)
            stake = bankroll * raw_kelly * kelly_multiplier * params.base_kelly_fraction
            if current_fiat_deployed + stake > core_fiat_limit:
                stake = core_fiat_limit - current_fiat_deployed
            
            stake = floor_money(min(stake, MAX_UNIT_STAKE))
            if stake < self.min_stake:
                continue  # dust for this leg; budget may still fund the next one

            try:
                structure = SingleBet(unit_stake=stake, leg=leg)
            except ValidationError as exc:
                ashoka_log.warning("Core single rejected for leg %s: %s", leg.leg_id, exc.errors())
                continue

            cost = calculate_potential_return(structure).total_cost
            ev = structure_true_ev(structure)
            suggestions.append(
                OracleSuggestion(
                    structure=structure,
                    total_ev_pct=round(ev * 100.0, 4),
                    capital_allocated=round(cost, 4),
                    rationale=(
                        f"CORE single {leg.selection} @ {leg.odds:.2f} (match {leg.match_id}): "
                        f"p={leg.true_probability:.2%}, EV={ev:+.2%}, "
                        f"Kelly {raw_kelly:.2%} x temp {kelly_multiplier:.2f} x base {params.base_kelly_fraction:.2f}"
                    ),
                )
            )
            current_fiat_deployed += cost

        # PHASE 4: Satellite overlay (convexity)
        n_legs = min(len(valid_legs), params.max_legs_per_combination, ABSOLUTE_MAX_SYSTEM_LEGS)
        if strategy in CONVEXITY_STRATEGIES and n_legs >= 2:
            convexity_budget = max_fiat_to_deploy - current_fiat_deployed
            if convexity_budget > 0.0:
                satellite = self._build_satellite(valid_legs[:n_legs], convexity_budget)
                if satellite is not None:
                    calc = calculate_potential_return(satellite)
                    ev = structure_true_ev(satellite)  # PHASE 5
                    suggestions.append(
                        OracleSuggestion(
                            structure=satellite,
                            total_ev_pct=round(ev * 100.0, 4),
                            capital_allocated=round(calc.total_cost, 4),
                            rationale=(
                                f"SATELLITE {satellite.structure_type}: {n_legs} uncorrelated legs, "
                                f"{calc.number_of_bets} lines @ {satellite.unit_stake:.4f}, "
                                f"avg fold EV={ev:+.2%} (assumes leg independence)"
                            ),
                        )
                    )
                    current_fiat_deployed += calc.total_cost

        # PHASE 6: Output
        ashoka_log.info(
            "ASHOKA strategy=%s temp=%.3f suggestions=%d deployed=%.4f / authorized=%.4f",
            strategy, temperature, len(suggestions), current_fiat_deployed, max_fiat_to_deploy,
        )
        return self._response(suggestions, strategy, temperature, current_fiat_deployed)

    # ---------- Internals ----------

    def _extract_legs(
        self,
        flags: list[ValueBetFlag],
        strategy: OracleStrategyType,
        target_win_rate: float,
    ) -> list[SimpleLeg]:
        best_per_match: dict[str, tuple[float, ValueBetFlag]] = {}
        for flag in flags:
            if safe_float(flag.expected_value) <= 0.0:
                continue
            match_id = (flag.match_id or "").strip()
            if not match_id:
                ashoka_log.debug("Skipping flag %s: missing match_id (cannot verify correlation)", flag.leg_id)
                continue
            # Trust the math, not the client: recompute EV from p and odds.
            recomputed_ev = safe_float(flag.true_prob) * safe_float(flag.bookmaker_odds) - 1.0
            if recomputed_ev <= 0.0:
                continue
            current = best_per_match.get(match_id)
            if current is None or recomputed_ev > current[0]:
                best_per_match[match_id] = (recomputed_ev, flag)

        ordered = sorted(best_per_match.items(), key=lambda item: item[1][0], reverse=True)

        legs: list[SimpleLeg] = []
        seen_leg_ids: set[str] = set()
        for match_id, (_ev, flag) in ordered:
            if flag.leg_id in seen_leg_ids:
                continue
            if strategy in DEFENSIVE_STRATEGIES and safe_float(flag.true_prob) < target_win_rate:
                continue
            try:
                leg = SimpleLeg(
                    leg_id=str(flag.leg_id),
                    match_id=str(match_id),
                    market_type=MarketType.MATCH_WINNER_1X2.value,
                    selection=str(flag.selection),
                    odds=float(flag.bookmaker_odds),
                    true_probability=float(flag.true_prob),
                )
            except ValidationError as exc:
                ashoka_log.warning("Rejected flag %s during leg cast: %s", flag.leg_id, exc.errors())
                continue
            seen_leg_ids.add(leg.leg_id)
            legs.append(leg)
        return legs

    def _build_satellite(self, system_legs: list[SimpleLeg], budget: float) -> AnyBetStructure | None:
        n = len(system_legs)
        try:
            if n == 2:
                unit_stake = floor_money(min(budget, MAX_UNIT_STAKE))
                if unit_stake < self.min_stake:
                    return None
                return ParlayBet(unit_stake=unit_stake, legs=system_legs)

            structure_cls = SYSTEMS_WITHOUT_SINGLES.get(n)
            if structure_cls is None:
                return None
            unit_stake = floor_money(min(budget / structure_cls.EXPECTED_BETS, MAX_UNIT_STAKE))
            if unit_stake < self.min_stake:
                return None
            return structure_cls(unit_stake=unit_stake, legs=system_legs)
        except ValidationError as exc:
            ashoka_log.warning("Satellite construction rejected: %s", exc.errors())
            return None

    @staticmethod
    def _response(
        suggestions: list[OracleSuggestion],
        strategy: OracleStrategyType,
        temperature: float,
        deployed: float,
    ) -> OracleResponse:
        return OracleResponse(
            suggestions=suggestions,
            strategy_used=strategy,
            risk_temperature=round(safe_float(temperature), 4),
            total_capital_deployed=round(safe_float(deployed), 4),
        )
