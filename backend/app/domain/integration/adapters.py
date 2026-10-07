"""Ports and default adapters bridging the orchestrator to the math and risk engines."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.domain.math.models_v2.normal_distribution import NormalDistributionModel


@dataclass(frozen=True, slots=True)
class ResearchInput:
    expected_margin: float
    spread_line: float


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    stake: float
    edge: float
    implied_probability: float
    reason: str


class PredictionModelPort(Protocol):
    @property
    def name(self) -> str: ...

    async def predict_probability(self, research: ResearchInput) -> float: ...


class RiskEnginePort(Protocol):
    def evaluate(self, *, probability: float, decimal_odds: float, bankroll: float, open_exposure: float) -> RiskDecision: ...


class SpreadCoverPredictionAdapter:
    """Runs the CPU-bound v2 Gaussian spread model off the event loop via ``asyncio.to_thread``."""

    def __init__(self, model: NormalDistributionModel, name: str) -> None:
        self._model = model
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def predict_probability(self, research: ResearchInput) -> float:
        features = np.array([[research.expected_margin, research.spread_line]], dtype=np.float64)
        output = await asyncio.to_thread(self._model.predict, features)
        probability = float(output[0])
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(f"Model {self._name} produced an invalid probability: {probability!r}.")
        return probability


class KellyRiskEngine:
    r"""Fractional Kelly with hard caps.

    :math:`f^\ast = (pb - q)/b`, stake :math:`= \min(\kappa f^\ast, f_{\max})\,B`, then clipped to the
    remaining exposure headroom :math:`e_{\max}B - E_{\text{open}}`. Rejects if edge < ``min_edge``.
    """

    def __init__(
        self, *, kelly_fraction: float, max_stake_fraction: float, max_open_exposure_fraction: float, min_edge: float
    ) -> None:
        self._kelly_fraction = kelly_fraction
        self._max_stake_fraction = max_stake_fraction
        self._max_open_exposure_fraction = max_open_exposure_fraction
        self._min_edge = min_edge

    def evaluate(self, *, probability: float, decimal_odds: float, bankroll: float, open_exposure: float) -> RiskDecision:
        values = (probability, decimal_odds, bankroll, open_exposure)
        if not all(math.isfinite(v) for v in values) or decimal_odds <= 1.0 or bankroll <= 0.0 or not 0.0 <= probability <= 1.0:
            return RiskDecision(False, 0.0, 0.0, 0.0, "rejected: invalid risk inputs")
        implied = 1.0 / decimal_odds
        edge = probability - implied
        if edge < self._min_edge:
            return RiskDecision(False, 0.0, edge, implied, "rejected: edge below minimum")
        b = decimal_odds - 1.0
        full_kelly = (probability * b - (1.0 - probability)) / b
        stake = min(full_kelly * self._kelly_fraction, self._max_stake_fraction) * bankroll
        headroom = max(self._max_open_exposure_fraction * bankroll - open_exposure, 0.0)
        stake = min(stake, headroom)
        if stake <= 0.0:
            return RiskDecision(False, 0.0, edge, implied, "rejected: exposure limit reached")
        return RiskDecision(True, stake, edge, implied, "approved: within risk limits")
