"""PhantomManager: GARUDA's parameter-driven quantitative engines.

Every business number comes from the caller. The only literals are mathematical
identities (decimal odds > 1, probabilities sum to 1, percent = /100) and the
2-decimal money precision / column sizes from the specification.
"""

import json
import logging
import math
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.phantom.errors import PhantomDomainError
from app.models.phantom import ArbitrageOpportunityModel, CalcType, PhantomCalculationLogModel

logger = logging.getLogger("betdoc.phantom.garuda")

MONEY_DECIMALS = 2
JSON_COLUMN_MAX_LENGTH = 2048
EVENT_NAME_MAX_LENGTH = 128
MARKET_TYPE_MAX_LENGTH = 64


class MatchedBettingMode(StrEnum):
    STANDARD = "STANDARD"
    UNDERLAY = "UNDERLAY"
    OVERLAY = "OVERLAY"


class CointegrationSignal(StrEnum):
    STOP_LOSS_LIQUIDATE = "STOP_LOSS_LIQUIDATE"
    SELL_SPREAD = "SELL_SPREAD"
    BUY_SPREAD = "BUY_SPREAD"
    CLOSE_POSITION = "CLOSE_POSITION"
    HOLD = "HOLD"


# --------------------------------------------------------------------------- validation helpers


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_finite(**values: Any) -> None:
    for name, value in values.items():
        if not _is_number(value) or not math.isfinite(value):
            raise PhantomDomainError(f"{name} must be a finite number.", field=name)


def _require_finite_list(name: str, values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)):
        raise PhantomDomainError(f"{name} must be a list of numbers.", field=name)
    for index, value in enumerate(values):
        _require_finite(**{f"{name}[{index}]": value})
    return [float(v) for v in values]


def _require_decimal_odds(name: str, values: Sequence[float]) -> None:
    for index, value in enumerate(values):
        if value <= 1.0:
            raise PhantomDomainError(f"{name}[{index}] must be decimal odds greater than 1.", field=name)


def _require_commission_pct(name: str, value: float) -> None:
    if not 0.0 <= value < 100.0:
        raise PhantomDomainError(f"{name} must be in the range [0, 100).", field=name)


def _require_finite_outputs(**values: float) -> None:
    for name, value in values.items():
        if not math.isfinite(value):
            raise PhantomDomainError(f"{name} overflowed to a non-finite value; inputs are out of range.", field=name)


def _money(value: float) -> float:
    return round(value, MONEY_DECIMALS) + 0.0  # + 0.0 normalises -0.0


def _dumps(payload: Any, label: str) -> str:
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PhantomDomainError(f"{label} is not JSON-serialisable.") from exc
    if len(encoded) > JSON_COLUMN_MAX_LENGTH:
        raise PhantomDomainError(f"{label} exceeds {JSON_COLUMN_MAX_LENGTH} characters.")
    return encoded


def _require_text(name: str, value: Any, max_length: int) -> str:
    text = (value if isinstance(value, str) else "").strip()
    if not text:
        raise PhantomDomainError(f"{name} must not be blank.", field=name)
    if len(text) > max_length:
        raise PhantomDomainError(f"{name} must be at most {max_length} characters.", field=name)
    return text


# --------------------------------------------------------------------------- manager


