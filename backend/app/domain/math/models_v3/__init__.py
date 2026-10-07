"""Smallcase Engine v3, Group 54 (Models 31-59): final registration of all 29 models."""

from app.domain.math.models_v2 import math_model_registry
from app.domain.math.models_v3.adaboost import AdaBoostConfig, AdaBoostModel
from app.domain.math.models_v3.black_scholes import BlackScholesConfig, BlackScholesModel
from app.domain.math.models_v3.dirichlet_process import DirichletProcessConfig, DirichletProcessModel
from app.domain.math.models_v3.dynamic_time_warping import DynamicTimeWarpingConfig, DynamicTimeWarpingModel
from app.domain.math.models_v3.elastic_net import ElasticNetConfig, ElasticNetModel
from app.domain.math.models_v3.extreme_value import ExtremeValueConfig, ExtremeValueModel
from app.domain.math.models_v3.fractional_brownian_motion import (
    FractionalBrownianMotionConfig,
    FractionalBrownianMotionModel,
)
from app.domain.math.models_v3.garch import GARCHConfig, GARCHModel
from app.domain.math.models_v3.gaussian_copula import GaussianCopulaConfig, GaussianCopulaModel
from app.domain.math.models_v3.gaussian_process import GaussianProcessConfig, GaussianProcessModel
from app.domain.math.models_v3.gradient_boosting import (
    GradientBoostingRegressionConfig,
    GradientBoostingRegressionModel,
)
from app.domain.math.models_v3.hawkes_process import HawkesConfig, HawkesProcessModel
from app.domain.math.models_v3.hmm import HMMConfig, HMMModel
from app.domain.math.models_v3.hsmm import HSMMConfig, HSMMModel
from app.domain.math.models_v3.kalman_filter import KalmanFilterConfig, KalmanFilterModel
from app.domain.math.models_v3.kernel_density import KernelDensityConfig, KernelDensityModel
from app.domain.math.models_v3.lasso_ridge import LassoRidgeConfig, LassoRidgeModel
from app.domain.math.models_v3.lstm_numpy import LSTMConfig, LSTMModel
from app.domain.math.models_v3.nash_equilibrium import NashEquilibriumConfig, NashEquilibriumModel, NashSolution
from app.domain.math.models_v3.ornstein_uhlenbeck import OrnsteinUhlenbeckConfig, OrnsteinUhlenbeckModel
from app.domain.math.models_v3.particle_filter import (
    ParticleFilterConfig,
    ParticleFilterDiagnostics,
    ParticleFilterModel,
)
from app.domain.math.models_v3.quadratic_programming import MarkowitzConfig, MarkowitzModel
from app.domain.math.models_v3.queueing_theory import QueueingTheoryConfig, QueueingTheoryModel
from app.domain.math.models_v3.sde_jump_diffusion import JumpDiffusionConfig, JumpDiffusionModel
from app.domain.math.models_v3.shapley_values import ShapleyConfig, ShapleyValuesModel
from app.domain.math.models_v3.simplex_optimization import SimplexOptimizationConfig, SimplexOptimizationModel
from app.domain.math.models_v3.survival_analysis import CoxPHConfig, CoxPHModel
from app.domain.math.models_v3.transformer_attention import TransformerAttentionConfig, TransformerAttentionModel
from app.domain.math.models_v3.var_model import VARConfig, VARModel


# ---- Batch 1 (Models 31-40) ----
@math_model_registry.register("GaussianProcess")
class RegisteredGaussianProcessModel(GaussianProcessModel):
    pass


@math_model_registry.register("ElasticNet")
class RegisteredElasticNetModel(ElasticNetModel):
    pass


@math_model_registry.register("AdaBoost")
class RegisteredAdaBoostModel(AdaBoostModel):
    pass


@math_model_registry.register("HMM")
class RegisteredHMMModel(HMMModel):
    pass


@math_model_registry.register("GARCH")
class RegisteredGARCHModel(GARCHModel):
    pass


@math_model_registry.register("KalmanFilter")
class RegisteredKalmanFilterModel(KalmanFilterModel):
    pass


@math_model_registry.register("ParticleFilter")
class RegisteredParticleFilterModel(ParticleFilterModel):
    pass


@math_model_registry.register("NashEquilibrium")
class RegisteredNashEquilibriumModel(NashEquilibriumModel):
    pass


@math_model_registry.register("QueueingTheory")
class RegisteredQueueingTheoryModel(QueueingTheoryModel):
    pass


@math_model_registry.register("JumpDiffusion")
class RegisteredJumpDiffusionModel(JumpDiffusionModel):
    pass


