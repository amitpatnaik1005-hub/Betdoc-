
"""Resource-depletion archetype for accumulating-score innings sports.



Target sports: T20 cricket (also ODI, and baseball with a reconfigured

resource axis).



Cricket's defining feature is that a score is accumulated against two

simultaneously depleting resources: overs remaining (:math:`u`) and wickets

in hand. The Duckworth-Lewis-style resource curve used here is



.. math::



    R(u, w) = R_0(w)\\left(1 - e^{-b(w)\\,u}\\right)



with wicket-dependent asymptote and decay rate



.. math::



    R_0(w) = R_0 \\left(\\frac{W - w}{W}\\right)^{\\gamma},

    \\qquad b(w) = b_0 e^{\\delta w}



Resources are always reported as a *fraction* of a full innings,

:math:`R(u,w) / R(U, 0)`, which makes the quantity scale-free.



**Identifiability caveat, stated plainly.** The curve hyperparameters

(:math:`b_0, \\gamma, \\delta, R_0`) cannot be estimated from final innings

totals, because a final total contains no information about the *path* taken

to reach it. They are therefore fixed, documented hyperparameters with

defaults in the range implied by published T20 resource tables, and can be

refit by :meth:`calibrate_resource_curve` when partial-innings state data is

available. What *is* fit by MCMC here is the posterior over innings totals

conditioned on batting and bowling strength. Presenting the curve constants

as though they were learned would be a fabrication.

"""



from __future__ import annotations



import logging

import math

from dataclasses import dataclass

from typing import Any, Final



import arviz as az

import numpy as np

import polars as pl

import pymc as pm

import pytensor.tensor as pt

from scipy.special import gammaln



from betdoc.domain.modeling.archetypes.base import BayesianArchetype

from betdoc.domain.modeling.types import (

    MATCH_SCHEMA,

    ModelUpdateError,

    SamplerConfig,

    SportArchetype,

    validate_frame,

)



