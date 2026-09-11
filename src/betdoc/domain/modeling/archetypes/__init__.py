
"""Mathematical archetypes for sport-agnostic Bayesian pricing.



Each module implements one generative process. Sports are routed onto these

by :class:`~betdoc.domain.modeling.types.SportArchetype`, so the archetype

count stays fixed as sport coverage grows.

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

__all__ = [

    "BayesianArchetype",

    "BradleyTerryArchetype",

    "DynamicCricketArchetype",

    "GaussianSpreadArchetype",

    "PlackettLuceArchetype",

    "PoissonDiscreteArchetype",

]


