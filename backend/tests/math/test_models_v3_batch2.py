import numpy as np
import pytest

from app.domain.math.models_v2.base import BaseMathModel
from app.domain.math.models_v3 import math_model_registry
from app.domain.math.models_v3.lstm_numpy import LSTMConfig
from app.domain.math.models_v3.transformer_attention import TransformerAttentionConfig
from app.domain.math.models_v3.survival_analysis import CoxPHConfig
from app.domain.math.models_v3.ornstein_uhlenbeck import OrnsteinUhlenbeckConfig
from app.domain.math.models_v3.var_model import VARConfig
from app.domain.math.models_v3.shapley_values import ShapleyConfig
from app.domain.math.models_v3.fractional_brownian_motion import FractionalBrownianMotionConfig
from app.domain.math.models_v3.quadratic_programming import MarkowitzConfig
from app.domain.math.models_v3.dirichlet_process import DirichletProcessConfig
from app.domain.math.models_v3.hawkes_process import HawkesConfig

def test_registry_contains_batch_2():
    batch2_keys = [
        "LSTM", "TransformerAttention", "SurvivalAnalysis", "OrnsteinUhlenbeck",
        "VAR", "ShapleyValues", "FractionalBrownianMotion", "QuadraticProgramming",
        "DirichletProcess", "HawkesProcess"
    ]
    for key in batch2_keys:
        assert math_model_registry.get(key) is not None, f"{key} missing from registry"


def test_lstm_model():
    ModelClass = math_model_registry.get("LSTM")
    # Config has default hidden_size=32, batch_size=32, n_epochs=100
    model = ModelClass(LSTMConfig(n_epochs=2, batch_size=4))
    rng = np.random.default_rng(42)
    X = rng.normal(0, 1, (10, 5, 3)) # (batch, time, features)
    y = rng.normal(0, 1, (10, 2)) # (batch, targets)
    model.fit(X, y)
    y_pred = model.predict(X)
    assert y_pred.shape == (10, 2)
    # Check that predictions are reproducible with same state
    y_pred2 = model.predict(X)
    np.testing.assert_allclose(y_pred, y_pred2)


def test_transformer_attention():
    ModelClass = math_model_registry.get("TransformerAttention")
    # Need d_model % num_heads == 0. Defaults: d_model=32, num_heads=4
    model = ModelClass(TransformerAttentionConfig(d_model=16, num_heads=4))
    rng = np.random.default_rng(42)
    X = rng.normal(0, 1, (8, 10, 16)) # (batch, time, d_model)
    # fit without y to initialize weights
    model.fit(X)
    # encode only
    enc = model.encode(X)
    assert enc.shape == (8, 10, 16)
    # fit with y
    y = rng.normal(0, 1, (8, 1))
    model.fit(X, y)
    y_pred = model.predict(X)
    assert y_pred.shape == (8, 1)


def test_survival_analysis():
    ModelClass = math_model_registry.get("SurvivalAnalysis")
    model = ModelClass(CoxPHConfig(max_iter=5))
    rng = np.random.default_rng(42)
    X = rng.normal(0, 1, (20, 3))
    time = rng.uniform(0.1, 10.0, 20)
    event = rng.integers(0, 2, 20)
    # Ensure at least one event
    event[0] = 1
    y = np.column_stack([time, event])
    model.fit(X, y)
    y_pred = model.predict(X)
    assert y_pred.shape == (20,)
    assert np.all(y_pred >= 0)


def test_ornstein_uhlenbeck():
    ModelClass = math_model_registry.get("OrnsteinUhlenbeck")
    model = ModelClass(OrnsteinUhlenbeckConfig(n_steps=10, n_paths=5, forecast_horizon=3, refine_mle=False))
    rng = np.random.default_rng(42)
    X = rng.normal(5, 1, 50)
    model.fit(X)
    preds = model.predict(X, horizon=3)
    assert preds.shape == (3,)
    sim = model.simulate(X, n_paths=5, n_steps=10)
    assert sim.shape == (50, 5, 10)


def test_var_model():
    ModelClass = math_model_registry.get("VAR")
    model = ModelClass(VARConfig(lags=2, forecast_horizon=4))
    rng = np.random.default_rng(42)
    X = np.zeros((50, 2))
    for t in range(2, 50):
        X[t] = 0.5 * X[t-1] - 0.2 * X[t-2] + rng.normal(0, 0.1, 2)
    model.fit(X)
    preds = model.predict(X)
    assert preds.shape == (4, 2)


def test_shapley_values():
    ModelClass = math_model_registry.get("ShapleyValues")
    model = ModelClass(ShapleyConfig(method="exact", exact_max_players=4))
    rng = np.random.default_rng(42)
    X = rng.normal(0, 1, (30, 3))
    y = 0.5 * X[:, 0] + 0.3 * X[:, 1] + rng.normal(0, 0.1, 30)
    model.fit(X, y)
    y_pred = model.predict(X)
    assert y_pred.shape == (30,)
    assert model.shapley_values.shape == (3,)


def test_fractional_brownian_motion():
    ModelClass = math_model_registry.get("FractionalBrownianMotion")
    model = ModelClass(FractionalBrownianMotionConfig(n_steps=20, n_paths=10, estimate_parameters=True, max_variogram_lag=5))
    rng = np.random.default_rng(42)
    X = np.cumsum(rng.normal(0, 1, 50))
    model.fit(X)
    assert 0 < model.hurst < 1
    paths = model.predict(X)
    assert paths.shape == (50, 10, 20)


def test_markowitz():
    ModelClass = math_model_registry.get("QuadraticProgramming")
    model = ModelClass(MarkowitzConfig(risk_aversion=2.0, max_weight=1.0))
    rng = np.random.default_rng(42)
    returns = rng.normal(0.01, 0.05, (100, 4))
    model.fit(returns)
    w = model.weights
    assert w.shape == (4,)
    np.testing.assert_allclose(w.sum(), 1.0, atol=1e-5)
    preds = model.predict(returns)
    assert preds.shape == (100,)


def test_dirichlet_process():
    ModelClass = math_model_registry.get("DirichletProcess")
    model = ModelClass(DirichletProcessConfig(n_iter=5, burn_in=1, concentration=1.5))
    rng = np.random.default_rng(42)
    X = np.vstack([rng.normal(0, 1, (20, 2)), rng.normal(5, 1, (20, 2))])
    model.fit(X)
    labels = model.predict(X)
    assert labels.shape == (40,)
    assert model.n_clusters >= 1


def test_hawkes_process():
    ModelClass = math_model_registry.get("HawkesProcess")
    model = ModelClass(HawkesConfig(max_iter=10))
    # Generate some sorted event times
    events = np.cumsum(np.random.default_rng(42).exponential(0.5, 30))
    model.fit(events)
    query = np.linspace(0, events[-1] + 1, 10)
    intensities = model.predict(query)
    assert intensities.shape == (10,)
    sim = model.simulate(horizon=5.0)
    # simulate returns an array of times
    assert sim.ndim == 1
    assert len(sim) >= 0