__all__ = ["DynamicCricketArchetype", "ResourceCurve"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_SIMULATIONS: Final[int] = 10_000

MIN_OBSERVATIONS: Final[int] = 20





@dataclass(frozen=True, slots=True)

class ResourceCurve:

    """Deterministic resource-depletion curve for a fixed-overs innings."""



    total_overs: float = 20.0

    total_wickets: int = 10

    r0: float = 1.0

    b0: float = 0.12

    gamma: float = 0.70

    delta: float = 0.06



    def __post_init__(self) -> None:

        if self.total_overs <= 0:

            raise ModelUpdateError(

                "total_overs must be positive", code="MALFORMED_CONFIG"

            )

        if self.total_wickets < 1:

            raise ModelUpdateError(

                "total_wickets must be >= 1", code="MALFORMED_CONFIG"

            )

        if self.b0 <= 0 or self.r0 <= 0:

            raise ModelUpdateError(

                "b0 and r0 must be positive", code="MALFORMED_CONFIG"

            )



    def decay(self, wickets_lost: int) -> float:

        """Return :math:`b(w)`, the decay rate after ``wickets_lost``."""

        return self.b0 * math.exp(self.delta * float(wickets_lost))



    def asymptote(self, wickets_lost: int) -> float:

        """Return :math:`R_0(w)`, the achievable ceiling with wickets in hand."""

        remaining = max(self.total_wickets - int(wickets_lost), 0)

        if remaining == 0:

            return 0.0

        return self.r0 * (remaining / self.total_wickets) ** self.gamma



    def resources(self, overs_remaining: float, wickets_lost: int) -> float:

        """Return the unnormalised resource value :math:`R(u, w)`."""

        if overs_remaining <= 0 or wickets_lost >= self.total_wickets:

            return 0.0

        ceiling = self.asymptote(wickets_lost)

        return ceiling * (1.0 - math.exp(-self.decay(wickets_lost) * overs_remaining))



    @property

    def full_innings(self) -> float:

        """Resource value of a complete, wicket-intact innings."""

        return self.resources(self.total_overs, 0)



    def fraction_remaining(self, overs_remaining: float, wickets_lost: int) -> float:

        """Return remaining resources as a fraction of a full innings."""

        baseline = self.full_innings

        if baseline <= 0:

            raise ModelUpdateError(

                "resource curve is degenerate: full innings has zero resources",

                code="MALFORMED_CONFIG",

            )

        return max(

            0.0, min(1.0, self.resources(overs_remaining, wickets_lost) / baseline)

        )





class DynamicCricketArchetype(BayesianArchetype):

    """Gamma innings-total model with live resource-conditioned updating."""



    archetype = SportArchetype.DYNAMIC_ACCUMULATION



    def __init__(

        self,

        *,

        sampler: SamplerConfig | None = None,

        name: str | None = None,

        curve: ResourceCurve | None = None,

        intercept_mu: float = 5.0,

        intercept_sigma: float = 0.5,

        simulations: int = DEFAULT_SIMULATIONS,

    ) -> None:

        super().__init__(sampler=sampler, name=name)

        self._curve: ResourceCurve = curve or ResourceCurve()

        self._intercept_mu: float = intercept_mu

        self._intercept_sigma: float = intercept_sigma

        self._simulations: int = int(simulations)



    @property

    def curve(self) -> ResourceCurve:

        """The active resource-depletion curve."""

        return self._curve



    def _fit(

        self, data: pl.DataFrame

    ) -> tuple[az.InferenceData, dict[str, int], pm.Model]:

        """Fit the Gamma innings-total model.



        ``participant_a`` is the batting side and ``score_a`` its completed

        innings total; ``participant_b`` is the bowling side. ``score_b`` is

        unused by this archetype and may carry the reply total.

        """

        frame = validate_frame(data, MATCH_SCHEMA, min_rows=MIN_OBSERVATIONS)

        scoring = frame.filter(pl.col("score_a") > 0)

        if scoring.height < MIN_OBSERVATIONS:

            raise ModelUpdateError(

                f"Gamma likelihood requires strictly positive totals; only "

                f"{scoring.height} usable innings remain",

                code="INSUFFICIENT_DATA",

            )



        index = self._build_index(scoring["participant_a"], scoring["participant_b"])

        n_teams = len(index)

        if n_teams < 2:

            raise ModelUpdateError(

                f"need at least 2 distinct teams, found {n_teams}",

                code="INSUFFICIENT_DATA",

            )



        bat_idx = self._codes(scoring["participant_a"], index)

        bowl_idx = self._codes(scoring["participant_b"], index)

        totals = scoring["score_a"].cast(pl.Float64).to_numpy()



        coords: dict[str, list[str]] = {

            "team": sorted(index, key=lambda team: index[team]),

            "innings": scoring["match_id"].to_list(),

        }



        with pm.Model(coords=coords) as model:

            intercept = pm.Normal(

                "intercept", mu=self._intercept_mu, sigma=self._intercept_sigma

            )

            sigma_bat = pm.Exponential("sigma_bat", 1.0)

            sigma_bowl = pm.Exponential("sigma_bowl", 1.0)

            batting = pm.ZeroSumNormal("batting", sigma=sigma_bat, shape=n_teams)

            bowling = pm.ZeroSumNormal("bowling", sigma=sigma_bowl, shape=n_teams)



            mu_total = pm.Deterministic(

                "mu_total",

                pt.exp(intercept + batting[bat_idx] - bowling[bowl_idx]),

                dims="innings",

            )

            sigma_total = pm.HalfNormal("sigma_total", sigma=40.0)



            pm.Gamma(

                "total",

                mu=mu_total,

                sigma=sigma_total,

                observed=totals,

                dims="innings",

            )



            idata = pm.sample(**self._sampler.to_sample_kwargs())



        return idata, index, model



    def _matchup_mu(

        self, batting: str | None, bowling: str | None, simulations: int

    ) -> tuple[np.ndarray, np.ndarray]:

        """Return posterior draws of ``(mu_total, sigma_total)`` for a matchup.



        Omitting both team names yields the league-average innings, which is

        the correct fallback when a live feed has not yet resolved lineups.

        """

        intercept = self._flat_posterior("intercept")

        sigma = self._flat_posterior("sigma_total")

        picks = self._draw_indices(intercept.shape[0], simulations)



        log_mu = intercept[picks]

        if batting is not None:

            log_mu = log_mu + self._flat_posterior("batting")[picks, self._resolve(batting)]

        if bowling is not None:

            log_mu = log_mu - self._flat_posterior("bowling")[picks, self._resolve(bowling)]

        return np.exp(log_mu), sigma[picks]



    def update_live(

        self,

        current_runs: float,

        wickets: int,

        overs: float,

        *,

        batting: str | None = None,

        bowling: str | None = None,

        simulations: int | None = None,

    ) -> dict[str, float]:

        """Update the projected-total posterior from a live innings state.



        Parameters

        ----------

        current_runs:

            Runs scored so far in the innings.

        wickets:

            Wickets lost so far.

        overs:

            Overs *completed* so far.



        The update is a self-normalised importance reweighting of the prior

        posterior draws. Each draw proposes a full-innings mean; the observed

        ``current_runs`` is scored against the Gamma implied by the resources

        already consumed, and draws that explain the current state well gain

        weight. This is exact Bayesian conditioning up to Monte Carlo error,

        and unlike a point rescale it narrows the interval as the innings

        progresses. Effective sample size is reported so a degenerate update

        (all weight on a handful of draws) is visible to the caller rather

        than silently producing an overconfident projection.



        Returns

        -------

        dict[str, float]

            Projected total median/mean, an 80% credible interval, the method

            of moments Gamma ``alpha``/``beta`` of the updated projection,

            resource accounting, and the importance-sampling ESS.

        """

        self._require_trained()

        if current_runs < 0:

            raise ModelUpdateError(

                "current_runs cannot be negative", code="MALFORMED_INPUT"

            )

        if not 0 <= wickets <= self._curve.total_wickets:

            raise ModelUpdateError(

                f"wickets must lie in [0, {self._curve.total_wickets}]",

                code="MALFORMED_INPUT",

            )

        if not 0.0 <= overs <= self._curve.total_overs:

            raise ModelUpdateError(

                f"overs must lie in [0, {self._curve.total_overs}]",

                code="MALFORMED_INPUT",

            )



        draws = int(simulations or self._simulations)

        overs_remaining = self._curve.total_overs - float(overs)

        fraction_remaining = self._curve.fraction_remaining(overs_remaining, wickets)

        fraction_used = 1.0 - fraction_remaining



        mu, sigma = self._matchup_mu(batting, bowling, draws)



        if fraction_used > 1e-6 and current_runs > 0:

            expected_so_far = mu * fraction_used

            sd_so_far = sigma * math.sqrt(fraction_used)

            log_weights = self._gamma_logpdf(

                float(current_runs), expected_so_far, sd_so_far

            )

            log_weights -= log_weights.max()

            weights = np.exp(log_weights)

            total_weight = weights.sum()

            if total_weight <= 0 or not np.isfinite(total_weight):

                raise ModelUpdateError(

                    "live state is incompatible with the fitted posterior; "

                    "every importance weight underflowed",

                    code="DEGENERATE_POSTERIOR",

                    diagnostics={"current_runs": current_runs, "overs": overs},

                )

            weights /= total_weight

            ess = float(1.0 / np.sum(weights**2))

        else:

            weights = np.full(draws, 1.0 / draws)

            ess = float(draws)



        resample = self._rng.choice(draws, size=draws, replace=True, p=weights)

        remaining_mean = mu[resample] * fraction_remaining

        remaining_sd = sigma[resample] * math.sqrt(max(fraction_remaining, 0.0))



        if fraction_remaining <= 1e-9:

            projected = np.full(draws, float(current_runs))

        else:

            shape, scale = self._gamma_shape_scale(remaining_mean, remaining_sd)

            projected = float(current_runs) + self._rng.gamma(shape, scale)



        alpha, beta = self._fit_gamma_moments(projected)

        return {

            "projected_total_median": float(np.median(projected)),

            "projected_total_mean": float(projected.mean()),

            "projected_total_p10": float(np.percentile(projected, 10.0)),

            "projected_total_p90": float(np.percentile(projected, 90.0)),

            "current_run_rate": (

                float(current_runs) / float(overs) if overs > 0 else 0.0

            ),

            "resources_remaining": fraction_remaining,

            "resources_used": fraction_used,

            "gamma_alpha": alpha,

            "gamma_beta": beta,

            "effective_sample_size": ess,

        }



    def predict_odds(self, **kwargs: Any) -> dict[str, float]:

        """Price the innings, optionally against a chase target.



        Parameters

        ----------

        batting, bowling:

            Optional team names; omitted means league-average.

        target:

            Optional runs required to win. When supplied, the batting side

            wins by reaching ``target`` (i.e. total >= target).

        current_runs, wickets, overs:

            Optional live state. When present, the projection is conditioned

            through :meth:`update_live` before pricing.

        """

        self._require_trained()

        simulations = int(kwargs.get("simulations", self._simulations))

        batting = kwargs.get("batting")

        bowling = kwargs.get("bowling")

        target = kwargs.get("target")



        if "current_runs" in kwargs:

            live = self.update_live(

                float(kwargs["current_runs"]),

                int(kwargs.get("wickets", 0)),

                float(kwargs.get("overs", 0.0)),

                batting=batting,

                bowling=bowling,

                simulations=simulations,

            )

            centre = live["projected_total_mean"]

            spread = max(

                (live["projected_total_p90"] - live["projected_total_p10"]) / 2.563,

                1e-6,

            )

            shape, scale = self._gamma_shape_scale(

                np.full(simulations, centre), np.full(simulations, spread)

            )

            totals = self._rng.gamma(shape, scale)

        else:

            mu, sigma = self._matchup_mu(batting, bowling, simulations)

            shape, scale = self._gamma_shape_scale(mu, sigma)

            totals = self._rng.gamma(shape, scale)



        result: dict[str, float] = {

            "expected_total_median": float(np.median(totals)),

            "expected_total_mean": float(totals.mean()),

            "total_p10": float(np.percentile(totals, 10.0)),

            "total_p90": float(np.percentile(totals, 90.0)),

        }

        if target is not None:

            reached = float(np.count_nonzero(totals >= float(target)) / simulations)

            result["batting_win"] = reached

            result["bowling_win"] = 1.0 - reached

        return result



    def calibrate_resource_curve(self, states: pl.DataFrame) -> ResourceCurve:

        """Refit the curve hyperparameters from observed partial-innings states.



        Expects columns ``overs_remaining``, ``wickets_lost`` and

        ``fraction_scored`` (runs added after the state divided by the final

        total). A coarse grid search minimises squared error against the

        observed fractions. This is deliberately a grid search rather than

        gradient descent: the surface is low-dimensional and non-convex in

        ``delta``, and a reproducible grid is easier to audit than a solver

        that lands somewhere different on every refit.

        """

        required = {"overs_remaining", "wickets_lost", "fraction_scored"}

        missing = sorted(required - set(states.columns))

        if missing:

            raise ModelUpdateError(

                f"calibration frame is missing column(s): {', '.join(missing)}",

                code="SCHEMA_MISMATCH",

            )

        if states.height < 50:

            raise ModelUpdateError(

                f"need at least 50 partial-innings states to calibrate, "

                f"got {states.height}",

                code="INSUFFICIENT_DATA",

            )



        overs = states["overs_remaining"].cast(pl.Float64).to_numpy()

        wickets = states["wickets_lost"].cast(pl.Int32).to_numpy()

        observed = states["fraction_scored"].cast(pl.Float64).to_numpy()



        best: ResourceCurve = self._curve

        best_error = float("inf")

        for b0 in np.linspace(0.05, 0.30, 26):

            for gamma in np.linspace(0.3, 1.5, 25):

                for delta in np.linspace(0.0, 0.20, 21):

                    candidate = ResourceCurve(

                        total_overs=self._curve.total_overs,

                        total_wickets=self._curve.total_wickets,

                        r0=self._curve.r0,

                        b0=float(b0),

                        gamma=float(gamma),

                        delta=float(delta),

                    )

                    predicted = np.array(

                        [

                            candidate.fraction_remaining(float(u), int(w))

                            for u, w in zip(overs, wickets)

                        ]

                    )

                    error = float(np.mean((predicted - observed) ** 2))

                    if error < best_error:

                        best_error = error

                        best = candidate



        _LOG.info(

            "calibrated resource curve: b0=%.4f gamma=%.3f delta=%.3f (mse=%.6f)",

            best.b0,

            best.gamma,

            best.delta,

            best_error,

        )

        self._curve = best

        return best



    @staticmethod

    def _gamma_shape_scale(

        mean: np.ndarray, sd: np.ndarray

    ) -> tuple[np.ndarray, np.ndarray]:

        """Convert mean/sd to Gamma shape/scale, clamped away from zero."""

        safe_mean = np.maximum(mean, 1e-6)

        safe_sd = np.maximum(sd, 1e-6)

        shape = (safe_mean / safe_sd) ** 2

        scale = safe_sd**2 / safe_mean

        return np.maximum(shape, 1e-6), np.maximum(scale, 1e-9)



    @classmethod

    def _gamma_logpdf(

        cls, value: float, mean: np.ndarray, sd: np.ndarray

    ) -> np.ndarray:

        """Log density of a mean/sd-parameterised Gamma at a scalar ``value``."""

        shape, scale = cls._gamma_shape_scale(mean, sd)

        x = max(value, 1e-9)

        return (

            (shape - 1.0) * math.log(x)

            - x / scale

            - gammaln(shape)

            - shape * np.log(scale)

        )



    @staticmethod

    def _fit_gamma_moments(samples: np.ndarray) -> tuple[float, float]:

        """Method of moments Gamma fit, returning ``(alpha, beta)``."""

        mean = float(samples.mean())

        variance = float(samples.var())

        if variance <= 0 or mean <= 0:

            return 0.0, 0.0

        alpha = mean**2 / variance

        beta = mean / variance

        return alpha, beta


