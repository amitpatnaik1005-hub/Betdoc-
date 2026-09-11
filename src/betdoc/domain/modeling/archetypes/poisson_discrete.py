
"""Hierarchical double-Poisson archetype for low-count discrete scoring.



Target sports: football/soccer, ice hockey, handball, water polo.



Two likelihood modes are provided:



``correlation=False`` (default, matches the Group 9 specification)

    Independent Poisson marginals with a shared log-link. Fast, well

    identified, and the standard Dixon-Coles precursor. Known weakness: it

    under-predicts draws because it assumes the two scorelines are

    conditionally independent given team strengths.



``correlation=True``

    A genuine bivariate Poisson with a shared covariance component

    ``lambda_cov``. The marginal means become ``theta_home + lambda_cov`` and

    ``theta_away + lambda_cov``, and ``Corr > 0`` lifts draw probability into

    the empirically observed range. The log-pmf contains a sum over

    ``k = 0..min(x, y)`` with no closed form, so it is evaluated as a masked

    ``logsumexp`` over a padded k-axis and attached via :func:`pm.Potential`.

    Consequence: because the likelihood is a Potential rather than an

    ``observed`` distribution, ``pm.sample_posterior_predictive`` is not

    available in this mode. :meth:`predict_odds` simulates from the posterior

    directly, so pricing is unaffected.

"""



from __future__ import annotations



import logging

from typing import Any, Final



import arviz as az

import numpy as np

import polars as pl

import pymc as pm

import pytensor.tensor as pt



from betdoc.domain.modeling.archetypes.base import BayesianArchetype

from betdoc.domain.modeling.types import (

    MATCH_SCHEMA,

    ModelUpdateError,

    SamplerConfig,

    SportArchetype,

)



