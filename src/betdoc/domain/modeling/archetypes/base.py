
"""Abstract base for all Bayesian archetypes.



Sampling is CPU-bound and blocks for seconds to minutes, so :meth:`train`

offloads the blocking fit to a worker thread and serialises concurrent

training attempts behind a lock. Posterior objects are replaced atomically:

a failed refit leaves the previously-good posterior intact and priceable.

"""



from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Final

import arviz as az
import numpy as np
import polars as pl
import pymc as pm

from betdoc.domain.modeling.types import (
    ModelUpdateError,
    SamplerConfig,
    SportArchetype,
    TrainingDiagnostics,
)

__all__ = ["BayesianArchetype"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)





class BayesianArchetype(ABC):

    """Base class holding the fit/predict lifecycle shared by all archetypes."""



    #: Overridden by each concrete subclass.

    archetype: SportArchetype



    def __init__(

        self,

        *,

        sampler: SamplerConfig | None = None,

        name: str | None = None,

    ) -> None:

        if not hasattr(self, "archetype"):

            raise ModelUpdateError(

                f"{type(self).__name__} must declare a class-level 'archetype'",

                code="MALFORMED_CONFIG",

            )

        self._sampler: SamplerConfig = sampler or SamplerConfig()

        self._name: str = name or type(self).__name__

        self._idata: az.InferenceData | None = None

        self._model: pm.Model | None = None

        self._index: dict[str, int] = {}

        self._diagnostics: TrainingDiagnostics | None = None

        self._lock: asyncio.Lock = asyncio.Lock()

        self._rng: np.random.Generator = np.random.default_rng(

            self._sampler.random_seed

        )



    @property

    def name(self) -> str:

        """Human readable model name used in logs and the model registry."""

        return self._name



    @property

    def is_trained(self) -> bool:

        """Return True if the model has a fitted posterior trace."""

        return self._idata is not None



    def load_posterior(self, idata: az.InferenceData, index: dict[str, int]) -> None:

        """Load a pre-trained posterior and participant index."""

        self._idata = idata

        self._index = dict(index)



    def export_posterior(self) -> tuple[az.InferenceData | None, dict[str, int]]:

        """Return the current posterior and participant index for serialization."""

        return self._idata, dict(self._index)



    @property

    def participants(self) -> tuple[str, ...]:

        """Participants in posterior index order."""

        return tuple(

            sorted(self._index, key=lambda participant: self._index[participant])

        )



    @property

    def diagnostics(self) -> TrainingDiagnostics | None:

        """Telemetry from the most recent successful fit."""

        return self._diagnostics



    @property

    def posterior(self) -> az.InferenceData:

        """The held posterior.



        Raises

        ------

        ModelUpdateError

            ``NOT_TRAINED`` if no fit has succeeded yet.

        """

        self._require_trained()

        assert self._idata is not None  # narrowed by _require_trained

        return self._idata



    async def train(self, data: pl.DataFrame) -> None:

        """Fit the archetype to ``data`` without blocking the event loop.



        The blocking NUTS run is dispatched to a worker thread. Concurrent

        callers are serialised, so a burst of refit requests for the same

        archetype collapses into sequential fits rather than oversubscribing

        every core on the box.



        Raises

        ------

        ModelUpdateError

            ``SCHEMA_MISMATCH`` / ``INSUFFICIENT_DATA`` from validation, or

            ``DIVERGENT`` when the resulting fit breaches the configured

            divergence or R-hat thresholds.

        """

        async with self._lock:

            started = time.perf_counter()

            try:

                idata, index, model = await asyncio.to_thread(self._fit, data)

            except ModelUpdateError:

                raise

            except Exception as error:

                raise ModelUpdateError(

                    f"{self._name} sampling failed: {error}",

                    code="SAMPLING_FAILED",

                ) from error



            duration = time.perf_counter() - started

            diagnostics = self._evaluate(

                idata,

                observations=data.height,

                participants=len(index),

                duration_seconds=duration,

            )

            self._assert_healthy(diagnostics)



            # Atomic swap: only a healthy fit is allowed to replace the old one.

            self._idata = idata

            self._index = index

            self._model = model

            self._diagnostics = diagnostics

            _LOG.info(

                "%s trained on %d observations in %.2fs (divergences=%d, r_hat=%.4f)",

                self._name,

                data.height,

                duration,

                diagnostics.divergences,

                diagnostics.max_r_hat,

            )



    @abstractmethod

    def _fit(

        self, data: pl.DataFrame

    ) -> tuple[az.InferenceData, dict[str, int], pm.Model]:

        """Blocking fit. Returns the posterior, participant index, and model.



        Implementations validate their own frame against the appropriate

        schema, build the PyMC model, and call ``pm.sample``. This method runs

        on a worker thread and must not touch the event loop.

        """



    @abstractmethod

    def predict_odds(self, **kwargs: Any) -> dict[str, float]:

        """Extract calibrated outcome probabilities from the posterior."""



    def _evaluate(

        self,

        idata: az.InferenceData,

        *,

        observations: int,

        participants: int,

        duration_seconds: float,

    ) -> TrainingDiagnostics:

        """Summarise sampler health into :class:`TrainingDiagnostics`."""

        warnings: list[str] = []

        divergences = 0

        total_draws = 0



        if hasattr(idata, "sample_stats") and "diverging" in idata.sample_stats:

            diverging = idata.sample_stats["diverging"].values

            divergences = int(np.count_nonzero(diverging))

            total_draws = int(np.asarray(diverging).size)

        else:

            warnings.append("sampler did not report divergence statistics")



        max_r_hat = 1.0

        min_ess = float("inf")

        try:

            summary = az.summary(idata, kind="diagnostics")

            if "r_hat" in summary and summary["r_hat"].notna().any():

                max_r_hat = float(np.nanmax(summary["r_hat"].to_numpy()))

            if "ess_bulk" in summary and summary["ess_bulk"].notna().any():

                min_ess = float(np.nanmin(summary["ess_bulk"].to_numpy()))

        except Exception as error:

            warnings.append(f"convergence summary unavailable: {error}")



        if min_ess == float("inf"):

            min_ess = 0.0

            warnings.append("effective sample size could not be computed")



        return TrainingDiagnostics(

            archetype=self.archetype,

            observations=observations,

            participants=participants,

            divergences=divergences,

            total_draws=total_draws,

            max_r_hat=max_r_hat,

            min_ess=min_ess,

            duration_seconds=duration_seconds,

            warnings=tuple(warnings),

        )



    def _assert_healthy(self, diagnostics: TrainingDiagnostics) -> None:

        """Reject a fit whose geometry is too poor to price against.



        A divergent posterior is not a slightly-worse posterior; it is a

        biased sample of the wrong distribution. Pricing real money against

        it is strictly worse than declining to quote.

        """

        if diagnostics.divergence_ratio > self._sampler.max_divergence_ratio:

            raise ModelUpdateError(

                f"{self._name} produced {diagnostics.divergences} divergent "

                f"transitions ({diagnostics.divergence_ratio:.2%} of draws), "

                f"exceeding the {self._sampler.max_divergence_ratio:.2%} threshold",

                code="DIVERGENT",

                diagnostics=diagnostics.to_dict(),

            )

        if diagnostics.max_r_hat > self._sampler.max_r_hat:

            raise ModelUpdateError(

                f"{self._name} chains failed to mix: max R-hat "

                f"{diagnostics.max_r_hat:.4f} > {self._sampler.max_r_hat}",

                code="DIVERGENT",

                diagnostics=diagnostics.to_dict(),

            )



    def _require_trained(self) -> None:

        """Guard predict-time access to the posterior."""

        if self._idata is None:

            raise ModelUpdateError(

                f"{self._name} has no posterior; call train() first",

                code="NOT_TRAINED",

            )



    def _build_index(self, *columns: pl.Series) -> dict[str, int]:

        """Build a stable participant to integer-code mapping.



        Sorted order makes posterior coordinates reproducible across refits,

        which matters when diffing model versions in the registry.

        """

        names: set[str] = set()

        for column in columns:

            names.update(column.drop_nulls().unique().to_list())

        if not names:

            raise ModelUpdateError(

                "no participants present in the training frame",

                code="INSUFFICIENT_DATA",

            )

        return {name: code for code, name in enumerate(sorted(names))}



    def _codes(self, column: pl.Series, index: Mapping[str, int]) -> np.ndarray:

        """Vectorised participant-name to integer-code translation."""

        mapped = column.replace_strict(

            old=list(index.keys()),

            new=list(index.values()),

            return_dtype=pl.Int32,

            default=None,

        )

        if mapped.null_count():

            unknown = (

                column.filter(mapped.is_null()).unique().to_list()[:5]

            )

            raise ModelUpdateError(

                f"frame contains participants absent from the index: {unknown}",

                code="UNKNOWN_PARTICIPANT",

            )

        return mapped.to_numpy().astype(np.int32, copy=False)



    def _resolve(self, participant: str) -> int:

        """Look up a single participant's posterior index.



        Raises

        ------

        ModelUpdateError

            ``UNKNOWN_PARTICIPANT`` for a participant the model never saw.

            A debutant has no posterior skill, and substituting the field

            average silently would understate uncertainty.

        """

        try:

            return self._index[participant]

        except KeyError as error:

            raise ModelUpdateError(

                f"{participant!r} was not present in the training data; "

                f"no posterior skill exists for this participant",

                code="UNKNOWN_PARTICIPANT",

                diagnostics={"known_participants": len(self._index)},

            ) from error



    def _flat_posterior(self, variable: str) -> np.ndarray:

        """Return a chain-flattened posterior array for ``variable``."""

        self._require_trained()

        assert self._idata is not None

        posterior = self._idata.posterior

        if variable not in posterior:

            raise ModelUpdateError(

                f"posterior has no variable {variable!r}",

                code="NOT_TRAINED",

                diagnostics={"available": list(posterior.data_vars)},

            )

        values = posterior[variable].values

        return values.reshape((-1,) + values.shape[2:])



    def _draw_indices(self, population: int, draws: int) -> np.ndarray:

        """Sample ``draws`` posterior row indices with replacement."""

        if population <= 0:

            raise ModelUpdateError(

                "posterior is empty", code="NOT_TRAINED"

            )

        return self._rng.integers(0, population, size=draws)



    @staticmethod

    def _normalise(probabilities: Sequence[float]) -> list[float]:

        """Renormalise a probability vector, guarding against zero mass."""

        total = float(sum(probabilities))

        if total <= 0.0:

            raise ModelUpdateError(

                "simulated outcome probabilities summed to zero",

                code="DEGENERATE_POSTERIOR",

            )

        return [float(value) / total for value in probabilities]



    def __repr__(self) -> str:

        return (

            f"{type(self).__name__}(archetype={self.archetype.value}, "

            f"trained={self.is_trained}, participants={len(self._index)})"

        )


