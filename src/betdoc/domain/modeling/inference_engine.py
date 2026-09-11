
"""Routing facade over the five Bayesian archetypes.



The engine owns one instance of each archetype, routes Polars frames to the

right one for training, and converts a model probability plus a market price

into an expected value and a Kelly stake.

"""



from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import polars as pl

from betdoc.domain.modeling.archetypes.base import BayesianArchetype
from betdoc.domain.modeling.archetypes.bradley_terry_h2h import BradleyTerryArchetype
from betdoc.domain.modeling.archetypes.dynamic_cricket import DynamicCricketArchetype
from betdoc.domain.modeling.archetypes.gaussian_spread import GaussianSpreadArchetype
from betdoc.domain.modeling.archetypes.plackett_luce_racing import (
    PlackettLuceArchetype,
)
from betdoc.domain.modeling.archetypes.poisson_discrete import PoissonDiscreteArchetype
from betdoc.domain.modeling.types import (
    ModelUpdateError,
    SamplerConfig,
    SportArchetype,
    TrainingDiagnostics,
)

__all__ = ["EdgeAssessment", "InferenceEngine"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_KELLY_MULTIPLIER: Final[float] = 0.25

DEFAULT_MAX_FRACTION: Final[float] = 0.05





@dataclass(frozen=True, slots=True)

class EdgeAssessment:

    """Result of comparing a model probability against a market price."""



    market_probability: float

    true_probability: float

    decimal_odds: float

    expected_value: float

    full_kelly_fraction: float

    recommended_fraction: float

    recommended_stake: float

    bankroll: float

    has_edge: bool



    def to_dict(self) -> dict[str, float | bool]:

        """Return a JSON-safe projection for trade logging."""

        return {

            "market_probability": self.market_probability,

            "true_probability": self.true_probability,

            "decimal_odds": self.decimal_odds,

            "expected_value": self.expected_value,

            "full_kelly_fraction": self.full_kelly_fraction,

            "recommended_fraction": self.recommended_fraction,

            "recommended_stake": self.recommended_stake,

            "bankroll": self.bankroll,

            "has_edge": self.has_edge,

        }





class InferenceEngine:

    """Facade routing sports data onto mathematical archetypes."""



    __slots__ = ("_archetypes", "_kelly_multiplier", "_max_fraction")



    def __init__(

        self,

        *,

        sampler: SamplerConfig | None = None,

        archetypes: Mapping[SportArchetype, BayesianArchetype] | None = None,

        kelly_multiplier: float = DEFAULT_KELLY_MULTIPLIER,

        max_fraction: float = DEFAULT_MAX_FRACTION,

    ) -> None:

        if not 0.0 < kelly_multiplier <= 1.0:

            raise ModelUpdateError(

                "kelly_multiplier must lie in (0, 1]", code="MALFORMED_CONFIG"

            )

        if not 0.0 < max_fraction <= 1.0:

            raise ModelUpdateError(

                "max_fraction must lie in (0, 1]", code="MALFORMED_CONFIG"

            )



        self._kelly_multiplier: float = kelly_multiplier

        self._max_fraction: float = max_fraction

        self._archetypes: dict[SportArchetype, BayesianArchetype] = (

            dict(archetypes)

            if archetypes is not None

            else {

                SportArchetype.POISSON_DISCRETE: PoissonDiscreteArchetype(

                    sampler=sampler

                ),

                SportArchetype.GAUSSIAN_SPREAD: GaussianSpreadArchetype(

                    sampler=sampler

                ),

                SportArchetype.BRADLEY_TERRY_H2H: BradleyTerryArchetype(

                    sampler=sampler

                ),

                SportArchetype.PLACKETT_LUCE_RACING: PlackettLuceArchetype(

                    sampler=sampler

                ),

                SportArchetype.DYNAMIC_ACCUMULATION: DynamicCricketArchetype(

                    sampler=sampler

                ),

            }

        )



        missing = [member for member in SportArchetype if member not in self._archetypes]

        if missing:

            raise ModelUpdateError(

                f"engine is missing archetype(s): "

                f"{', '.join(member.value for member in missing)}",

                code="MALFORMED_CONFIG",

            )



    @property

    def archetypes(self) -> Mapping[SportArchetype, BayesianArchetype]:

        """Read-only view of the registered archetypes."""

        return dict(self._archetypes)



    def archetype_for(self, archetype: SportArchetype) -> BayesianArchetype:

        """Return the model instance for ``archetype``."""

        try:

            return self._archetypes[archetype]

        except KeyError as error:

            raise ModelUpdateError(

                f"no model registered for archetype {archetype!r}",

                code="UNKNOWN_ARCHETYPE",

            ) from error



    def archetype_for_sport(self, sport: str) -> BayesianArchetype:

        """Route a free-text sport name straight to its model instance."""

        return self.archetype_for(SportArchetype.for_sport(sport))



    async def train_sport(

        self, archetype: SportArchetype, data: pl.DataFrame

    ) -> None:

        """Route ``data`` to ``archetype`` and train it off the event loop."""

        model = self.archetype_for(archetype)

        _LOG.info(

            "routing %d row(s) to %s for training", data.height, archetype.value

        )

        await model.train(data)



    async def train_all(

        self, datasets: Mapping[SportArchetype, pl.DataFrame]

    ) -> dict[SportArchetype, TrainingDiagnostics | ModelUpdateError]:

        """Train several archetypes concurrently, isolating their failures.



        Each fit is independent, so one archetype's divergence must not abort

        the nightly retrain of the others. Failures are returned alongside

        successes rather than raised.

        """

        keys = list(datasets.keys())

        outcomes = await asyncio.gather(

            *(self.train_sport(key, datasets[key]) for key in keys),

            return_exceptions=True,

        )



        report: dict[SportArchetype, TrainingDiagnostics | ModelUpdateError] = {}

        for key, outcome in zip(keys, outcomes):

            if isinstance(outcome, ModelUpdateError):

                _LOG.error("training %s failed: %s", key.value, outcome)

                report[key] = outcome

            elif isinstance(outcome, BaseException):

                wrapped = ModelUpdateError(

                    f"unexpected failure training {key.value}: {outcome}",

                    code="SAMPLING_FAILED",

                )

                _LOG.error("training %s failed: %s", key.value, outcome)

                report[key] = wrapped

            else:

                diagnostics = self.archetype_for(key).diagnostics

                if diagnostics is not None:

                    report[key] = diagnostics

        return report



    def predict(self, archetype: SportArchetype, **kwargs: Any) -> dict[str, float]:

        """Price a market through the requested archetype."""

        return self.archetype_for(archetype).predict_odds(**kwargs)



    async def calculate_edge(

        self,

        market_probability: float,

        true_probability: float,

        bankroll: float = 1.0,

    ) -> dict[str, float | bool]:

        """Return expected value and the recommended Kelly stake fraction.



        With decimal odds :math:`O = 1 / p_{market}` and net return

        :math:`b = O - 1`, the per-unit expected value is

        :math:`\\mathrm{EV} = p b - (1 - p)` and the growth-optimal fraction

        is :math:`f^{*} = (p b - q) / b`.



        Two deliberate guardrails: a negative :math:`f^{*}` returns a zero

        stake rather than a lay recommendation, because a back-market edge

        calculation says nothing about the layable price; and the fraction is

        scaled by ``kelly_multiplier`` and hard-capped by ``max_fraction``,

        since full Kelly on a *estimated* probability is materially

        over-levered once model error is accounted for.



        Note: this method performs no I/O and is ``async`` only to satisfy the

        specified interface.

        """

        if not 0.0 < market_probability < 1.0:

            raise ModelUpdateError(

                f"market_probability must lie in (0, 1), got {market_probability}",

                code="MALFORMED_INPUT",

            )

        if not 0.0 <= true_probability <= 1.0:

            raise ModelUpdateError(

                f"true_probability must lie in [0, 1], got {true_probability}",

                code="MALFORMED_INPUT",

            )

        if bankroll < 0.0:

            raise ModelUpdateError(

                f"bankroll cannot be negative, got {bankroll}",

                code="MALFORMED_INPUT",

            )



        decimal_odds = 1.0 / market_probability

        net_return = decimal_odds - 1.0

        loss_probability = 1.0 - true_probability



        expected_value = true_probability * net_return - loss_probability

        full_kelly = (

            expected_value / net_return if net_return > 0.0 else 0.0

        )



        recommended = max(0.0, full_kelly) * self._kelly_multiplier

        recommended = min(recommended, self._max_fraction)



        assessment = EdgeAssessment(

            market_probability=market_probability,

            true_probability=true_probability,

            decimal_odds=decimal_odds,

            expected_value=expected_value,

            full_kelly_fraction=full_kelly,

            recommended_fraction=recommended,

            recommended_stake=recommended * bankroll,

            bankroll=bankroll,

            has_edge=expected_value > 0.0,

        )

        return assessment.to_dict()


