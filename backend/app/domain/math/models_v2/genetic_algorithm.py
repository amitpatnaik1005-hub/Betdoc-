"""Evolutionary portfolio optimisation maximising the Sharpe ratio."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Self

import numpy as np
from pydantic import Field, model_validator

from app.domain.math.models_v2.base import (
    FLOAT_EPS,
    FLOAT_TINY,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_feature_matrix,
    check_finite_scalar,
    freeze,
    resolve_config,
    safe_array_divide,
    sanitize,
)

__all__ = ["GeneticAlgorithmConfig", "GeneticAlgorithmModel"]


class GeneticAlgorithmConfig(MathModelConfig):
    population_size: int = Field(default=100, ge=4)
    n_generations: int = Field(default=200, ge=1)
    crossover_rate: float = Field(default=0.9, ge=0.0, le=1.0)
    mutation_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    mutation_scale: float = Field(default=0.05, gt=0.0)
    elite_count: int = Field(default=2, ge=0)
    tournament_size: int = Field(default=3, ge=2)
    risk_free_rate: float = Field(default=0.0, description="Per-period risk-free rate.")
    annualization_factor: float = Field(default=252.0, gt=0.0)
    allow_short: bool = False
    max_weight: float = Field(default=1.0, gt=0.0, le=1.0)
    ddof: int = Field(default=1, ge=0)
    patience: int = Field(default=25, ge=1)
    improvement_tol: float = Field(default=1e-9, ge=0.0)
    min_volatility: float = Field(default=1e-12, gt=0.0)

    @model_validator(mode="after")
    def _check_sizes(self) -> Self:
        if self.elite_count >= self.population_size:
            raise ValueError("elite_count must be < population_size.")
        if self.tournament_size > self.population_size:
            raise ValueError("tournament_size must be <= population_size.")
        return self


@dataclass(frozen=True, slots=True)
class _GAState:
    weights: FloatArray
    best_sharpe: float
    fitness_history: FloatArray
    generations_run: int
    n_assets: int


class GeneticAlgorithmModel(BaseMathModel):
    r"""Genetic search over portfolio weights.

    Fitness (annualised Sharpe ratio) for weights :math:`w` and period returns :math:`R`:

    .. math:: S(w) = \sqrt{A}\,\frac{\mathbb{E}[R w - r_f]}{\sigma(R w - r_f)}

    Operators:

    * Initialisation: :math:`w \sim \mathrm{Dirichlet}(\mathbf 1)` (long-only) or
      :math:`U(-1,1)^n / \|\cdot\|_1` (long-short).
    * Tournament selection of size :math:`k`.
    * Blend crossover: :math:`c = \beta p_1 + (1-\beta) p_2,\ \beta_j \sim U(0,1)`.
    * Gaussian mutation: :math:`c_j \leftarrow c_j + \mathbb{1}[u_j < p_m]\,\mathcal N(0, s^2)`.
    * Feasibility projection: long-only :math:`\sum w = 1,\ 0 \le w \le w_{\max}`
      (iterative capped water-filling); long-short :math:`\|w\|_1 \le 1,\ |w| \le w_{\max}`.

    ``fit`` takes a ``(T, n_assets)`` return matrix. ``predict`` returns the portfolio
    return series :math:`R w^\star` for new returns.
    """

    config: GeneticAlgorithmConfig

    def __init__(self, config: GeneticAlgorithmConfig | None = None) -> None:
        super().__init__(resolve_config(config, GeneticAlgorithmConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        returns = as_feature_matrix(X, name="X", min_samples=cfg.ddof + 2)
        n_assets = returns.shape[1]
        if not cfg.allow_short and cfg.max_weight * n_assets < 1.0 - FLOAT_EPS:
            raise ValueError("max_weight * n_assets must be >= 1 for a fully invested long-only portfolio.")

        rng = self._rng(0)
        population = self.initialize_population(rng, n_assets)
        fitness = self.evaluate_fitness(returns, population)
        best_idx = int(np.argmax(fitness))
        best_w, best_f = population[best_idx].copy(), float(fitness[best_idx])
        history = [best_f]
        stale = 0
        generations_run = 0

        n_children = cfg.population_size - cfg.elite_count
        for generation in range(cfg.n_generations):
            elite_idx = np.argsort(-fitness, kind="stable")[: cfg.elite_count]
            parents_a = population[self._tournament(rng, fitness, n_children)]
            parents_b = population[self._tournament(rng, fitness, n_children)]
            children = self.mutate(rng, self.crossover(rng, parents_a, parents_b))
            population = np.vstack([population[elite_idx], self.project(children)])
            fitness = self.evaluate_fitness(returns, population)
            generations_run = generation + 1

            idx = int(np.argmax(fitness))
            if float(fitness[idx]) > best_f + cfg.improvement_tol:
                best_w, best_f = population[idx].copy(), float(fitness[idx])
                stale = 0
            else:
                stale += 1
            history.append(best_f)
            if stale >= cfg.patience:
                break

        self._publish_state(
            _GAState(
                weights=freeze(best_w),
                best_sharpe=check_finite_scalar(best_f, name="best_sharpe"),
                fitness_history=freeze(np.asarray(history, dtype=np.float64)),
                generations_run=generations_run,
                n_assets=n_assets,
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _GAState = self._current_state()
        returns = as_feature_matrix(X, name="X", n_features=state.n_assets)
        return sanitize(returns @ state.weights)

    def initialize_population(self, rng: np.random.Generator, n_assets: int) -> FloatArray:
        size = self.config.population_size
        if self.config.allow_short:
            genes = rng.uniform(-1.0, 1.0, size=(size, n_assets))
        else:
            genes = rng.dirichlet(np.ones(n_assets), size=size)
        return self.project(genes)

    def evaluate_fitness(self, returns: FloatArray, weights: FloatArray) -> FloatArray:
        cfg = self.config
        excess = returns @ weights.T - cfg.risk_free_rate
        mean = excess.mean(axis=0)
        std = excess.std(axis=0, ddof=cfg.ddof)
        sharpe = safe_array_divide(mean, std) * math.sqrt(cfg.annualization_factor)
        sharpe[std < cfg.min_volatility] = 0.0
        return sanitize(sharpe)

    def sharpe_ratio(self, portfolio_returns: FloatArray) -> float:
        series = as_feature_matrix(np.asarray(portfolio_returns, dtype=np.float64).reshape(-1, 1), name="returns")
        return float(self.evaluate_fitness(series, np.ones((1, 1)))[0])

    def crossover(self, rng: np.random.Generator, parents_a: FloatArray, parents_b: FloatArray) -> FloatArray:
        mix = rng.random(parents_a.shape[0]) < self.config.crossover_rate
        beta = rng.random(parents_a.shape)
        blended = beta * parents_a + (1.0 - beta) * parents_b
        return np.where(mix[:, None], blended, parents_a)

    def mutate(self, rng: np.random.Generator, children: FloatArray) -> FloatArray:
        mask = rng.random(children.shape) < self.config.mutation_rate
        noise = rng.normal(0.0, self.config.mutation_scale, size=children.shape)
        return children + mask * noise

    def project(self, genes: FloatArray) -> FloatArray:
        cap = self.config.max_weight
        n_assets = genes.shape[1]
        if self.config.allow_short:
            gross = np.abs(genes).sum(axis=1, keepdims=True)
            weights = np.where(gross > FLOAT_TINY, genes / np.where(gross > FLOAT_TINY, gross, 1.0), 1.0 / n_assets)
            return np.clip(weights, -cap, cap)
        weights = np.clip(genes, 0.0, None)
        sums = weights.sum(axis=1, keepdims=True)
        weights = np.where(sums > FLOAT_TINY, weights / np.where(sums > FLOAT_TINY, sums, 1.0), 1.0 / n_assets)
        for _ in range(n_assets):
            if not np.any(weights > cap + FLOAT_EPS):
                break
            excess = np.clip(weights - cap, 0.0, None).sum(axis=1, keepdims=True)
            weights = np.minimum(weights, cap)
            free = weights < cap - FLOAT_EPS
            free_mass = (weights * free).sum(axis=1, keepdims=True)
            proportional = safe_array_divide(weights * free, free_mass)
            equal = safe_array_divide(free.astype(np.float64), free.sum(axis=1, keepdims=True))
            share = np.where(free_mass > FLOAT_TINY, proportional, equal)
            weights = weights + excess * share
        return weights

    def _tournament(self, rng: np.random.Generator, fitness: FloatArray, n_select: int) -> np.ndarray:
        contenders = rng.integers(0, fitness.size, size=(n_select, self.config.tournament_size))
        winners = np.argmax(fitness[contenders], axis=1)
        return contenders[np.arange(n_select), winners]

    @property
    def weights(self) -> FloatArray:
        return self._current_state().weights

    @property
    def best_sharpe(self) -> float:
        return self._current_state().best_sharpe

    @property
    def fitness_history(self) -> FloatArray:
        return self._current_state().fitness_history
