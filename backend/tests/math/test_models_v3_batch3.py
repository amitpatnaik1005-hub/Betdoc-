"""Pytest suite for Smallcase Engine v3, Batch 3 (Models 51-59)."""

import numpy as np
import pytest

from app.domain.math.models_v2.base import NumericalStabilityError
from app.domain.math.models_v3 import (
    BlackScholesConfig,
    BlackScholesModel,
    DynamicTimeWarpingConfig,
    DynamicTimeWarpingModel,
    ExtremeValueConfig,
    ExtremeValueModel,
    GaussianCopulaConfig,
    GaussianCopulaModel,
    GradientBoostingRegressionConfig,
    GradientBoostingRegressionModel,
    HSMMConfig,
    HSMMModel,
    KernelDensityConfig,
    KernelDensityModel,
    LassoRidgeConfig,
    LassoRidgeModel,
    SimplexOptimizationConfig,
    SimplexOptimizationModel,
)


def test_lasso_ridge_model():
    X = np.random.RandomState(42).randn(50, 3)
    y = X @ np.array([1.5, -2.0, 0.0]) + np.random.RandomState(43).randn(50) * 0.1

    # Test Ridge
    cfg_ridge = LassoRidgeConfig(penalty="l2", alpha=1.0)
    model_ridge = LassoRidgeModel(cfg_ridge)
    model_ridge.fit(X, y)
    y_pred_ridge = model_ridge.predict(X)
    assert y_pred_ridge.shape == (50,)
    assert model_ridge.coefficients.shape == (3,)

    # Test Lasso
    cfg_lasso = LassoRidgeConfig(penalty="l1", alpha=0.5)
    model_lasso = LassoRidgeModel(cfg_lasso)
    model_lasso.fit(X, y)
    y_pred_lasso = model_lasso.predict(X)
    assert y_pred_lasso.shape == (50,)
    assert model_lasso.coefficients.shape == (3,)
    # L1 should ideally shrink the 3rd coefficient to 0
    assert abs(model_lasso.coefficients[2]) < 0.2


def test_gradient_boosting_model():
    X = np.random.RandomState(42).randn(100, 4)
    y = np.sin(X[:, 0]) + X[:, 1]**2
    
    cfg = GradientBoostingRegressionConfig(n_estimators=10, max_depth=2, rng_stream=42)
    model = GradientBoostingRegressionModel(cfg)
    model.fit(X, y)
    
    preds = model.predict(X)
    assert preds.shape == (100,)
    assert model.feature_importances.shape == (4,)
    
    # Check deterministic randomness via rng_stream
    model2 = GradientBoostingRegressionModel(cfg)
    model2.fit(X, y)
    assert np.allclose(model.predict(X), model2.predict(X))


def test_kernel_density_model():
    # 2D data
    X = np.random.RandomState(42).randn(100, 2)
    cfg = KernelDensityConfig(bandwidth="auto")
    model = KernelDensityModel(cfg)
    model.fit(X)
    
    densities = model.predict(X)
    assert densities.shape == (100,)
    assert np.all(densities >= 0)
    assert model.bandwidth > 0
    
    log_densities = model.log_density(X)
    assert log_densities.shape == (100,)
    assert np.allclose(np.exp(log_densities), densities)


def test_black_scholes_model():
    # X = [S, K, T, r, sigma, q]
    X = np.array([
        [100.0, 100.0, 1.0, 0.05, 0.2, 0.0],
        [100.0, 120.0, 0.5, 0.05, 0.2, 0.02],
        [100.0, 90.0, 1e-10, 0.05, 0.2, 0.0]  # Limit case
    ])
    
    # Call
    cfg_call = BlackScholesConfig(option_type="call")
    model_call = BlackScholesModel(cfg_call)
    res_call = model_call.predict(X)
    assert res_call.shape == (3, 6)
    
    # Put
    cfg_put = BlackScholesConfig(option_type="put")
    model_put = BlackScholesModel(cfg_put)
    res_put = model_put.predict(X)
    assert res_put.shape == (3, 6)
    
    # Limit case Call Intrinsic (S - K)
    assert np.isclose(res_call[2, 0], 10.0)  # 100 - 90 = 10
    assert np.isclose(res_call[2, 2], 0.0)   # Gamma = 0 in limit


