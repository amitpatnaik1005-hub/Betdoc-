
"""Plackett-Luce archetype for rank-ordered multi-participant fields.



Target sports: Formula 1, MotoGP, NASCAR, horse racing, greyhounds, cycling,

stroke-play golf.



Each participant has an exponentiated latent strength

:math:`\\lambda_i = e^{\\theta_i}`. The probability that participant

:math:`i` wins from a field :math:`D` is



.. math::



    P(i \\text{ first}) = \\frac{\\lambda_i}{\\sum_{j \\in D} \\lambda_j}



The full rank-ordering likelihood is the product of that choice probability

applied sequentially to the shrinking remaining field, so an observed finish

order :math:`\\pi_1, \\pi_2, \\ldots` contributes



.. math::



    \\log L = \\sum_{k=1}^{K} \\left[ \\theta_{\\pi_k}

              - \\log \\sum_{j \\in D_k} e^{\\theta_j} \\right]



where :math:`D_k` is the field still unfinished at stage :math:`k`. This has

no distribution in PyMC, so it is assembled as a masked ``logsumexp`` over a

padded field axis and attached with :func:`pm.Potential`.



Only the first ``depth`` stages are evaluated (default 3, i.e. the podium).

Back-of-the-grid orderings are dominated by mechanical failure and traffic

rather than pace, so including them injects noise into the strength estimates

while adding almost nothing to win-market accuracy.

"""



from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

import arviz as az
import numpy as np
import polars as pl
import pymc as pm
import pytensor.tensor as pt

from betdoc.domain.modeling.archetypes.base import BayesianArchetype
from betdoc.domain.modeling.types import (
    RACE_SCHEMA,
    ModelUpdateError,
    SamplerConfig,
    SportArchetype,
    validate_frame,
)

