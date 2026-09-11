
"""Bayesian modeling domain for BetDoc.



Exposes the five mathematical archetypes, the routing facade

(:class:`InferenceEngine`), the archetype taxonomy (:class:`SportArchetype`),

and the domain error raised on sampler divergence or schema violation

(:class:`ModelUpdateError`).



Archetypes are deliberately sport-agnostic: a sport is a *configuration* of an

archetype, never a subclass. Adding Formula E or county cricket requires a

routing entry, not a new model.

"""



from __future__ import annotations

from betdoc.domain.modeling.archetypes.base import BayesianArchetype
from betdoc.domain.modeling.archetypes.bradley_terry_h2h import BradleyTerryArchetype
from betdoc.domain.modeling.archetypes.dynamic_cricket import DynamicCricketArchetype
from betdoc.domain.modeling.archetypes.gaussian_spread import GaussianSpreadArchetype
from betdoc.domain.modeling.archetypes.plackett_luce_racing import (
    PlackettLuceArchetype,
)
from betdoc.domain.modeling.archetypes.poisson_discrete import PoissonDiscreteArchetype
from betdoc.domain.modeling.inference_engine import EdgeAssessment, InferenceEngine
from betdoc.domain.modeling.types import (
    MATCH_SCHEMA,
    RACE_SCHEMA,
    ModelUpdateError,
    SamplerConfig,
    SportArchetype,
    TrainingDiagnostics,
)

__all__ = [

    "MATCH_SCHEMA",

    "RACE_SCHEMA",

    "BayesianArchetype",

    "BradleyTerryArchetype",

    "DynamicCricketArchetype",

    "EdgeAssessment",

    "GaussianSpreadArchetype",

    "InferenceEngine",

    "ModelUpdateError",

    "PlackettLuceArchetype",

    "PoissonDiscreteArchetype",

    "SamplerConfig",

    "SportArchetype",

    "TrainingDiagnostics",

]


