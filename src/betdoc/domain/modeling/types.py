
"""Core schemas, enums, and errors for the Bayesian modeling domain.



Every frame entering an archetype is validated against a strict Polars schema

before a single tensor is constructed. Silent dtype coercion is the most

common cause of a model that samples cleanly and prices nonsense, so schema

violations are hard failures here.

"""



from __future__ import annotations



from dataclasses import dataclass, field

from enum import Enum

from typing import Any, Final, Mapping, Sequence



import polars as pl



__all__ = [

    "MATCH_SCHEMA",

    "RACE_SCHEMA",

    "ModelUpdateError",

    "SamplerConfig",

    "SportArchetype",

    "TrainingDiagnostics",

    "validate_frame",

]





class SportArchetype(str, Enum):

    """Mathematical family a sport's scoring process belongs to.



    Membership is determined by the *generative process*, not by the sport's

    cultural category. Ice hockey and football share ``POISSON_DISCRETE``

    because both are low-count independent-ish scoring events; basketball is

    ``GAUSSIAN_SPREAD`` because its high-count scoring is well approximated by

    a continuous margin.

    """



    POISSON_DISCRETE = "POISSON_DISCRETE"

    GAUSSIAN_SPREAD = "GAUSSIAN_SPREAD"

    BRADLEY_TERRY_H2H = "BRADLEY_TERRY_H2H"

    PLACKETT_LUCE_RACING = "PLACKETT_LUCE_RACING"

    DYNAMIC_ACCUMULATION = "DYNAMIC_ACCUMULATION"



    @property

    def is_ranked_field(self) -> bool:

        """Whether this archetype prices a multi-participant field."""

        return self is SportArchetype.PLACKETT_LUCE_RACING



    @property

    def supports_draw(self) -> bool:

        """Whether the archetype can emit a non-zero draw probability."""

        return self in {

            SportArchetype.POISSON_DISCRETE,

            SportArchetype.GAUSSIAN_SPREAD,

        }



    @classmethod

    def for_sport(cls, sport: str) -> "SportArchetype":

        """Route a free-text sport name to its archetype.



        Raises

        ------

        ModelUpdateError

            ``UNKNOWN_SPORT`` when the sport has no registered archetype.

            Guessing here would price a market with the wrong generative

            model, which is worse than refusing to price it.

        """

        key = sport.strip().upper().replace(" ", "_").replace("-", "_")

        try:

            return _SPORT_ROUTING[key]

        except KeyError as error:

            raise ModelUpdateError(

                f"no archetype registered for sport {sport!r}; "

                f"register it in _SPORT_ROUTING before pricing",

                code="UNKNOWN_SPORT",

            ) from error





