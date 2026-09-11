
"""Continuous margin-of-victory archetype for high-count scoring sports.



Target sports: basketball, NFL, rugby, Aussie rules.



Once a sport routinely scores 40+ points, the discrete scoring process is

well approximated by a continuous margin and modelling individual scoring

events buys nothing. The margin is modelled directly:



.. math::



    \\Delta = S_{home} - S_{away}

    \\sim \\mathrm{StudentT}(\\nu,\\; \\mu_{home} - \\mu_{away} + \\mathrm{HFA},\\; \\sigma)



Student-T rather than Gaussian is the default because blowouts and garbage-time

collapses give empirical margin distributions heavier tails than a normal. A

Gaussian likelihood fits the centre and then prices the tails far too thin,

which is exactly where large-handicap markets live. Set ``robust=False`` for

the plain Gaussian in the specification.

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



__all__ = ["GaussianSpreadArchetype"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_SIMULATIONS: Final[int] = 10_000

MIN_OBSERVATIONS: Final[int] = 20





class GaussianSpreadArchetype(BayesianArchetype):

    """Bayesian point-differential model with zero-sum team ratings."""



    archetype = SportArchetype.GAUSSIAN_SPREAD



    def __init__(

        self,

        *,

        sampler: SamplerConfig | None = None,

        name: str | None = None,

        robust: bool = True,

        rating_prior_sigma: float = 10.0,

        hfa_mu: float = 2.5,

        hfa_sigma: float = 1.5,

        margin_sigma_prior: float = 15.0,

        simulations: int = DEFAULT_SIMULATIONS,

    ) -> None:

        super().__init__(sampler=sampler, name=name)

        self._robust: bool = robust

        self._rating_prior_sigma: float = rating_prior_sigma

        self._hfa_mu: float = hfa_mu

        self._hfa_sigma: float = hfa_sigma

        self._margin_sigma_prior: float = margin_sigma_prior

        self._simulations: int = int(simulations)



    @property

    def is_robust(self) -> bool:

        """Whether the heavy-tailed Student-T likelihood is active."""

        return self._robust



    def _fit(

        self, data: pl.DataFrame

    ) -> tuple[az.InferenceData, dict[str, int], pm.Model]:

        """Build and sample the margin model."""

        frame = validate_frame(data, MATCH_SCHEMA, min_rows=MIN_OBSERVATIONS)

        index = self._build_index(frame["participant_a"], frame["participant_b"])

        n_teams = len(index)

        if n_teams < 2:

            raise ModelUpdateError(

                f"need at least 2 distinct teams, found {n_teams}",

                code="INSUFFICIENT_DATA",

            )



        home_idx = self._codes(frame["participant_a"], index)

        away_idx = self._codes(frame["participant_b"], index)

        margin = (

            frame["score_a"].cast(pl.Float64) - frame["score_b"].cast(pl.Float64)

        ).to_numpy()



        coords: dict[str, list[str]] = {

            "team": sorted(index, key=lambda team: index[team]),

            "match": frame["match_id"].to_list(),

        }



        with pm.Model(coords=coords) as model:

            sigma_rating = pm.HalfNormal("sigma_rating", sigma=self._rating_prior_sigma)

            rating = pm.ZeroSumNormal("rating", sigma=sigma_rating, shape=n_teams)

            home_field = pm.Normal(

                "home_field_advantage", mu=self._hfa_mu, sigma=self._hfa_sigma

            )

            sigma_margin = pm.HalfNormal(

                "sigma_margin", sigma=self._margin_sigma_prior

            )



            mu_margin = pm.Deterministic(

                "mu_margin",

                rating[home_idx] - rating[away_idx] + home_field,

                dims="match",

            )



            if self._robust:

                # Lower bound nu at 2 so the variance remains finite; an

                # unbounded nu wanders toward 1 on small samples and the

                # implied Cauchy has no mean to price against.

                nu = pm.Truncated(

                    "nu", pm.Exponential.dist(1.0 / 20.0), lower=2.0, upper=None

                )

                pm.StudentT(

                    "margin",

                    nu=nu,

                    mu=mu_margin,

                    sigma=sigma_margin,

                    observed=margin,

                    dims="match",

                )

            else:

                pm.Normal(

                    "margin",

                    mu=mu_margin,

                    sigma=sigma_margin,

                    observed=margin,

                    dims="match",

                )



            idata = pm.sample(**self._sampler.to_sample_kwargs())



        return idata, index, model



    def predict_odds(self, **kwargs: Any) -> dict[str, float]:

        """Simulate the margin and return moneyline plus spread diagnostics.



        Parameters

        ----------

        home, away:

            Participant names (required).

        spread:

            Optional handicap applied to the home side, using the standard

            sign convention where ``-6.5`` means the home team must win by

            7 or more. Defaults to ``0.0``.

        simulations:

            Monte Carlo draws, default 10,000.



        Returns

        -------

        dict[str, float]

            ``home_win``, ``away_win``, ``push``, ``expected_margin_median``,

            ``expected_margin_mean``, ``margin_p05`` / ``margin_p95``,

            ``home_covers`` and ``away_covers``.

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



        spread = float(kwargs.get("spread", 0.0))

        simulations = int(kwargs.get("simulations", self._simulations))

        home_code = self._resolve(home)

        away_code = self._resolve(away)



        rating = self._flat_posterior("rating")

        home_field = self._flat_posterior("home_field_advantage")

        sigma = self._flat_posterior("sigma_margin")



        picks = self._draw_indices(sigma.shape[0], simulations)

        mu = (

            rating[picks, home_code]

            - rating[picks, away_code]

            + home_field[picks]

        )



        if self._robust:

            nu = self._flat_posterior("nu")[picks]

            # StudentT(nu, mu, sigma) == mu + sigma * standard_t(nu)

            draws = mu + sigma[picks] * self._rng.standard_t(nu)

        else:

            draws = self._rng.normal(loc=mu, scale=sigma[picks])



        # Scoring is integral even when modelled continuously, so round to

        # recover a genuine push/draw probability at whole-number lines.

        discrete = np.rint(draws)

        home_wins = int(np.count_nonzero(discrete > 0))

        away_wins = int(np.count_nonzero(discrete < 0))

        pushes = simulations - home_wins - away_wins



        covered = discrete + spread

        home_covers = int(np.count_nonzero(covered > 0))

        away_covers = int(np.count_nonzero(covered < 0))

        cover_decided = home_covers + away_covers



        return {

            "home_win": home_wins / simulations,

            "away_win": away_wins / simulations,

            "push": pushes / simulations,

            "expected_margin_median": float(np.median(draws)),

            "expected_margin_mean": float(draws.mean()),

            "margin_p05": float(np.percentile(draws, 5.0)),

            "margin_p95": float(np.percentile(draws, 95.0)),

            "home_covers": (home_covers / cover_decided) if cover_decided else 0.0,

            "away_covers": (away_covers / cover_decided) if cover_decided else 0.0,

        }


