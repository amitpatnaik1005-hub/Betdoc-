"""KUMBHA Risk Engine."""
from app.domain.risk.common import (
    ACTIVE_STATUSES,
    SETTLED_STATUSES,
    bet_profit,
    safe_float,
    to_finite_array,
)
from app.domain.risk.var import calculate_historical_var, calculate_parametric_var
from app.domain.risk.cvar import calculate_cvar
from app.domain.risk.drawdown import calculate_current_drawdown, calculate_max_drawdown
from app.domain.risk.ratios import calmar_ratio, omega_ratio, sharpe_ratio, sortino_ratio
from app.domain.risk.kelly_portfolio import portfolio_kelly
from app.domain.risk.exposure import calculate_exposure
from app.domain.risk.evt import evt_tail_risk
from app.domain.risk.mpt import optimize_portfolio
from app.domain.risk.black_litterman import black_litterman_adjust
from app.domain.risk.stop_loss import StopLossEngine

# Group 55: advanced risk models
from app.domain.risk.spectral import exponential_spectral_risk
from app.domain.risk.parity import risk_parity_weights
from app.domain.risk.concentration import calculate_hhi
from app.domain.risk.correlation_risk import eigen_dispersion
from app.domain.risk.liquidity_risk import liquidity_adjusted_var
from app.domain.risk.credit_risk import expected_shortfall_counterparty
from app.domain.risk.operational_risk import lda_percentile
from app.domain.risk.model_risk import edge_decay_penalty
from app.domain.risk.copula_clayton import clayton_lower_tail_dependence
from app.domain.risk.copula_gumbel import gumbel_upper_tail_dependence
from app.domain.risk.scenario_matrix import stress_test_portfolio
from app.domain.risk.reverse_stress import implied_ruin_volatility
from app.domain.risk.entropic_risk import entropic_risk_measure
from app.domain.risk.component_var import component_var

__all__ = [
    "ACTIVE_STATUSES",
    "SETTLED_STATUSES",
    "bet_profit",
    "safe_float",
    "to_finite_array",
    "calculate_parametric_var",
    "calculate_historical_var",
    "calculate_cvar",
    "calculate_max_drawdown",
    "calculate_current_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "omega_ratio",
    "portfolio_kelly",
    "calculate_exposure",
    "evt_tail_risk",
    "optimize_portfolio",
    "black_litterman_adjust",
    "StopLossEngine",
    "exponential_spectral_risk",
    "risk_parity_weights",
    "calculate_hhi",
    "eigen_dispersion",
    "liquidity_adjusted_var",
    "expected_shortfall_counterparty",
    "lda_percentile",
    "edge_decay_penalty",
    "clayton_lower_tail_dependence",
    "gumbel_upper_tail_dependence",
    "stress_test_portfolio",
    "implied_ruin_volatility",
    "entropic_risk_measure",
    "component_var",
]