__all__ = ["PlackettLuceArchetype"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_DEPTH: Final[int] = 3

DEFAULT_SIMULATIONS: Final[int] = 10_000

MIN_RACES: Final[int] = 5





class PlackettLuceArchetype(BayesianArchetype):

    """Rank-ordered choice model over variable-size competitive fields."""



    archetype = SportArchetype.PLACKETT_LUCE_RACING



    def __init__(

        self,

        *,

        sampler: SamplerConfig | None = None,

        name: str | None = None,

        depth: int = DEFAULT_DEPTH,

        simulations: int = DEFAULT_SIMULATIONS,

    ) -> None:

        super().__init__(sampler=sampler, name=name)

        if depth < 1:

            raise ModelUpdateError("depth must be >= 1", code="MALFORMED_CONFIG")

        self._depth: int = int(depth)

        self._simulations: int = int(simulations)

        self._races_fitted: int = 0



    @property

    def depth(self) -> int:

        """Number of finishing positions included in the likelihood."""

        return self._depth



    @property

    def races_fitted(self) -> int:

        """Number of races in the most recent fit."""

        return self._races_fitted



    def _fit(

        self, data: pl.DataFrame

    ) -> tuple[az.InferenceData, dict[str, int], pm.Model]:

        """Build and sample the Plackett-Luce model."""

        frame = validate_frame(data, RACE_SCHEMA, min_rows=MIN_RACES * 2)



        classified = frame.filter(pl.col("finish_position") >= 1)

        if classified.height < frame.height:

            _LOG.info(

                "%s ignored %d non-classified result(s) (DNF/DSQ)",

                self.name,

                frame.height - classified.height,

            )



        ordered = classified.sort(["race_id", "finish_position"])

        duplicated = (

            ordered.group_by(["race_id", "finish_position"])

            .len()

            .filter(pl.col("len") > 1)

        )

        if duplicated.height:

            raise ModelUpdateError(

                f"{duplicated.height} race/position pair(s) are duplicated; "

                f"dead heats must be resolved upstream before fitting",

                code="SCHEMA_MISMATCH",

            )



        grids = ordered.group_by("race_id", maintain_order=True).agg(

            pl.col("participant").alias("finishers")

        )

        if grids.height < MIN_RACES:

            raise ModelUpdateError(

                f"need at least {MIN_RACES} races, found {grids.height}",

                code="INSUFFICIENT_DATA",

            )



        index = self._build_index(classified["participant"])

        n_drivers = len(index)

        if n_drivers < 2:

            raise ModelUpdateError(

                f"need at least 2 distinct participants, found {n_drivers}",

                code="INSUFFICIENT_DATA",

            )



        codes, field_sizes = self._encode_grids(grids["finishers"].to_list(), index)

        self._races_fitted = int(codes.shape[0])

        depth = min(self._depth, int(field_sizes.max()))



        stage_mask, stage_valid = self._build_masks(field_sizes, codes.shape[1], depth)



        coords: dict[str, list[str]] = {

            "driver": sorted(index, key=lambda driver: index[driver]),

            "race": grids["race_id"].to_list(),

        }



        with pm.Model(coords=coords) as model:

            sigma_strength = pm.Exponential("sigma_strength", 1.0)

            strength = pm.ZeroSumNormal(

                "strength", sigma=sigma_strength, shape=n_drivers

            )



            pm.Potential(

                "plackett_luce_loglik",

                self._rank_ordered_logp(

                    strength, codes, stage_mask, stage_valid, depth

                ),

            )



            idata = pm.sample(**self._sampler.to_sample_kwargs())



        return idata, index, model



    @staticmethod

    def _encode_grids(

        grids: Sequence[Sequence[str]], index: dict[str, int]

    ) -> tuple[np.ndarray, np.ndarray]:

        """Encode ragged finish orders into a padded integer code matrix.



        Row ``r`` holds the participant codes for race ``r`` in finishing

        order, right-padded with code 0. Padding is never read because every

        reduction is masked by the true field size.

        """

        field_sizes = np.array([len(grid) for grid in grids], dtype=np.int64)

        max_field = int(field_sizes.max())

        codes = np.zeros((len(grids), max_field), dtype=np.int32)

        for row, grid in enumerate(grids):

            for slot, participant in enumerate(grid):

                codes[row, slot] = index[participant]

        return codes, field_sizes



    @staticmethod

    def _build_masks(

        field_sizes: np.ndarray, max_field: int, depth: int

    ) -> tuple[np.ndarray, np.ndarray]:

        """Precompute the remaining-field and stage-validity masks.



        ``stage_mask[r, k, s]`` is True when slot ``s`` is still in the field

        at stage ``k`` of race ``r``, i.e. the finisher has not yet been

        removed (``s >= k``) and the slot is a real entrant.

        ``stage_valid[r, k]`` is True when race ``r`` actually reaches stage

        ``k``, which keeps short fields from contributing phantom terms.

        """

        slots = np.arange(max_field, dtype=np.int64)[None, None, :]

        stages = np.arange(depth, dtype=np.int64)[None, :, None]

        sizes = field_sizes[:, None, None]

        stage_mask = (slots >= stages) & (slots < sizes)

        stage_valid = np.arange(depth, dtype=np.int64)[None, :] < field_sizes[:, None]

        return stage_mask, stage_valid



    @staticmethod

    def _rank_ordered_logp(

        strength: pt.TensorVariable,

        codes: np.ndarray,

        stage_mask: np.ndarray,

        stage_valid: np.ndarray,

        depth: int,

    ) -> pt.TensorVariable:

        """Total Plackett-Luce log-likelihood over all races and stages.



        The normaliser for each stage is a ``logsumexp`` restricted to the

        drivers still running, obtained by pushing excluded slots to

        ``-inf``. Every stage retains at least the finisher itself, so no

        reduction is taken over an empty set.

        """

        theta = strength[codes]  # (races, max_field)

        theta_stages = theta[:, None, :]  # (races, depth, max_field)

        masked = pt.switch(stage_mask, theta_stages, -np.inf)

        log_normaliser = pt.logsumexp(masked, axis=2)  # (races, depth)

        chosen = theta[:, :depth]

        stage_logp = pt.switch(stage_valid, chosen - log_normaliser, 0.0)

        return pt.sum(stage_logp)



    def predict_odds(self, **kwargs: Any) -> dict[str, float]:

        """Return win probability for every driver on a declared grid.



        Parameters

        ----------

        grid:

            List of participant names taking the start. Win probabilities are

            conditional on this exact field, which is the whole point of the

            archetype: removing the favourite redistributes their probability

            mass across the remaining runners rather than rescaling it.



        Returns

        -------

        dict[str, float]

            Participant name to win probability, summing to 1.0.

        """

        self._require_trained()

        grid = kwargs.get("grid")

        if not isinstance(grid, (list, tuple)) or len(grid) < 2:

            raise ModelUpdateError(

                "predict_odds requires 'grid' as a list of at least 2 participants",

                code="MALFORMED_INPUT",

            )

        if len(set(grid)) != len(grid):

            raise ModelUpdateError(

                "grid contains duplicate participants", code="MALFORMED_INPUT"

            )



        codes = np.array([self._resolve(str(name)) for name in grid], dtype=np.int32)

        strength = self._flat_posterior("strength")[:, codes]



        # Softmax per posterior draw, then average. Averaging strengths first

        # and exponentiating once would collapse the field's uncertainty and

        # over-price the favourite.

        shifted = strength - strength.max(axis=1, keepdims=True)

        weights = np.exp(shifted)

        probabilities = weights / weights.sum(axis=1, keepdims=True)

        mean_probabilities = probabilities.mean(axis=0)



        normalised = self._normalise(mean_probabilities.tolist())

        return {str(name): value for name, value in zip(grid, normalised)}



    def predict_podium(self, grid: Sequence[str], **kwargs: Any) -> dict[str, float]:

        """Return top-``depth`` finish probability for each driver on ``grid``.



        Podium probability is not a closed-form marginal of the Plackett-Luce

        model, so it is estimated by sequential sampling without replacement:

        draw a winner from the softmax, remove them, redraw, and repeat.

        """

        self._require_trained()

        if len(grid) < 2:

            raise ModelUpdateError(

                "grid must contain at least 2 participants", code="MALFORMED_INPUT"

            )



        simulations = int(kwargs.get("simulations", self._simulations))

        places = int(kwargs.get("places", min(self._depth, len(grid))))

        codes = np.array([self._resolve(str(name)) for name in grid], dtype=np.int32)



        strength = self._flat_posterior("strength")[:, codes]

        picks = self._draw_indices(strength.shape[0], simulations)

        sampled = strength[picks]  # (simulations, field)



        hits = np.zeros(len(grid), dtype=np.int64)

        available = np.ones_like(sampled, dtype=bool)

        for _ in range(places):

            masked = np.where(available, sampled, -np.inf)

            shifted = masked - masked.max(axis=1, keepdims=True)

            weights = np.exp(shifted)

            weights /= weights.sum(axis=1, keepdims=True)

            cumulative = weights.cumsum(axis=1)

            thresholds = self._rng.random((simulations, 1))

            chosen = (cumulative < thresholds).sum(axis=1)

            chosen = np.clip(chosen, 0, len(grid) - 1)

            hits += np.bincount(chosen, minlength=len(grid))

            available[np.arange(simulations), chosen] = False



        return {

            str(name): float(hits[position] / simulations)

            for position, name in enumerate(grid)

        }


