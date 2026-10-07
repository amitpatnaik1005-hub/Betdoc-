import numpy as np
import pytest

from app.domain.math.models_v2 import math_model_registry
# Ensure v3 is imported so it registers models
import app.domain.math.models_v3
from app.domain.math.models_v3.garch import GARCHModel, GARCHConfig
from app.domain.math.models_v3.adaboost import AdaBoostModel, AdaBoostConfig
from app.domain.math.models_v3.kalman_filter import KalmanFilterModel, KalmanFilterConfig
from app.domain.math.models_v3.particle_filter import ParticleFilterModel, ParticleFilterConfig
from app.domain.math.models_v3.nash_equilibrium import NashEquilibriumModel, NashEquilibriumConfig
from app.domain.math.models_v3.sde_jump_diffusion import JumpDiffusionModel, JumpDiffusionConfig


def test_v3_models_registered():
    expected_models = [
        "GaussianProcess", "ElasticNet", "AdaBoost", "HMM",
        "GARCH", "KalmanFilter", "ParticleFilter", "NashEquilibrium",
        "QueueingTheory", "JumpDiffusion"
    ]
    for name in expected_models:
        assert math_model_registry.get(name) is not None, f"Missing {name} in registry."


def test_garch_fit():
    # Use config with high floor to prevent failure on tiny dummy data
    cfg = GARCHConfig(min_observations=10, max_iter=20)
    model = GARCHModel(cfg)
    
    # Generate some heteroskedastic dummy log returns
    np.random.seed(42)
    returns = np.random.normal(0, 0.01, 100)
    model.fit(returns)
    assert model.is_fitted
    
    # Predict 5 steps ahead
    forecast = model.predict(returns, steps=5)
    assert forecast.shape == (5,)


def test_kalman_filter():
    cfg = KalmanFilterConfig(min_observations=5)
    model = KalmanFilterModel(cfg)
    
    # Dummy observations
    z = np.array([1.0, 1.1, 1.05, 1.2, 1.15, 1.3])
    model.fit(z)
    assert model.is_fitted
    
    means = model.predict(z)
    assert means.shape == (6, 1)


def test_nash_equilibrium():
    cfg = NashEquilibriumConfig()
    model = NashEquilibriumModel(cfg)
    
    # Rock Paper Scissors payoff matrix (0 sum)
    # R  P  S
    # R [ 0 -1  1]
    # P [ 1  0 -1]
    # S [-1  1  0]
    payoff = np.array([
        [0, -1, 1],
        [1, 0, -1],
        [-1, 1, 0]
    ], dtype=np.float64)
    
    strategy = model.predict(payoff)
    assert strategy.shape == (3,)
    assert np.allclose(strategy, [1/3, 1/3, 1/3], atol=1e-2)


def test_sde_jump_diffusion():
    cfg = JumpDiffusionConfig(n_steps=10, n_paths=2)
    model = JumpDiffusionModel(cfg)
    
    # Un-fitted predict should work since params are in config
    initial_prices = np.array([100.0, 150.0])
    paths = model.predict(initial_prices)
    # Shape: (n_assets, n_paths, n_steps) -> (2, 2, 10)
    assert paths.shape == (2, 2, 10)
    assert np.all(paths > 0)
