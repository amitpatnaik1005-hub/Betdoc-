import math
import numpy as np
import pytest

from app.domain.risk import (
    exponential_spectral_risk,
    risk_parity_weights,
    calculate_hhi,
    eigen_dispersion,
    liquidity_adjusted_var,
    expected_shortfall_counterparty,
    lda_percentile,
    edge_decay_penalty,
    clayton_lower_tail_dependence,
    gumbel_upper_tail_dependence,
    stress_test_portfolio,
    implied_ruin_volatility,
    entropic_risk_measure,
    component_var,
)


def test_exponential_spectral_risk():
    returns = np.array([0.1, -0.05, -0.2, 0.05])
    # Empty case
    assert exponential_spectral_risk([]) == 0.0
    # Normal case
    risk = exponential_spectral_risk(returns, gamma=2.0)
    assert isinstance(risk, float)
    assert risk > 0.0  # Since there's a big loss (-0.2), risk should be positive
    # Invalid gamma
    assert exponential_spectral_risk(returns, gamma=-1.0) == 0.0


def test_risk_parity_weights():
    # 2 assets, A is high variance, B is low variance
    cov = np.array([[0.04, 0.0], [0.0, 0.01]])
    
    w = risk_parity_weights(cov)
    assert w.shape == (2,)
    assert np.isclose(w.sum(), 1.0)
    # Asset B (low var) should have a higher weight than Asset A
    assert w[1] > w[0]
    
    # Invalid cov
    assert np.all(risk_parity_weights(np.array([[1.0]])) == np.array([1.0]))
    assert risk_parity_weights([]).shape == (0,)


def test_calculate_hhi():
    assert calculate_hhi([]) == 0.0
    assert calculate_hhi([0.0, 0.0]) == 0.0
    
    w1 = [0.5, 0.5]
    assert np.isclose(calculate_hhi(w1), 0.5)
    
    w2 = [1.0, 0.0]
    assert np.isclose(calculate_hhi(w2), 1.0)
    
    # Normalisation check (if inputs don't sum to 1)
    w3 = [10.0, 10.0]
    assert np.isclose(calculate_hhi(w3), 0.5)


def test_eigen_dispersion():
    # Singular return matrix
    assert eigen_dispersion([]) == 0.0
    
    # Highly correlated returns
    returns = np.array([
        [0.01, 0.011],
        [-0.02, -0.022],
        [0.03, 0.029],
        [-0.01, -0.011]
    ])
    d = eigen_dispersion(returns)
    assert 0.5 < d <= 1.0
    
    # Flat return matrix (var = 0)
    flat_returns = np.zeros((10, 2))
    assert eigen_dispersion(flat_returns) == 0.0


def test_liquidity_adjusted_var():
    assert liquidity_adjusted_var(0.0, 100, 0.01) == 0.0
    
    var = 1000.0
    lvar = liquidity_adjusted_var(var, position_size=500, bid_ask_spread=0.05, daily_volume=1000, volume_penalty_c=1.0)
    assert lvar > var
    assert lvar <= var * 10.0


def test_expected_shortfall_counterparty():
    exposures = [1000.0, -500.0, 2000.0]  # Middle is negative exposure
    pd = [0.05, 0.1, 0.02]
    lgd = [0.5, 0.4, 0.8]
    
    el = expected_shortfall_counterparty(exposures, pd, lgd)
    # Expected: (1000 * 0.05 * 0.5) + 0 (floored) + (2000 * 0.02 * 0.8)
    # = 25.0 + 0.0 + 32.0 = 57.0
    assert np.isclose(el, 57.0)
    
    assert expected_shortfall_counterparty([], [], []) == 0.0


def test_lda_percentile():
    # Normal case
    loss = lda_percentile(lambda_freq=10.0, lognorm_mu=1.0, lognorm_sigma=0.5, n_sims=100)
    assert isinstance(loss, float)
    assert loss >= 0.0
    
    # Invalid params
    assert lda_percentile(-1.0, 1.0, 0.5) == 0.0


def test_edge_decay_penalty():
    assert edge_decay_penalty(0, 30) == 1.0
    assert edge_decay_penalty(30, 30) == 0.5
    assert edge_decay_penalty(60, 30) == 0.25
    assert edge_decay_penalty(-10, 30) == 1.0  # max(t, 0) floors it to 0
    
    # Invalid half life
    assert edge_decay_penalty(0, 0) == 1.0
    assert edge_decay_penalty(10, 0) == 0.0


def test_copulas():
    assert clayton_lower_tail_dependence(0.5) == 0.25
    assert clayton_lower_tail_dependence(0.0) == 0.0
    
    assert gumbel_upper_tail_dependence(2.0) == 2.0 - 2.0**(0.5)
    assert gumbel_upper_tail_dependence(0.5) == 0.0


def test_stress_test_portfolio():
    weights = [0.5, 0.5]
    shocks = np.array([
        [-0.1, -0.2],
        [0.05, -0.05]
    ])
    
    pnl = stress_test_portfolio(weights, shocks)
    assert pnl.shape == (2,)
    assert np.isclose(pnl[0], -0.15)
    assert np.isclose(pnl[1], 0.0)
    
    # Dimension mismatch
    assert stress_test_portfolio([0.5], shocks).shape == (2,)
    assert np.all(stress_test_portfolio([0.5], shocks) == 0.0)


def test_implied_ruin_volatility():
    # v0 = 100, ruin = 80, T = 1, conf = 97.5% (Z approx 1.96)
    sigma = implied_ruin_volatility(100.0, 80.0, 1.0, confidence=0.975)
    assert sigma > 0.0
    
    # Ruin > V0 (already ruined)
    assert implied_ruin_volatility(100.0, 120.0, 1.0) == 0.0
    # V0 <= 0
    assert implied_ruin_volatility(-10.0, -20.0, 1.0) == 0.0


def test_entropic_risk_measure():
    returns = np.array([0.1, 0.2, -0.1])
    # theta > 0
    rho = entropic_risk_measure(returns, theta=1.0)
    assert isinstance(rho, float)
    
    # theta <= 0
    assert entropic_risk_measure(returns, theta=-1.0) == 0.0
    # Empty
    assert entropic_risk_measure([]) == 0.0


def test_component_var():
    w = np.array([0.6, 0.4])
    cov = np.array([[0.04, 0.01], [0.01, 0.02]])
    
    cvar = component_var(w, cov)
    assert cvar.shape == (2,)
    assert cvar[0] > 0 and cvar[1] > 0
    
    # Invalid confidence
    assert np.all(component_var(w, cov, confidence=1.5) == 0.0)
    # Empty
    assert component_var([], cov).shape == (0,)