__all__ = ["PoissonDiscreteArchetype"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_SIMULATIONS: Final[int] = 10_000

MIN_OBSERVATIONS: Final[int] = 20





class PoissonDiscreteArchetype(BayesianArchetype):

    """Hierarchical Poisson goal-scoring model with zero-sum team effects.



    Attack and defense strengths use :class:`pm.ZeroSumNormal` so that the

    team effects are identified against the global ``intercept`` rather than

    drifting jointly with it. Without the zero-sum constraint the posterior

    has a ridge (add c to every attack, subtract c from the intercept) and

    NUTS will happily wander along it, wrecking R-hat.

    """



    archetype = SportArchetype.POISSON_DISCRETE



    def __init__(

        self,

        *,

        sampler: SamplerConfig | None = None,

        name: str | None = None,

        correlation: bool = False,

        home_advantage_mu: float = 0.2,

        home_advantage_sigma: float = 0.1,

        intercept_mu: float = 1.0,

        intercept_sigma: float = 0.5,

        simulations: int = DEFAULT_SIMULATIONS,

    ) -> None:

        super().__init__(sampler=sampler, name=name)

        self._correlation: bool = correlation

        self._home_advantage_mu: float = home_advantage_mu

        self._home_advantage_sigma: float = home_advantage_sigma

        self._intercept_mu: float = intercept_mu

        self._intercept_sigma: float = intercept_sigma

        self._simulations: int = int(simulations)

        if self._simulations < 1000:

            raise ModelUpdateError(

                "simulations must be >= 1000 for stable scoreline probabilities",

                code="MALFORMED_CONFIG",

            )



    @property

    def models_correlation(self) -> bool:

        """Whether the shared covariance component is active."""

        return self._correlation



    def _fit(

        self, data: pl.DataFrame

    ) -> tuple[az.InferenceData, dict[str, int], pm.Model]:

        """Build and sample the hierarchical Poisson model.



        ``participant_a`` is treated as the home side and ``participant_b``

        as the away side. Feeds that do not encode venue must not use this

        archetype, since the home-advantage term would absorb an arbitrary

        orientation.

        """

        frame = self._validate(data)

        index = self._build_index(frame["participant_a"], frame["participant_b"])

        n_teams = len(index)

        if n_teams < 2:

            raise ModelUpdateError(

                f"need at least 2 distinct teams, found {n_teams}",

                code="INSUFFICIENT_DATA",

            )



        home_idx = self._codes(frame["participant_a"], index)

        away_idx = self._codes(frame["participant_b"], index)

        goals_home = frame["score_a"].to_numpy().astype(np.int64, copy=False)

        goals_away = frame["score_b"].to_numpy().astype(np.int64, copy=False)



        if np.any(goals_home < 0) or np.any(goals_away < 0):

            raise ModelUpdateError(

                "Poisson archetype received a negative score; "

                "check the upstream feed's handling of voided fixtures",

                code="SCHEMA_MISMATCH",

            )



        coords: dict[str, list[str]] = {

            "team": sorted(index, key=lambda team: index[team]),

            "match": frame["match_id"].to_list(),

        }



        with pm.Model(coords=coords) as model:

            intercept = pm.Normal(

                "intercept", mu=self._intercept_mu, sigma=self._intercept_sigma

            )

            home_advantage = pm.Normal(

                "home_advantage",

                mu=self._home_advantage_mu,

                sigma=self._home_advantage_sigma,

            )



            sigma_att = pm.Exponential("sigma_att", 1.0)

            sigma_def = pm.Exponential("sigma_def", 1.0)

            attack = pm.ZeroSumNormal("attack", sigma=sigma_att, shape=n_teams)

            defense = pm.ZeroSumNormal("defense", sigma=sigma_def, shape=n_teams)



            log_theta_home = (

                intercept + home_advantage + attack[home_idx] - defense[away_idx]

            )

            log_theta_away = intercept + attack[away_idx] - defense[home_idx]



            theta_home = pm.Deterministic(

                "theta_home", pt.exp(log_theta_home), dims="match"

            )

            theta_away = pm.Deterministic(

                "theta_away", pt.exp(log_theta_away), dims="match"

            )



            if self._correlation:

                lambda_cov = pm.HalfNormal("lambda_cov", sigma=0.25)

                pm.Potential(

                    "bivariate_poisson_loglik",

                    self._bivariate_poisson_logp(

                        goals_home, goals_away, theta_home, theta_away, lambda_cov

                    ),

                )

            else:

                pm.Poisson(

                    "goals_home", mu=theta_home, observed=goals_home, dims="match"

                )

                pm.Poisson(

                    "goals_away", mu=theta_away, observed=goals_away, dims="match"

                )



            idata = pm.sample(**self._sampler.to_sample_kwargs())



        return idata, index, model



    @staticmethod

    def _bivariate_poisson_logp(

        goals_home: np.ndarray,

        goals_away: np.ndarray,

        theta_home: pt.TensorVariable,

        theta_away: pt.TensorVariable,

        lambda_cov: pt.TensorVariable,

    ) -> pt.TensorVariable:

        """Total log-likelihood of the bivariate Poisson (Karlis-Ntzoufras).



        For ``X = U1 + U3``, ``Y = U2 + U3`` with independent Poisson

        components, the joint pmf is



        .. math::



            P(x, y) = e^{-(\\lambda_1 + \\lambda_2 + \\lambda_3)}

                      \\frac{\\lambda_1^x}{x!}\\frac{\\lambda_2^y}{y!}

                      \\sum_{k=0}^{\\min(x,y)}

                      \\binom{x}{k}\\binom{y}{k} k!

                      \\left(\\frac{\\lambda_3}{\\lambda_1\\lambda_2}\\right)^k



        The inner sum is evaluated in log space over a padded ``k`` axis of

        width ``min(x, y).max() + 1``; entries with ``k > min(x, y)`` are

        masked to ``-inf`` so they contribute no mass to the ``logsumexp``.

        The ``k = 0`` term is always valid, so the reduction never sees an

        all-``-inf`` row.

        """

        k_max = int(np.minimum(goals_home, goals_away).max())

        k_axis = np.arange(k_max + 1, dtype=np.float64)[:, None]



        x = goals_home.astype(np.float64)[None, :]

        y = goals_away.astype(np.float64)[None, :]



        log_binom_x = (

            pt.gammaln(x + 1.0)

            - pt.gammaln(k_axis + 1.0)

            - pt.gammaln(pt.maximum(x - k_axis, 0.0) + 1.0)

        )

        log_binom_y = (

            pt.gammaln(y + 1.0)

            - pt.gammaln(k_axis + 1.0)

            - pt.gammaln(pt.maximum(y - k_axis, 0.0) + 1.0)

        )

        log_ratio = pt.log(lambda_cov) - pt.log(theta_home) - pt.log(theta_away)



        log_terms = (

            log_binom_x + log_binom_y + pt.gammaln(k_axis + 1.0) + k_axis * log_ratio

        )

        valid = (k_axis <= np.minimum(goals_home, goals_away)[None, :]).astype(

            np.float64

        )

        log_terms = pt.switch(pt.gt(valid, 0.0), log_terms, -np.inf)

        log_sum = pt.logsumexp(log_terms, axis=0)



        log_prob = (

            -(theta_home + theta_away + lambda_cov)

            + goals_home * pt.log(theta_home)

            + goals_away * pt.log(theta_away)

            - pt.gammaln(goals_home + 1.0)

            - pt.gammaln(goals_away + 1.0)

            + log_sum

        )

        return pt.sum(log_prob)



    def predict_odds(self, **kwargs: Any) -> dict[str, float]:

        """Simulate the match and return 1X2 plus derived goal markets.



        Parameters

        ----------

        home:

            Home participant name (required).

        away:

            Away participant name (required).

        simulations:

            Monte Carlo draws, defaulting to the configured 10,000. Each draw

            takes one joint posterior sample and then one Poisson realisation,

            so both parameter and outcome uncertainty propagate into the

            quoted price. Averaging Poisson pmfs at the posterior *mean*

            instead would understate tail probabilities.



        Returns

        -------

        dict[str, float]

            ``home_win``, ``draw``, ``away_win``, ``over_2_5``, ``under_2_5``,

            ``btts``, ``expected_goals_home``, ``expected_goals_away``, and

            ``most_likely_score_home`` / ``most_likely_score_away``.

        """

        self._require_trained()

        home = kwargs.get("home")

        away = kwargs.get("away")

        if not isinstance(home, str) or not isinstance(away, str):

            raise ModelUpdateError(

                "predict_odds requires string 'home' and 'away' participants",

                code="MALFORMED_INPUT",

            )

        if home == away:

            raise ModelUpdateError(

                f"cannot price {home!r} against itself", code="MALFORMED_INPUT"

            )



        simulations = int(kwargs.get("simulations", self._simulations))

        home_code = self._resolve(home)

        away_code = self._resolve(away)



        intercept = self._flat_posterior("intercept")

        advantage = self._flat_posterior("home_advantage")

        attack = self._flat_posterior("attack")

        defense = self._flat_posterior("defense")



        picks = self._draw_indices(intercept.shape[0], simulations)

        theta_home = np.exp(

            intercept[picks]

            + advantage[picks]

            + attack[picks, home_code]

            - defense[picks, away_code]

        )

        theta_away = np.exp(

            intercept[picks]

            + attack[picks, away_code]

            - defense[picks, home_code]

        )



        goals_home = self._rng.poisson(theta_home)

        goals_away = self._rng.poisson(theta_away)



        if self._correlation:

            shared = self._rng.poisson(self._flat_posterior("lambda_cov")[picks])

            goals_home = goals_home + shared

            goals_away = goals_away + shared



        total = goals_home + goals_away

        home_wins = int(np.count_nonzero(goals_home > goals_away))

        away_wins = int(np.count_nonzero(goals_away > goals_home))

        draws = simulations - home_wins - away_wins



        probabilities = self._normalise([home_wins, draws, away_wins])

        modal_home, modal_away = self._modal_scoreline(goals_home, goals_away)



        return {

            "home_win": probabilities[0],

            "draw": probabilities[1],

            "away_win": probabilities[2],

            "over_2_5": float(np.count_nonzero(total > 2) / simulations),

            "under_2_5": float(np.count_nonzero(total <= 2) / simulations),

            "btts": float(

                np.count_nonzero((goals_home > 0) & (goals_away > 0)) / simulations

            ),

            "expected_goals_home": float(goals_home.mean()),

            "expected_goals_away": float(goals_away.mean()),

            "most_likely_score_home": float(modal_home),

            "most_likely_score_away": float(modal_away),

        }



    @staticmethod

    def _modal_scoreline(

        goals_home: np.ndarray, goals_away: np.ndarray

    ) -> tuple[int, int]:

        """Return the single most frequently simulated exact scoreline."""

        cap = 15

        clipped_home = np.clip(goals_home, 0, cap)

        clipped_away = np.clip(goals_away, 0, cap)

        flat = clipped_home * (cap + 1) + clipped_away

        modal = int(np.bincount(flat, minlength=(cap + 1) ** 2).argmax())

        return divmod(modal, cap + 1)



    def _validate(self, data: pl.DataFrame) -> pl.DataFrame:

        """Validate against :data:`MATCH_SCHEMA` with a minimum-history floor."""

        from betdoc.domain.modeling.types import validate_frame



        return validate_frame(data, MATCH_SCHEMA, min_rows=MIN_OBSERVATIONS)


