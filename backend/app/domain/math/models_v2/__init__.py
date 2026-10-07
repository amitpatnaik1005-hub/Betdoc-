"""Smallcase Engine v2: plugin registration for math models 11 through 30."""

from app.core.registry import PluginRegistry
from app.domain.math.models_v2.arima import ARIMAConfig, ARIMAModel
from app.domain.math.models_v2.base import BaseMathModel, MathModelConfig
from app.domain.math.models_v2.bayesian_network import BayesianNetworkConfig, BayesianNetworkModel
from app.domain.math.models_v2.binomial import BinomialConfig, BinomialModel
from app.domain.math.models_v2.causal_inference import CausalInferenceConfig, CausalInferenceModel
from app.domain.math.models_v2.clustering import ClusteringConfig, ClusteringModel
from app.domain.math.models_v2.copulas import CopulaConfig, CopulaModel
from app.domain.math.models_v2.fourier_analysis import FourierAnalysisConfig, FourierAnalysisModel
from app.domain.math.models_v2.genetic_algorithm import GeneticAlgorithmConfig, GeneticAlgorithmModel
from app.domain.math.models_v2.information_theory import InformationTheoryConfig, InformationTheoryModel
from app.domain.math.models_v2.logistic_regression import LogisticRegressionConfig, LogisticRegressionModel
from app.domain.math.models_v2.markov_chain import MarkovChainConfig, MarkovChainModel
from app.domain.math.models_v2.moving_averages import MovingAverageConfig, MovingAverageModel
from app.domain.math.models_v2.neural_net import NeuralNetConfig, NeuralNetModel
from app.domain.math.models_v2.normal_distribution import NormalDistributionConfig, NormalDistributionModel
from app.domain.math.models_v2.pca import PCAConfig, PCAModel
from app.domain.math.models_v2.prospect_theory import ProspectTheoryConfig, ProspectTheoryModel
from app.domain.math.models_v2.random_forest import RandomForestConfig, RandomForestModel
from app.domain.math.models_v2.reinforcement_learning import QLearningConfig, QLearningModel
from app.domain.math.models_v2.svm import SVMConfig, SVMModel
from app.domain.math.models_v2.xgboost_model import GradientBoostingConfig, GradientBoostingModel

math_model_registry = PluginRegistry[BaseMathModel]("math_models_v2")


@math_model_registry.register("Binomial")
class RegisteredBinomialModel(BinomialModel):
    pass


@math_model_registry.register("NormalDistribution")
class RegisteredNormalDistributionModel(NormalDistributionModel):
    pass


@math_model_registry.register("Copula")
class RegisteredCopulaModel(CopulaModel):
    pass


@math_model_registry.register("BayesianNetwork")
class RegisteredBayesianNetworkModel(BayesianNetworkModel):
    pass


@math_model_registry.register("MarkovChain")
class RegisteredMarkovChainModel(MarkovChainModel):
    pass


@math_model_registry.register("MovingAverage")
class RegisteredMovingAverageModel(MovingAverageModel):
    pass


@math_model_registry.register("ARIMA")
class RegisteredARIMAModel(ARIMAModel):
    pass


@math_model_registry.register("FourierAnalysis")
class RegisteredFourierAnalysisModel(FourierAnalysisModel):
    pass


@math_model_registry.register("InformationTheory")
class RegisteredInformationTheoryModel(InformationTheoryModel):
    pass


@math_model_registry.register("ProspectTheory")
class RegisteredProspectTheoryModel(ProspectTheoryModel):
    pass


@math_model_registry.register("CausalInference")
class RegisteredCausalInferenceModel(CausalInferenceModel):
    pass


@math_model_registry.register("LogisticRegression")
class RegisteredLogisticRegressionModel(LogisticRegressionModel):
    pass


@math_model_registry.register("RandomForest")
class RegisteredRandomForestModel(RandomForestModel):
    pass


@math_model_registry.register("SVM")
class RegisteredSVMModel(SVMModel):
    pass


@math_model_registry.register("XGBoost")
class RegisteredGradientBoostingModel(GradientBoostingModel):
    pass


@math_model_registry.register("NeuralNet")
class RegisteredNeuralNetModel(NeuralNetModel):
    pass


@math_model_registry.register("PCA")
class RegisteredPCAModel(PCAModel):
    pass


@math_model_registry.register("Clustering")
class RegisteredClusteringModel(ClusteringModel):
    pass


@math_model_registry.register("QLearning")
class RegisteredQLearningModel(QLearningModel):
    pass


@math_model_registry.register("GeneticAlgorithm")
class RegisteredGeneticAlgorithmModel(GeneticAlgorithmModel):
    pass


__all__ = [
    "math_model_registry",
    "BaseMathModel",
    "MathModelConfig",
    "ARIMAConfig",
    "ARIMAModel",
    "BayesianNetworkConfig",
    "BayesianNetworkModel",
    "BinomialConfig",
    "BinomialModel",
    "CausalInferenceConfig",
    "CausalInferenceModel",
    "ClusteringConfig",
    "ClusteringModel",
    "CopulaConfig",
    "CopulaModel",
    "FourierAnalysisConfig",
    "FourierAnalysisModel",
    "GeneticAlgorithmConfig",
    "GeneticAlgorithmModel",
    "GradientBoostingConfig",
    "GradientBoostingModel",
    "InformationTheoryConfig",
    "InformationTheoryModel",
    "LogisticRegressionConfig",
    "LogisticRegressionModel",
    "MarkovChainConfig",
    "MarkovChainModel",
    "MovingAverageConfig",
    "MovingAverageModel",
    "NeuralNetConfig",
    "NeuralNetModel",
    "NormalDistributionConfig",
    "NormalDistributionModel",
    "PCAConfig",
    "PCAModel",
    "ProspectTheoryConfig",
    "ProspectTheoryModel",
    "QLearningConfig",
    "QLearningModel",
    "RandomForestConfig",
    "RandomForestModel",
    "SVMConfig",
    "SVMModel",
    "RegisteredARIMAModel",
    "RegisteredBayesianNetworkModel",
    "RegisteredBinomialModel",
    "RegisteredCausalInferenceModel",
    "RegisteredClusteringModel",
    "RegisteredCopulaModel",
    "RegisteredFourierAnalysisModel",
    "RegisteredGeneticAlgorithmModel",
    "RegisteredGradientBoostingModel",
    "RegisteredInformationTheoryModel",
    "RegisteredLogisticRegressionModel",
    "RegisteredMarkovChainModel",
    "RegisteredMovingAverageModel",
    "RegisteredNeuralNetModel",
    "RegisteredNormalDistributionModel",
    "RegisteredPCAModel",
    "RegisteredProspectTheoryModel",
    "RegisteredQLearningModel",
    "RegisteredRandomForestModel",
    "RegisteredSVMModel",
]