_SPORT_ROUTING: Final[dict[str, SportArchetype]] = {

    # Low-count discrete scoring.

    "FOOTBALL": SportArchetype.POISSON_DISCRETE,

    "SOCCER": SportArchetype.POISSON_DISCRETE,

    "ICE_HOCKEY": SportArchetype.POISSON_DISCRETE,

    "HOCKEY": SportArchetype.POISSON_DISCRETE,

    "HANDBALL": SportArchetype.POISSON_DISCRETE,

    "WATER_POLO": SportArchetype.POISSON_DISCRETE,

    # High-count continuous margin.

    "BASKETBALL": SportArchetype.GAUSSIAN_SPREAD,

    "NBA": SportArchetype.GAUSSIAN_SPREAD,

    "NFL": SportArchetype.GAUSSIAN_SPREAD,

    "AMERICAN_FOOTBALL": SportArchetype.GAUSSIAN_SPREAD,

    "RUGBY": SportArchetype.GAUSSIAN_SPREAD,

    "RUGBY_UNION": SportArchetype.GAUSSIAN_SPREAD,

    "AUSSIE_RULES": SportArchetype.GAUSSIAN_SPREAD,

    # Pairwise comparison, no draw.

    "TENNIS": SportArchetype.BRADLEY_TERRY_H2H,

    "MMA": SportArchetype.BRADLEY_TERRY_H2H,

    "UFC": SportArchetype.BRADLEY_TERRY_H2H,

    "BOXING": SportArchetype.BRADLEY_TERRY_H2H,

    "ESPORTS": SportArchetype.BRADLEY_TERRY_H2H,

    "DARTS": SportArchetype.BRADLEY_TERRY_H2H,

    "SNOOKER": SportArchetype.BRADLEY_TERRY_H2H,

    "TABLE_TENNIS": SportArchetype.BRADLEY_TERRY_H2H,

    # Rank-ordered fields.

    "FORMULA_1": SportArchetype.PLACKETT_LUCE_RACING,

    "F1": SportArchetype.PLACKETT_LUCE_RACING,

    "NASCAR": SportArchetype.PLACKETT_LUCE_RACING,

    "MOTOGP": SportArchetype.PLACKETT_LUCE_RACING,

    "HORSE_RACING": SportArchetype.PLACKETT_LUCE_RACING,

    "GREYHOUNDS": SportArchetype.PLACKETT_LUCE_RACING,

    "CYCLING": SportArchetype.PLACKETT_LUCE_RACING,

    "GOLF": SportArchetype.PLACKETT_LUCE_RACING,

    # Resource-depletion accumulation.

    "CRICKET": SportArchetype.DYNAMIC_ACCUMULATION,

    "T20": SportArchetype.DYNAMIC_ACCUMULATION,

    "T20_CRICKET": SportArchetype.DYNAMIC_ACCUMULATION,

    "ODI": SportArchetype.DYNAMIC_ACCUMULATION,

    "BASEBALL": SportArchetype.DYNAMIC_ACCUMULATION,

}





class ModelUpdateError(RuntimeError):

    """Raised on schema violation, sampler divergence, or unpriceable input.



    Parameters

    ----------

    message:

        Operator-facing description.

    code:

        Stable machine readable code, e.g. ``SCHEMA_MISMATCH``,

        ``DIVERGENT``, ``NOT_TRAINED``, ``UNKNOWN_PARTICIPANT``,

        ``INSUFFICIENT_DATA``, ``UNKNOWN_SPORT``.

    diagnostics:

        Structured sampler telemetry, when the failure originated in MCMC.

    """



    __slots__ = ("code", "diagnostics")



    def __init__(

        self,

        message: str,

        *,

        code: str = "MODEL_UPDATE_ERROR",

        diagnostics: Mapping[str, Any] | None = None,

    ) -> None:

        super().__init__(message)

        self.code: str = code

        self.diagnostics: dict[str, Any] = dict(diagnostics or {})



    def __str__(self) -> str:

        return f"[{self.code}] {super().__str__()}"





# Standard two-participant match ingestion schema.

MATCH_SCHEMA: Final[pl.Schema] = pl.Schema(

    {

        "match_id": pl.Utf8,

        "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),

        "participant_a": pl.Utf8,

        "participant_b": pl.Utf8,

        "score_a": pl.Int32,

        "score_b": pl.Int32,

    }

)



# Rank-ordered field ingestion schema for the Plackett-Luce archetype.

RACE_SCHEMA: Final[pl.Schema] = pl.Schema(

    {

        "race_id": pl.Utf8,

        "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),

        "participant": pl.Utf8,

        "finish_position": pl.Int32,

    }

)





@dataclass(frozen=True, slots=True)

class SamplerConfig:

    """NUTS sampler configuration.



    ``cores`` defaults to 1 because :meth:`BayesianArchetype.train` runs the

    sampler inside a worker thread via :func:`asyncio.to_thread`. Spawning

    PyMC's multiprocess backend from a non-main thread is fragile across

    platforms (notably macOS spawn semantics), so parallel chains are opt-in

    and should only be enabled when training from the main thread.

    """



    draws: int = 2000

    tune: int = 1000

    chains: int = 4

    cores: int = 1

    target_accept: float = 0.9

    random_seed: int | None = 20240601

    progressbar: bool = False

    max_divergence_ratio: float = 0.01

    max_r_hat: float = 1.01



    def __post_init__(self) -> None:

        if self.draws < 1:

            raise ModelUpdateError("draws must be >= 1", code="MALFORMED_CONFIG")

        if self.tune < 0:

            raise ModelUpdateError("tune must be >= 0", code="MALFORMED_CONFIG")

        if self.chains < 1:

            raise ModelUpdateError("chains must be >= 1", code="MALFORMED_CONFIG")

        if not 0.0 < self.target_accept < 1.0:

            raise ModelUpdateError(

                "target_accept must lie in (0, 1)", code="MALFORMED_CONFIG"

            )



    def to_sample_kwargs(self) -> dict[str, Any]:

        """Project onto the keyword arguments accepted by ``pm.sample``."""

        return {

            "draws": self.draws,

            "tune": self.tune,

            "chains": self.chains,

            "cores": self.cores,

            "target_accept": self.target_accept,

            "random_seed": self.random_seed,

            "progressbar": self.progressbar,

            "return_inferencedata": True,

        }





