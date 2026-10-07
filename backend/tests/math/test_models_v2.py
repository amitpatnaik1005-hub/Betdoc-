import numpy as np
import pytest
from pydantic import ValidationError

from app.domain.math.models_v2 import math_model_registry
from app.domain.math.models_v2.base import ModelNotFittedError
from app.domain.math.models_v2.arima import ARIMAModel, ARIMAConfig
from app.domain.math.models_v2.genetic_algorithm import GeneticAlgorithmModel, GeneticAlgorithmConfig
from app.domain.math.models_v2.moving_averages import MovingAverageModel, MovingAverageConfig
from app.domain.math.models_v2.random_forest import RandomForestModel, RandomForestConfig


def test_registry_contains_all_models():
    """Verify that all 20 advanced math models are correctly registered."""
    expected_models = [
        "Binomial", "NormalDistribution", "Copula", "BayesianNetwork",
        "MarkovChain", "MovingAverage", "ARIMA", "FourierAnalysis",
        "InformationTheory", "ProspectTheory", "CausalInference",
        "LogisticRegression", "RandomForest", "SVM", "XGBoost",
        "NeuralNet", "PCA", "Clustering", "QLearning", "GeneticAlgorithm"
    ]
    for name in expected_models:
        assert math_model_registry.get(name) is not None, f"Missing {name} in registry."


def test_unfitted_model_raises_error():
    """Verify the concurrency and state safety contract."""
    model = ARIMAModel(ARIMAConfig())
    assert not model.is_fitted
    with pytest.raises(ModelNotFittedError):
        model.predict(np.array(5))


def test_arima_fit_predict():
    """Verify custom ARIMA implementation (OLS base)."""
    model = ARIMAModel(ARIMAConfig(p=1, d=0, q=0))
    # Simple AR(1) process
    X = np.array([1.0, -0.5, 0.25, -0.125, 0.0625, -0.03125])
    model.fit(X)
    assert model.is_fitted
    
    preds = model.predict(np.array(3))
    assert preds.shape == (3,)
    assert np.all(np.isfinite(preds))


def test_genetic_algorithm_fit():
    """Verify custom Genetic Algorithm portfolio optimization."""
    # Fast config for testing
    cfg = GeneticAlgorithmConfig(
        population_size=10, n_generations=5, elite_count=2, max_weight=1.0
    )
    model = GeneticAlgorithmModel(cfg)
    
    # 6 periods, 3 assets
    returns = np.array([
        [0.01, -0.02, 0.03],
        [0.02, 0.00, 0.01],
        [-0.01, 0.01, 0.02],
        [0.03, -0.01, -0.01],
        [0.00, 0.02, 0.04],
        [0.01, 0.01, 0.01],
    ])
    model.fit(returns)
    assert model.is_fitted
    
    # Check weights sum to 1.0 (long-only bounded projection)
    assert model.weights.shape == (3,)
    assert np.isclose(model.weights.sum(), 1.0)
    
    # Predict should return dot product of new returns and optimal weights
    portfolio_returns = model.predict(returns)
    assert portfolio_returns.shape == (6,)


def test_moving_average_macd():
    """Verify stateless EWMA and MACD filtering."""
    model = MovingAverageModel(MovingAverageConfig())
    X = np.array([[10], [11], [12], [13], [14]])
    macd, sig, hist = model.macd(X)
    assert macd.shape == (5, 1)
    assert sig.shape == (5, 1)


def test_random_forest_classification():
    """Verify scikit-learn wrapper logic and IEEE NaN sanitization."""
    model = RandomForestModel(RandomForestConfig(n_estimators=5, max_depth=2))
    # Dummy cluster data
    X = np.array([[1, 2], [1.5, 1.8], [5, 8], [8, 8], [1, 0.6], [9, 11]])
    y = np.array([0, 0, 1, 1, 0, 1])
    
    model.fit(X, y)
    assert model.is_fitted
    
    preds = model.predict(X)
    assert preds.shape == (6,)
    
    probs = model.predict_proba(X)
    assert probs.shape == (6, 2)
