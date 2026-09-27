from app.domain.risk.common import safe_float
from app.schemas.oracle import DynamicStrategyParams, OracleStrategyType
from app.schemas.risk import RiskMetrics

MIN_KELLY_MULTIPLIER = 0.1
BALANCED_THRESHOLD = 0.25
PRESERVATION_THRESHOLD = 0.75
RECOVERY_THRESHOLD = 0.95


def _strategy_for(temperature: float) -> OracleStrategyType:
    if temperature < BALANCED_THRESHOLD:
        return OracleStrategyType.AGGRESSIVE_GROWTH
    if temperature < PRESERVATION_THRESHOLD:
        return OracleStrategyType.BALANCED
    if temperature < RECOVERY_THRESHOLD:
        return OracleStrategyType.CAPITAL_PRESERVATION
    return OracleStrategyType.RECOVERY


def calculate_risk_temperature(metrics: RiskMetrics, params: DynamicStrategyParams) -> dict:
    """Pure function. 0.0 = cold (no drawdown), 1.0 = at/over the drawdown ceiling."""
    max_dd = safe_float(params.max_allowed_drawdown)
    current_dd = max(0.0, safe_float(metrics.current_drawdown))

    # Fail closed: an invalid ceiling is treated as maximum heat.
    drawdown_ratio = current_dd / max_dd if max_dd > 0.0 else 1.0
    temperature = max(0.0, min(1.0, safe_float(drawdown_ratio)))
    kelly_multiplier = max(MIN_KELLY_MULTIPLIER, 1.0 - temperature)

    return {
        "temperature": float(temperature),
        "kelly_multiplier": float(kelly_multiplier),
        "strategy": _strategy_for(temperature),
    }