# ---- Batch 2 (Models 41-50) ----
@math_model_registry.register("LSTM")
class RegisteredLSTMModel(LSTMModel):
    pass


@math_model_registry.register("TransformerAttention")
class RegisteredTransformerAttentionModel(TransformerAttentionModel):
    pass


@math_model_registry.register("SurvivalAnalysis")
class RegisteredCoxPHModel(CoxPHModel):
    pass


@math_model_registry.register("OrnsteinUhlenbeck")
class RegisteredOrnsteinUhlenbeckModel(OrnsteinUhlenbeckModel):
    pass


@math_model_registry.register("VAR")
class RegisteredVARModel(VARModel):
    pass


@math_model_registry.register("ShapleyValues")
class RegisteredShapleyValuesModel(ShapleyValuesModel):
    pass


@math_model_registry.register("FractionalBrownianMotion")
class RegisteredFractionalBrownianMotionModel(FractionalBrownianMotionModel):
    pass


@math_model_registry.register("QuadraticProgramming")
class RegisteredMarkowitzModel(MarkowitzModel):
    pass


@math_model_registry.register("DirichletProcess")
class RegisteredDirichletProcessModel(DirichletProcessModel):
    pass


@math_model_registry.register("HawkesProcess")
class RegisteredHawkesProcessModel(HawkesProcessModel):
    pass


# ---- Batch 3 (Models 51-59) ----
@math_model_registry.register("LassoRidge")
class RegisteredLassoRidgeModel(LassoRidgeModel):
    pass


@math_model_registry.register("GradientBoosting")
class RegisteredGradientBoostingRegressionModel(GradientBoostingRegressionModel):
    pass


@math_model_registry.register("KernelDensity")
class RegisteredKernelDensityModel(KernelDensityModel):
    pass


@math_model_registry.register("BlackScholes")
class RegisteredBlackScholesModel(BlackScholesModel):
    pass


@math_model_registry.register("GaussianCopula")
class RegisteredGaussianCopulaModel(GaussianCopulaModel):
    pass


@math_model_registry.register("ExtremeValueTheory")
class RegisteredExtremeValueModel(ExtremeValueModel):
    pass


@math_model_registry.register("DynamicTimeWarping")
class RegisteredDynamicTimeWarpingModel(DynamicTimeWarpingModel):
    pass


@math_model_registry.register("HSMM")
class RegisteredHSMMModel(HSMMModel):
    pass


@math_model_registry.register("SimplexOptimization")
class RegisteredSimplexOptimizationModel(SimplexOptimizationModel):
    pass


_CONFIGS = [
    "AdaBoostConfig", "BlackScholesConfig", "CoxPHConfig", "DirichletProcessConfig",
    "DynamicTimeWarpingConfig", "ElasticNetConfig", "ExtremeValueConfig", "FractionalBrownianMotionConfig",
    "GARCHConfig", "GaussianCopulaConfig", "GaussianProcessConfig", "GradientBoostingRegressionConfig",
    "HMMConfig", "HSMMConfig", "HawkesConfig", "JumpDiffusionConfig", "KalmanFilterConfig",
    "KernelDensityConfig", "LSTMConfig", "LassoRidgeConfig", "MarkowitzConfig", "NashEquilibriumConfig",
    "OrnsteinUhlenbeckConfig", "ParticleFilterConfig", "QueueingTheoryConfig", "ShapleyConfig",
    "SimplexOptimizationConfig", "TransformerAttentionConfig", "VARConfig",
]
_MODELS = [
    "AdaBoostModel", "BlackScholesModel", "CoxPHModel", "DirichletProcessModel",
    "DynamicTimeWarpingModel", "ElasticNetModel", "ExtremeValueModel", "FractionalBrownianMotionModel",
    "GARCHModel", "GaussianCopulaModel", "GaussianProcessModel", "GradientBoostingRegressionModel",
    "HMMModel", "HSMMModel", "HawkesProcessModel", "JumpDiffusionModel", "KalmanFilterModel",
    "KernelDensityModel", "LSTMModel", "LassoRidgeModel", "MarkowitzModel", "NashEquilibriumModel",
    "OrnsteinUhlenbeckModel", "ParticleFilterModel", "QueueingTheoryModel", "ShapleyValuesModel",
    "SimplexOptimizationModel", "TransformerAttentionModel", "VARModel",
]
_EXTRAS = ["NashSolution", "ParticleFilterDiagnostics"]
_REGISTERED = [f"Registered{name}" for name in _MODELS]

__all__ = ["math_model_registry", *_CONFIGS, *_MODELS, *_EXTRAS, *_REGISTERED]
