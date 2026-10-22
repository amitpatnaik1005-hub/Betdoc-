"""QuantToolkitManager: The Lab's (PANINI) parameter-driven quant toolkit: dutching, matched betting,
Avellaneda-Stoikov market making and cointegration signals. Every run is logged to ``phantom_calculations``.
Group 96 retired arbitrage detection and the Phantom section; the engines moved to /lab/quant-toolkit.

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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.quant_toolkit.errors import QuantToolkitError
from app.models.phantom import CalcType, PhantomCalculationLogModel

logger = logging.getLogger("betdoc.lab.quant")

MONEY_DECIMALS = 2
JSON_COLUMN_MAX_LENGTH = 2048


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
            raise QuantToolkitError(f"{name} must be a finite number.", field=name)


def _require_finite_list(name: str, values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)):
        raise QuantToolkitError(f"{name} must be a list of numbers.", field=name)
    for index, value in enumerate(values):
        _require_finite(**{f"{name}[{index}]": value})
    return [float(v) for v in values]


def _require_decimal_odds(name: str, values: Sequence[float]) -> None:
    for index, value in enumerate(values):
        if value <= 1.0:
            raise QuantToolkitError(f"{name}[{index}] must be decimal odds greater than 1.", field=name)


def _require_commission_pct(name: str, value: float) -> None:
    if not 0.0 <= value < 100.0:
        raise QuantToolkitError(f"{name} must be in the range [0, 100).", field=name)


def _require_finite_outputs(**values: float) -> None:
    for name, value in values.items():
        if not math.isfinite(value):
            raise QuantToolkitError(f"{name} overflowed to a non-finite value; inputs are out of range.", field=name)


def _money(value: float) -> float:
    return round(value, MONEY_DECIMALS) + 0.0  # + 0.0 normalises -0.0


def _dumps(payload: Any, label: str) -> str:
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise QuantToolkitError(f"{label} is not JSON-serialisable.") from exc
    if len(encoded) > JSON_COLUMN_MAX_LENGTH:
        raise QuantToolkitError(f"{label} exceeds {JSON_COLUMN_MAX_LENGTH} characters.")
    return encoded


# --------------------------------------------------------------------------- manager


class QuantToolkitManager:
    async def _persist(self, db: AsyncSession, instance: Any, label: str) -> Any:
        db.add(instance)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            logger.warning("[PANINI]: %s rejected by storage constraints.", label)
            raise QuantToolkitError(f"{label} rejected by storage constraints.") from exc
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

    # ------------------------------------------------------------------ 1. dutching

    async def calculate_dutching(
        self, db: AsyncSession, target_total_stake: float, odds: Sequence[float]
    ) -> dict[str, Any]:
        odds_list = _require_finite_list("odds", odds)
        if len(odds_list) < 2:
            raise QuantToolkitError("odds must contain at least two selections.", field="odds")
        _require_decimal_odds("odds", odds_list)
        _require_finite(target_total_stake=target_total_stake)
        if target_total_stake <= 0:
            raise QuantToolkitError("target_total_stake must be greater than zero.")

        implied_probs = [1.0 / o for o in odds_list]
        total_implied = sum(implied_probs)
        if total_implied <= 0:
            raise QuantToolkitError("Total implied probability must be positive.")
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
        logger.info("[PANINI]: dutched %d selections -> return %.2f.", len(odds_list), guaranteed_return)
        return {"result": result, "log": log}

    # ------------------------------------------------------------------ 2. matched betting

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
            raise QuantToolkitError(f"Unsupported matched betting mode {mode!r}.") from exc
        _require_finite(
            back_stake=back_stake, back_odds=back_odds, lay_odds=lay_odds, lay_commission_pct=lay_commission_pct
        )
        if back_stake <= 0:
            raise QuantToolkitError("back_stake must be greater than zero.")
        if back_odds <= 1.0:
            raise QuantToolkitError("back_odds must be decimal odds greater than 1.")
        if lay_odds == 1.0:
            raise QuantToolkitError("lay_odds of 1.0 causes division by zero.")
        if lay_odds < 1.0:
            raise QuantToolkitError("lay_odds must be decimal odds greater than 1.")
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
            raise QuantToolkitError(f"{resolved_mode.value} lay stake denominator is not positive.")

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
            "[PANINI]: %s matched bet -> lay %.2f (back win %.2f / lay win %.2f).",
            resolved_mode.value,
            lay_stake,
            back_win_profit,
            lay_win_profit,
        )
        return {"result": result, "log": log}

    # ------------------------------------------------------------------ 3. Avellaneda-Stoikov

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
            raise QuantToolkitError("gamma of 0 causes division by zero.")
        if gamma < 0:
            raise QuantToolkitError("gamma (risk aversion) must be greater than zero.")
        if liquidity_k == 0:
            raise QuantToolkitError("liquidity_k of 0 causes division by zero.")
        if liquidity_k < 0:
            raise QuantToolkitError("liquidity_k must be greater than zero.")
        if volatility_sigma < 0:
            raise QuantToolkitError("volatility_sigma must not be negative.")

        try:
            dt = max(0.0, time_horizon_t - current_time_t)
            variance_term = gamma * (volatility_sigma**2) * dt
            reservation_price = mid_price - (inventory * variance_term)
            spread = variance_term + ((2.0 / gamma) * math.log1p(gamma / liquidity_k))
        except (OverflowError, ZeroDivisionError, ValueError) as exc:
            raise QuantToolkitError("Market-maker inputs overflow the model.") from exc
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
            "[PANINI]: A-S quotes bid %.2f / ask %.2f (reservation %.6f, spread %.6f).",
            optimal_bid,
            optimal_ask,
            reservation_price,
            spread,
        )
        return {"result": result, "log": log}

    # ------------------------------------------------------------------ 4. cointegration

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
            raise QuantToolkitError("Thresholds must satisfy 0 <= exit < entry < stop_loss.")

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
        log_fn("[PANINI]: cointegration z=%.4f -> %s.", current_z_score, signal.value)
        return {"result": result, "log": log}

    # ------------------------------------------------------------------ history

    async def list_calculations(self, db: AsyncSession, calc_type: str | None = None, limit: int = 25) -> list[PhantomCalculationLogModel]:
        """Newest runs first, optionally one calculator's only."""
        stmt = select(PhantomCalculationLogModel).order_by(PhantomCalculationLogModel.created_at.desc()).limit(limit)
        if calc_type:
            try:
                kind = CalcType(calc_type.strip().upper())
            except ValueError as exc:
                raise QuantToolkitError(f"calc_type must be one of {', '.join(m.value for m in CalcType)}.", field="calc_type") from exc
            stmt = stmt.where(PhantomCalculationLogModel.calc_type == kind.value)
        return list((await db.execute(stmt)).scalars().all())