def test_gaussian_copula_model():
    # Construct correlated uniforms
    X = np.random.RandomState(42).multivariate_normal([0, 0], [[1, 0.8], [0.8, 1]], size=100)
    
    cfg = GaussianCopulaConfig()
    model = GaussianCopulaModel(cfg)
    model.fit(X)
    
    assert model.correlation.shape == (2, 2)
    assert np.isclose(model.correlation[0, 0], 1.0)
    assert model.correlation[0, 1] > 0.5  # Should recover positive correlation
    
    u = np.array([[0.1, 0.1], [0.5, 0.5], [0.9, 0.9]])
    joint_probs = model.predict(u)
    assert joint_probs.shape == (3,)
    assert np.all((joint_probs >= 0) & (joint_probs <= 1))


def test_extreme_value_model():
    X = np.random.RandomState(42).standard_t(df=3, size=1000)
    
    # Upper tail
    cfg = ExtremeValueConfig(threshold_quantile=0.90, tail="upper")
    model = ExtremeValueModel(cfg)
    model.fit(X)
    
    assert model.parameters["exceedance_rate"] == 0.1
    assert model.parameters["scale"] > 0
    
    preds = model.predict(np.array([model.parameters["threshold"] + 1.0]))
    assert preds.shape == (1,)
    assert 0 <= preds[0] <= 1
    
    var_99 = model.value_at_risk(0.01)
    assert var_99 > model.parameters["threshold"]


def test_dynamic_time_warping_model():
    template = np.array([1, 2, 3, 4, 5, np.nan, np.nan])
    queries = np.array([
        [1, 2.1, 3.1, 4, 5.1, np.nan, np.nan],
        [1, 1, 2, 3, 4, 5, np.nan],
        [5, 4, 3, 2, 1, np.nan, np.nan]
    ])
    
    cfg = DynamicTimeWarpingConfig(window=2, local_cost="absolute")
    model = DynamicTimeWarpingModel(cfg)
    model.fit(template)  # template is automatically extracted (ignoring NaNs)
    
    dist = model.predict(queries)
    assert dist.shape == (3, 1)
    assert dist[0, 0] < dist[2, 0]  # Query 1 is closer to template than Query 3


def test_hsmm_model():
    # K=2 states
    cfg = HSMMConfig(
        transition_matrix=((0.0, 1.0), (1.0, 0.0)),
        duration_lambdas=(2.0, 3.0),
        emission_means=(0.0, 5.0),
        emission_variances=(1.0, 1.0),
        max_duration=10
    )
    model = HSMMModel(cfg)
    # Fit is dummy, state is built on init
    
    seqs = np.array([
        [0.1, -0.2, 0.0, 4.9, 5.1, 4.8],  # State 0 for 3, State 1 for 3
        [5.0, 5.1, 5.2, 0.1, 0.0, -0.1]   # State 1 for 3, State 0 for 3
    ])
    
    log_likelihoods = model.predict(seqs)
    assert log_likelihoods.shape == (2,)
    assert np.all(np.isfinite(log_likelihoods))


def test_simplex_optimization_model():
    # Maximize c^T x
    # c = [1.0, 2.0]
    # x1 + x2 <= 1.0 (handled by budget if enforce_budget=True)
    cfg = SimplexOptimizationConfig(
        min_alloc=0.0, max_alloc=1.0, enforce_budget=True, budget=1.0
    )
    model = SimplexOptimizationModel(cfg)
    
    c = np.array([1.0, 2.0])
    model.fit(c)
    
    # Optimal should be x = [0.0, 1.0] since 2.0 > 1.0
    assert np.allclose(model.allocation, [0.0, 1.0], atol=1e-5)
    assert np.isclose(model.objective, 2.0, atol=1e-5)
    
    scenarios = np.array([[1.0, 2.0], [3.0, -1.0]])
    returns = model.predict(scenarios)
    assert np.allclose(returns, [2.0, -1.0], atol=1e-5)
