
"""Bayesian Bradley-Terry archetype for pairwise head-to-head contests.



Target sports: tennis, MMA/boxing, esports, darts, snooker, table tennis.



Each participant carries a latent skill :math:`\\theta_i` and



.. math::



    P(i \\succ j) = \\mathrm{logit}^{-1}(\\theta_i - \\theta_j)

                  = \\frac{1}{1 + e^{-(\\theta_i - \\theta_j)}}



Only skill *differences* are identified: adding a constant to every

:math:`\\theta` leaves the likelihood unchanged. :class:`pm.ZeroSumNormal`

pins the sum to zero so NUTS cannot drift along that ridge, which is the

difference between clean R-hat and a posterior that never converges.



Draws are not representable in this archetype. Fixtures with equal scores are

dropped before fitting and the count is surfaced in the diagnostics warnings,

because silently coercing a draw into a win for the first-listed participant

would bias every skill estimate in the pool.

"""



from __future__ import annotations



import logging

from typing import Any, Final



import arviz as az

import numpy as np

import polars as pl

import pymc as pm



from betdoc.domain.modeling.archetypes.base import BayesianArchetype

from betdoc.domain.modeling.types import (

    MATCH_SCHEMA,

    ModelUpdateError,

    SamplerConfig,

    SportArchetype,

    validate_frame,

)



__all__ = ["BradleyTerryArchetype"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



MIN_OBSERVATIONS: Final[int] = 20





class BradleyTerryArchetype(BayesianArchetype):

    """Latent-skill pairwise comparison model with exact 2-way pricing."""



    archetype = SportArchetype.BRADLEY_TERRY_H2H



    def __init__(

        self,

        *,

        sampler: SamplerConfig | None = None,

        name: str | None = None,

        model_first_listed_advantage: bool = False,

        advantage_mu: float = 0.0,

        advantage_sigma: float = 0.25,

    ) -> None:

        super().__init__(sampler=sampler, name=name)

        self._model_advantage: bool = model_first_listed_advantage

        self._advantage_mu: float = advantage_mu

        self._advantage_sigma: float = advantage_sigma

        self._dropped_draws: int = 0



    @property

    def dropped_draws(self) -> int:

        """Number of drawn fixtures excluded by the most recent fit."""

        return self._dropped_draws



    def _fit(

        self, data: pl.DataFrame

    ) -> tuple[az.InferenceData, dict[str, int], pm.Model]:

        """Build and sample the Bradley-Terry model."""

        frame = validate_frame(data, MATCH_SCHEMA, min_rows=MIN_OBSERVATIONS)



        decided = frame.filter(pl.col("score_a") != pl.col("score_b"))

        self._dropped_draws = frame.height - decided.height

        if self._dropped_draws:

            _LOG.warning(

                "%s dropped %d drawn fixture(s): Bradley-Terry has no draw term",

                self.name,

                self._dropped_draws,

            )

        if decided.height < MIN_OBSERVATIONS:

            raise ModelUpdateError(

                f"only {decided.height} decided contest(s) remain after removing "

                f"{self._dropped_draws} draw(s); need {MIN_OBSERVATIONS}",

                code="INSUFFICIENT_DATA",

            )



        index = self._build_index(

            decided["participant_a"], decided["participant_b"]

        )

        n_players = len(index)

        if n_players < 2:

            raise ModelUpdateError(

                f"need at least 2 distinct participants, found {n_players}",

                code="INSUFFICIENT_DATA",

            )



        idx_a = self._codes(decided["participant_a"], index)

        idx_b = self._codes(decided["participant_b"], index)

        a_wins = (

            (decided["score_a"] > decided["score_b"])

            .cast(pl.Int8)

            .to_numpy()

            .astype(np.int64, copy=False)

        )



        coords: dict[str, list[str]] = {

            "player": sorted(index, key=lambda player: index[player]),

            "contest": decided["match_id"].to_list(),

        }



        with pm.Model(coords=coords) as model:

            sigma_skill = pm.Exponential("sigma_skill", 1.0)

            skill = pm.ZeroSumNormal("skill", sigma=sigma_skill, shape=n_players)



            logit_p = skill[idx_a] - skill[idx_b]

            if self._model_advantage:

                advantage = pm.Normal(

                    "advantage", mu=self._advantage_mu, sigma=self._advantage_sigma

                )

                logit_p = logit_p + advantage



            pm.Deterministic("logit_p", logit_p, dims="contest")

            pm.Bernoulli("outcome", logit_p=logit_p, observed=a_wins, dims="contest")



            idata = pm.sample(**self._sampler.to_sample_kwargs())



        return idata, index, model



    def predict_odds(self, **kwargs: Any) -> dict[str, float]:

        """Return exact 2-way moneyline probabilities.



        The posterior mean of the *probability* is computed, not the

        probability at the posterior mean of the skills. Because the logistic

        is non-linear, those two quantities differ, and the second one

        systematically overstates confidence in mismatches. No Monte Carlo

        outcome simulation is needed here: the Bernoulli expectation is

        analytic per posterior draw, so integrating over draws is exact up to

        posterior sampling error.



        Parameters

        ----------

        participant_a, participant_b:

            Participant names (required).



        Returns

        -------

        dict[str, float]

            ``participant_a_win``, ``participant_b_win``, the fair decimal

            odds for each side, and the posterior skill gap with a 90%

            credible interval.

        """

        self._require_trained()

        name_a = kwargs.get("participant_a")

        name_b = kwargs.get("participant_b")

        if not isinstance(name_a, str) or not isinstance(name_b, str):

            raise ModelUpdateError(

                "predict_odds requires string 'participant_a' and 'participant_b'",

                code="MALFORMED_INPUT",

            )

        if name_a == name_b:

            raise ModelUpdateError(

                f"cannot price {name_a!r} against itself", code="MALFORMED_INPUT"

            )



        code_a = self._resolve(name_a)

        code_b = self._resolve(name_b)



        skill = self._flat_posterior("skill")

        gap = skill[:, code_a] - skill[:, code_b]



        if self._model_advantage and kwargs.get("apply_advantage", True):

            gap = gap + self._flat_posterior("advantage")



        # Numerically stable logistic: avoids overflow for large |gap|.

        probability_a = float(np.mean(0.5 * (1.0 + np.tanh(0.5 * gap))))

        probability_b = 1.0 - probability_a



        return {

            "participant_a_win": probability_a,

            "participant_b_win": probability_b,

            "fair_odds_a": self._fair_odds(probability_a),

            "fair_odds_b": self._fair_odds(probability_b),

            "skill_gap_median": float(np.median(gap)),

            "skill_gap_p05": float(np.percentile(gap, 5.0)),

            "skill_gap_p95": float(np.percentile(gap, 95.0)),

        }



    def skill_ratings(self) -> dict[str, float]:

        """Return the posterior median skill for every known participant.



        Useful for monitoring and for sanity-checking a refit against the

        previous model version before it is promoted.

        """

        self._require_trained()

        skill = self._flat_posterior("skill")

        medians = np.median(skill, axis=0)

        return {

            participant: float(medians[code])

            for participant, code in sorted(self._index.items(), key=lambda kv: kv[1])

        }



    @staticmethod

    def _fair_odds(probability: float) -> float:

        """Convert a probability to zero-margin decimal odds."""

        if probability <= 0.0:

            return float("inf")

        return 1.0 / probability