@dataclass(frozen=True, slots=True)

class TrainingDiagnostics:

    """Post-sampling health telemetry for a single fit."""



    archetype: SportArchetype

    observations: int

    participants: int

    divergences: int

    total_draws: int

    max_r_hat: float

    min_ess: float

    duration_seconds: float

    warnings: Sequence[str] = field(default_factory=tuple)



    @property

    def divergence_ratio(self) -> float:

        """Fraction of post-warmup draws that diverged."""

        if self.total_draws <= 0:

            return 0.0

        return self.divergences / self.total_draws



    @property

    def is_healthy(self) -> bool:

        """Whether the fit is fit to price with, ignoring thresholds."""

        return self.divergences == 0 and self.max_r_hat <= 1.01



    def to_dict(self) -> dict[str, Any]:

        """Return a JSON-safe projection for model-registry logging."""

        return {

            "archetype": self.archetype.value,

            "observations": self.observations,

            "participants": self.participants,

            "divergences": self.divergences,

            "total_draws": self.total_draws,

            "divergence_ratio": round(self.divergence_ratio, 6),

            "max_r_hat": round(self.max_r_hat, 5),

            "min_ess": round(self.min_ess, 2),

            "duration_seconds": round(self.duration_seconds, 3),

            "warnings": list(self.warnings),

        }





def validate_frame(

    data: pl.DataFrame,

    schema: pl.Schema,

    *,

    min_rows: int = 1,

    drop_nulls: bool = True,

) -> pl.DataFrame:

    """Validate and normalise ``data`` against ``schema``.



    Columns are selected in schema order and cast strictly, so a string in a

    score column raises rather than silently becoming null. Extra columns are

    dropped, not rejected, which lets upstream feeds add fields freely.



    Raises

    ------

    ModelUpdateError

        ``SCHEMA_MISMATCH`` on a missing column or failed cast,

        ``INSUFFICIENT_DATA`` when fewer than ``min_rows`` usable rows remain.

    """

    if not isinstance(data, pl.DataFrame):

        raise ModelUpdateError(

            f"expected a polars DataFrame, received {type(data).__name__}",

            code="SCHEMA_MISMATCH",

        )



    missing = [name for name in schema.names() if name not in data.columns]

    if missing:

        raise ModelUpdateError(

            f"frame is missing required column(s): {', '.join(missing)}",

            code="SCHEMA_MISMATCH",

            diagnostics={"missing": missing, "received": list(data.columns)},

        )



    try:

        frame = data.select(

            [pl.col(name).cast(dtype, strict=True) for name, dtype in schema.items()]

        )

    except (pl.exceptions.InvalidOperationError, pl.exceptions.ComputeError) as error:

        raise ModelUpdateError(

            f"frame failed strict cast to the target schema: {error}",

            code="SCHEMA_MISMATCH",

            diagnostics={"expected": {k: str(v) for k, v in schema.items()}},

        ) from error



    if drop_nulls:

        before = frame.height

        frame = frame.drop_nulls()

        dropped = before - frame.height

        if dropped:

            frame = frame.with_columns()  # no-op; retains eager materialisation

    if frame.height < min_rows:

        raise ModelUpdateError(

            f"need at least {min_rows} complete row(s) to fit, got {frame.height}",

            code="INSUFFICIENT_DATA",

            diagnostics={"rows": frame.height, "required": min_rows},

        )

    return frame