class PhantomManager:
    async def _persist(self, db: AsyncSession, instance: Any, label: str) -> Any:
        db.add(instance)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            logger.warning("[GARUDA]: %s rejected by storage constraints.", label)
            raise PhantomDomainError(f"{label} rejected by storage constraints.") from exc
        await db.refresh(instance)
        return instance

    async def _log_calculation(
        self, db: AsyncSession, calc_type: CalcType, inputs: dict[str, Any], outputs: dict[str, Any]
    ) -> PhantomCalculationLogModel:
        log = PhantomCalculationLogModel(
            calc_type=calc_type.value,
            inputs_json=_dumps(inputs, f"{calc_type.value} inputs"),
            outputs_json=_dumps(outputs, f"{calc_type.value} outputs"),
        )
        return await self._persist(db, log, f"{calc_type.value} calculation log")

    # ------------------------------------------------------------------ 1. arbitrage

    async def detect_arbitrage(
        self,
        db: AsyncSession,
        event_name: str,
        market_type: str,
        odds: Sequence[float],
        commissions_pct: Sequence[float],
        target_total_stake: float,
        minimum_profit_margin_pct: float,
    ) -> dict[str, Any]:
        event = _require_text("event_name", event_name, EVENT_NAME_MAX_LENGTH)
        market = _require_text("market_type", market_type, MARKET_TYPE_MAX_LENGTH)
        odds_list = _require_finite_list("odds", odds)
        commissions = _require_finite_list("commissions_pct", commissions_pct)
        if len(odds_list) < 2:
            raise PhantomDomainError("odds must contain at least two outcomes.", field="odds")
        if len(odds_list) != len(commissions):
            raise PhantomDomainError(
                f"odds ({len(odds_list)}) and commissions_pct ({len(commissions)}) must have the same length."
            )
        _require_decimal_odds("odds", odds_list)
        for index, commission in enumerate(commissions):
            _require_commission_pct(f"commissions_pct[{index}]", commission)
        _require_finite(target_total_stake=target_total_stake, minimum_profit_margin_pct=minimum_profit_margin_pct)
        if target_total_stake <= 0:
            raise PhantomDomainError("target_total_stake must be greater than zero.")

        effective_odds = [1.0 + (o - 1.0) * (1.0 - (c / 100.0)) for o, c in zip(odds_list, commissions, strict=True)]
        if any(eo <= 0 for eo in effective_odds):
            raise PhantomDomainError("Effective odds must be positive.")
        implied_probs = [1.0 / eo for eo in effective_odds]
        total_prob = sum(implied_probs)
        if total_prob <= 0:
            raise PhantomDomainError("Total implied probability must be positive.")

        profit_pct = ((1.0 / total_prob) - 1.0) * 100.0
        weights = [p / total_prob for p in implied_probs]
        stakes = [round(weight * target_total_stake, MONEY_DECIMALS) for weight in weights]
        total_staked = _money(sum(stakes))
        leg_returns = [_money(stake * eo) for stake, eo in zip(stakes, effective_odds, strict=True)]
        guaranteed_profit = _money(min(leg_returns) - total_staked)
        is_arbitrage = total_prob < 1.0 and profit_pct >= minimum_profit_margin_pct

        opportunity: ArbitrageOpportunityModel | None = None
        if is_arbitrage:
            opportunity = await self._persist(
                db,
                ArbitrageOpportunityModel(
                    event_name=event,
                    market_type=market,
                    total_implied_probability=total_prob,
                    guaranteed_profit_pct=profit_pct,
                    target_total_stake=target_total_stake,
                    stakes_json=_dumps(stakes, "Arbitrage stakes"),
                    is_active=True,
                ),
                "Arbitrage opportunity",
            )
            logger.info(
                "[GARUDA]: arbitrage locked on %s / %s: %d legs, %.4f%% guaranteed.",
                event,
                market,
                len(odds_list),
                profit_pct,
            )
        else:
            logger.info(
                "[GARUDA]: no qualifying arbitrage on %s / %s (book %.6f, margin %.4f%%, required %.4f%%).",
                event,
                market,
                total_prob,
                profit_pct,
                minimum_profit_margin_pct,
            )

        result = {
            "is_arbitrage": is_arbitrage,
            "total_implied_probability": total_prob,
            "guaranteed_profit_pct": profit_pct,
            "effective_odds": effective_odds,
            "implied_probabilities": implied_probs,
            "stake_weights": weights,
            "stakes": stakes,
            "total_staked": total_staked,
            "leg_returns": leg_returns,
            "guaranteed_profit": guaranteed_profit,
        }
        return {"result": result, "opportunity": opportunity}

    # ------------------------------------------------------------------ 2. dutching

    async def calculate_dutching(
        self, db: AsyncSession, target_total_stake: float, odds: Sequence[float]
    ) -> dict[str, Any]:
        odds_list = _require_finite_list("odds", odds)
        if len(odds_list) < 2:
            raise PhantomDomainError("odds must contain at least two selections.", field="odds")
        _require_decimal_odds("odds", odds_list)
        _require_finite(target_total_stake=target_total_stake)
        if target_total_stake <= 0:
            raise PhantomDomainError("target_total_stake must be greater than zero.")

        implied_probs = [1.0 / o for o in odds_list]
        total_implied = sum(implied_probs)
        if total_implied <= 0:
            raise PhantomDomainError("Total implied probability must be positive.")
        stakes = [round((imp / total_implied) * target_total_stake, MONEY_DECIMALS) for imp in implied_probs]
        guaranteed_return = round(target_total_stake / total_implied, MONEY_DECIMALS)
        leg_returns = [_money(stake * o) for stake, o in zip(stakes, odds_list, strict=True)]
        profit = _money(guaranteed_return - target_total_stake)

        result = {
            "stakes": stakes,
            "guaranteed_return": guaranteed_return,
            "total_implied_probability": total_implied,
            "leg_returns": leg_returns,
            "profit": profit,
        }
        log = await self._log_calculation(
            db,
            CalcType.DUTCHING,
            {"target_total_stake": target_total_stake, "odds": odds_list},
            result,
        )
        logger.info("[GARUDA]: dutched %d selections -> return %.2f.", len(odds_list), guaranteed_return)
        return {"result": result, "log": log}

    # ------------------------------------------------------------------ 3. matched betting

    async def calculate_matched_bet(
        self,
        db: AsyncSession,
        back_stake: float,
        back_odds: float,
        lay_odds: float,
        lay_commission_pct: float,
        mode: MatchedBettingMode | str,
    ) -> dict[str, Any]:
        try:
            resolved_mode = MatchedBettingMode(mode)
        except ValueError as exc:
            raise PhantomDomainError(f"Unsupported matched betting mode {mode!r}.") from exc
        _require_finite(
            back_stake=back_stake, back_odds=back_odds, lay_odds=lay_odds, lay_commission_pct=lay_commission_pct
        )
        if back_stake <= 0:
            raise PhantomDomainError("back_stake must be greater than zero.")
        if back_odds <= 1.0:
            raise PhantomDomainError("back_odds must be decimal odds greater than 1.")
        if lay_odds == 1.0:
            raise PhantomDomainError("lay_odds of 1.0 causes division by zero.")
        if lay_odds < 1.0:
            raise PhantomDomainError("lay_odds must be decimal odds greater than 1.")
        _require_commission_pct("lay_commission_pct", lay_commission_pct)

        commission = lay_commission_pct / 100.0
        if resolved_mode is MatchedBettingMode.STANDARD:
            denominator = lay_odds - commission
            numerator = back_odds * back_stake
        elif resolved_mode is MatchedBettingMode.UNDERLAY:
            denominator = 1.0 - commission
            numerator = back_stake
        else:
            denominator = lay_odds - 1.0
            numerator = back_stake * (back_odds - 1.0)
        if denominator <= 0:
            raise PhantomDomainError(f"{resolved_mode.value} lay stake denominator is not positive.")

        lay_stake = round(numerator / denominator, MONEY_DECIMALS)
        back_win_profit = _money((back_stake * (back_odds - 1.0)) - (lay_stake * (lay_odds - 1.0)))
        lay_win_profit = _money(lay_stake * (1.0 - commission) - back_stake)
        lay_liability = _money(lay_stake * (lay_odds - 1.0))
        _require_finite_outputs(lay_stake=lay_stake, back_win_profit=back_win_profit, lay_win_profit=lay_win_profit)

        result = {
            "mode": resolved_mode.value,
            "lay_stake": lay_stake,
            "lay_liability": lay_liability,
            "back_win_profit": back_win_profit,
            "lay_win_profit": lay_win_profit,
        }
        log = await self._log_calculation(
            db,
            CalcType.MATCHED_BETTING,
            {
                "back_stake": back_stake,
                "back_odds": back_odds,
                "lay_odds": lay_odds,
                "lay_commission_pct": lay_commission_pct,
                "mode": resolved_mode.value,
            },
            result,
        )
        logger.info(
            "[GARUDA]: %s matched bet -> lay %.2f (back win %.2f / lay win %.2f).",
            resolved_mode.value,
            lay_stake,
            back_win_profit,
            lay_win_profit,
        )
        return {"result": result, "log": log}

    # ------------------------------------------------------------------ 4. Avellaneda-Stoikov

    async def calculate_market_maker_quotes(
        self,
        db: AsyncSession,
        mid_price: float,
        inventory: float,
        gamma: float,
        volatility_sigma: float,
        time_horizon_t: float,
        current_time_t: float,
        liquidity_k: float,
    ) -> dict[str, Any]:
        _require_finite(
            mid_price=mid_price,
            inventory=inventory,
            gamma=gamma,
            volatility_sigma=volatility_sigma,
            time_horizon_t=time_horizon_t,
            current_time_t=current_time_t,
            liquidity_k=liquidity_k,
        )
        if gamma == 0:
            raise PhantomDomainError("gamma of 0 causes division by zero.")
        if gamma < 0:
            raise PhantomDomainError("gamma (risk aversion) must be greater than zero.")
        if liquidity_k == 0:
            raise PhantomDomainError("liquidity_k of 0 causes division by zero.")
        if liquidity_k < 0:
            raise PhantomDomainError("liquidity_k must be greater than zero.")
        if volatility_sigma < 0:
            raise PhantomDomainError("volatility_sigma must not be negative.")

        try:
            dt = max(0.0, time_horizon_t - current_time_t)
            variance_term = gamma * (volatility_sigma**2) * dt
            reservation_price = mid_price - (inventory * variance_term)
            spread = variance_term + ((2.0 / gamma) * math.log1p(gamma / liquidity_k))
        except (OverflowError, ZeroDivisionError, ValueError) as exc:
            raise PhantomDomainError("Market-maker inputs overflow the model.") from exc
        _require_finite_outputs(reservation_price=reservation_price, optimal_spread=spread)

        optimal_ask = round(reservation_price + (spread / 2.0), MONEY_DECIMALS)
        optimal_bid = round(reservation_price - (spread / 2.0), MONEY_DECIMALS)
        _require_finite_outputs(optimal_ask=optimal_ask, optimal_bid=optimal_bid)

        result = {
            "time_remaining": dt,
            "reservation_price": reservation_price,
            "optimal_spread": spread,
            "optimal_ask": optimal_ask,
            "optimal_bid": optimal_bid,
            "inventory_skew": reservation_price - mid_price,
        }
        log = await self._log_calculation(
            db,
            CalcType.AVELLANEDA,
            {
                "mid_price": mid_price,
                "inventory": inventory,
                "gamma": gamma,
                "volatility_sigma": volatility_sigma,
                "time_horizon_t": time_horizon_t,
                "current_time_t": current_time_t,
                "liquidity_k": liquidity_k,
            },
            result,
        )
        logger.info(
            "[GARUDA]: A-S quotes bid %.2f / ask %.2f (reservation %.6f, spread %.6f).",
            optimal_bid,
            optimal_ask,
            reservation_price,
            spread,
        )
        return {"result": result, "log": log}

    # ------------------------------------------------------------------ 5. cointegration

    async def evaluate_cointegration(
        self,
        db: AsyncSession,
        current_z_score: float,
        entry_threshold: float,
        exit_threshold: float,
        stop_loss_threshold: float,
    ) -> dict[str, Any]:
        _require_finite(
            current_z_score=current_z_score,
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold,
            stop_loss_threshold=stop_loss_threshold,
        )
        if not 0 <= exit_threshold < entry_threshold < stop_loss_threshold:
            raise PhantomDomainError("Thresholds must satisfy 0 <= exit < entry < stop_loss.")

        abs_z = abs(current_z_score)
        if abs_z >= stop_loss_threshold:
            signal = CointegrationSignal.STOP_LOSS_LIQUIDATE
        elif current_z_score > entry_threshold:
            signal = CointegrationSignal.SELL_SPREAD
        elif current_z_score < -entry_threshold:
            signal = CointegrationSignal.BUY_SPREAD
        elif abs_z <= exit_threshold:
            signal = CointegrationSignal.CLOSE_POSITION
        else:
            signal = CointegrationSignal.HOLD

        result = {
            "signal": signal.value,
            "abs_z_score": abs_z,
            "distance_to_stop_loss": stop_loss_threshold - abs_z,
        }
        log = await self._log_calculation(
            db,
            CalcType.COINTEGRATION,
            {
                "current_z_score": current_z_score,
                "entry_threshold": entry_threshold,
                "exit_threshold": exit_threshold,
                "stop_loss_threshold": stop_loss_threshold,
            },
            result,
        )
        log_fn = logger.warning if signal is CointegrationSignal.STOP_LOSS_LIQUIDATE else logger.info
        log_fn("[GARUDA]: cointegration z=%.4f -> %s.", current_z_score, signal.value)
        return {"result": result, "log": log}
