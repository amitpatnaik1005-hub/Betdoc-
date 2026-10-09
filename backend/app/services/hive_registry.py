"""The model registry (Group 65): every math model, risk model and bet type BetDoc has, as rows in
``core_smallcase_registry`` that Hive bots chain into pipelines.

The catalogue below is the code's own inventory: each entry names the module that implements it,
and ``live_capable`` says whether a bot can run it on a live signal (``app.services.hive_pipeline``
has an adapter for exactly those). Everything else is real and selectable for research (The Core's
backtests) but refused for an autonomous bot: a model that cannot run must never count as a
confirmation.

``parse_blueprint`` reads the design documents (``betdoc_deep_dive.md``,
``betdoc_master_blueprint.md``) and ``reconcile`` lines their lists up against the catalogue:
documented components without an implementation are seeded too, marked not live.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.the_core import ComponentKind, SmallcaseRegistryModel, SmallcaseStatus

EXPECTED = {ComponentKind.MATH_MODEL: 59, ComponentKind.RISK_MODEL: 24, ComponentKind.BET_TYPE: 27}  # the blueprint's counts


@dataclass(frozen=True, slots=True)
class Component:
    kind: ComponentKind
    key: str
    name: str
    description: str
    category: str
    implementation: str | None
    live_capable: bool = False
    source: str = "code"
    aliases: tuple[str, ...] = ()


def _math(key: str, name: str, desc: str, category: str, impl: str, live: bool = False, aliases: tuple[str, ...] = ()) -> Component:
    return Component(ComponentKind.MATH_MODEL, f"math.{key}", name, desc, category, impl, live, aliases=aliases)


def _risk(key: str, name: str, desc: str, category: str, impl: str, live: bool = False, aliases: tuple[str, ...] = ()) -> Component:
    return Component(ComponentKind.RISK_MODEL, f"risk.{key}", name, desc, category, impl, live, aliases=aliases)


def _bet(key: str, name: str, desc: str, category: str, impl: str, live: bool = False, aliases: tuple[str, ...] = ()) -> Component:
    return Component(ComponentKind.BET_TYPE, f"bet.{key}", name, desc, category, impl, live, aliases=aliases)


_V1, _V2, _V3, _ARYA = "app.domain.math.models", "app.domain.math.models_v2", "app.domain.math.models_v3", "app.services.aryabhata_engine"
_RISK, _BETS = "app.domain.risk", "app.schemas.bet_types"

CATALOGUE: tuple[Component, ...] = (
    # ---------------------------------------------------------------- math: match-outcome models (team data)
    _math("poisson", "Poisson goal model", "Independent Poisson goal rates into 1X2 and scoreline probabilities.", "match_outcome", f"{_V1}.poisson:PoissonModel"),
    _math("dixon_coles", "Dixon-Coles", "Poisson with the Dixon-Coles low-score dependence correction.", "match_outcome", f"{_V1}.dixon_coles:DixonColesModel"),
    _math("elo", "Elo ratings", "Elo rating difference to win/draw/loss probabilities.", "match_outcome", f"{_V1}.elo:EloProbabilityModel"),
    _math("monte_carlo", "Monte Carlo match simulation", "Simulated scorelines from goal rates.", "match_outcome", f"{_V1}.monte_carlo:MonteCarloModel", aliases=("monte carlo simulation",)),
    # ---------------------------------------------------------------- math: v2 suite
    _math("arima", "ARIMA", "Autoregressive integrated moving average forecast of the line.", "time_series", f"{_V2}.arima:ARIMAModel", live=True),
    _math("bayesian_network", "Bayesian network", "Discrete Bayesian network with exact variable elimination.", "probabilistic", f"{_V2}.bayesian_network:BayesianNetworkModel"),
    _math("binomial", "Binomial / Poisson-binomial", "Tail probabilities for discrete sporting events.", "probabilistic", f"{_V2}.binomial:BinomialModel"),
    _math("causal_inference", "Causal inference (IV / IPW)", "Instrumental variables and inverse probability weighting.", "statistical", f"{_V2}.causal_inference:CausalInferenceModel"),
    _math("clustering", "K-means / Gaussian mixture clustering", "Regime clustering of market states.", "machine_learning", f"{_V2}.clustering:ClusteringModel"),
    _math("copulas", "Gaussian & Student-t copulas", "Multivariate dependence between outcomes.", "dependence", f"{_V2}.copulas:CopulaModel"),
    _math("fourier_analysis", "Fourier analysis", "Discrete Fourier transform for seasonality.", "time_series", f"{_V2}.fourier_analysis:FourierAnalysisModel"),
    _math("genetic_algorithm", "Genetic algorithm", "Evolutionary portfolio optimisation for Sharpe.", "optimisation", f"{_V2}.genetic_algorithm:GeneticAlgorithmModel"),
    _math("information_theory", "Information theory (entropy, MI, KL)", "Entropy, mutual information and KL divergence.", "statistical", f"{_V2}.information_theory:InformationTheoryModel"),
    _math("logistic_regression", "Logistic regression", "L-BFGS-B logistic regression.", "machine_learning", f"{_V2}.logistic_regression:LogisticRegressionModel"),
    _math("markov_chain", "Markov chain", "Discrete-time state transitions.", "probabilistic", f"{_V2}.markov_chain:MarkovChainModel"),
    _math("moving_averages", "EWMA / MACD", "Exponentially weighted moving average of the line.", "time_series", f"{_V2}.moving_averages:MovingAverageModel", live=True, aliases=("ewma", "macd", "moving average")),
    _math("neural_net", "Neural network (MLP)", "Multi-layer perceptron classifier.", "machine_learning", f"{_V2}.neural_net:NeuralNetModel", aliases=("mlp", "neural network")),
    _math("normal_distribution", "Normal point-spread model", "Gaussian point spread with dynamic variance.", "probabilistic", f"{_V2}.normal_distribution:NormalDistributionModel"),
    _math("pca", "Principal component analysis", "Dimensionality reduction of features.", "statistical", f"{_V2}.pca:PCAModel"),
    _math("prospect_theory", "Cumulative prospect theory", "Irrational market pricing (probability weighting).", "behavioural", f"{_V2}.prospect_theory:ProspectTheoryModel"),
    _math("random_forest", "Random forest", "Bagged decision-tree classifier.", "machine_learning", f"{_V2}.random_forest:RandomForestModel"),
    _math("reinforcement_learning", "Q-learning", "Value iteration for discrete decisions.", "machine_learning", f"{_V2}.reinforcement_learning:QLearningModel", aliases=("reinforcement learning",)),
    _math("svm", "Support vector machine", "SVM classifier.", "machine_learning", f"{_V2}.svm:SVMModel"),
    _math("gradient_boosting_classifier", "Gradient boosting classifier (XGBoost-style)", "Histogram gradient boosting classification.", "machine_learning", f"{_V2}.xgboost_model:GradientBoostingModel", aliases=("xgboost",)),
    # ---------------------------------------------------------------- math: v3 suite
    _math("adaboost", "AdaBoost", "Adaptive boosting over decision stumps.", "machine_learning", f"{_V3}.adaboost:AdaBoostModel"),
    _math("black_scholes", "Black-Scholes-Merton", "Option-style pricing and Greeks for in-play lines.", "pricing", f"{_V3}.black_scholes:BlackScholesModel"),
    _math("dirichlet_process", "Dirichlet process mixture", "Non-parametric Gaussian mixture (Gibbs sampling).", "probabilistic", f"{_V3}.dirichlet_process:DirichletProcessModel"),
    _math("dynamic_time_warping", "Dynamic time warping", "Shape similarity between price paths.", "time_series", f"{_V3}.dynamic_time_warping:DynamicTimeWarpingModel"),
    _math("elastic_net", "Elastic net", "L1+L2 regularised regression.", "statistical", f"{_V3}.elastic_net:ElasticNetModel"),
    _math("extreme_value", "Extreme value theory (POT)", "Generalised Pareto tail of outcomes.", "probabilistic", f"{_V3}.extreme_value:ExtremeValueModel"),
    _math("fractional_brownian_motion", "Fractional Brownian motion", "Long-memory price dynamics.", "stochastic", f"{_V3}.fractional_brownian_motion:FractionalBrownianMotionModel"),
    _math("garch", "GARCH(1,1)", "Conditional volatility of the line.", "time_series", f"{_V3}.garch:GARCHModel"),
    _math("gaussian_copula", "Gaussian copula (joint events)", "Joint probabilities of correlated events.", "dependence", f"{_V3}.gaussian_copula:GaussianCopulaModel"),
    _math("gaussian_process", "Gaussian process regression", "RBF-kernel GP regression.", "machine_learning", f"{_V3}.gaussian_process:GaussianProcessModel"),
    _math("gradient_boosting_regressor", "Gradient boosting regressor", "Bounded-complexity gradient boosting regression.", "machine_learning", f"{_V3}.gradient_boosting:GradientBoostingRegressionModel"),
    _math("hawkes_process", "Hawkes process", "Self-exciting event intensity (goals, steam).", "stochastic", f"{_V3}.hawkes_process:HawkesProcessModel"),
    _math("hmm", "Hidden Markov model", "Gaussian HMM regimes (Baum-Welch, Viterbi).", "probabilistic", f"{_V3}.hmm:HMMModel", aliases=("hidden markov",)),
    _math("hsmm", "Hidden semi-Markov model", "Explicit-duration HMM.", "probabilistic", f"{_V3}.hsmm:HSMMModel"),
    _math("kalman_filter", "Kalman filter", "State-space tracking of the true line.", "time_series", f"{_V3}.kalman_filter:KalmanFilterModel", live=True),
    _math("kernel_density", "Kernel density estimation", "Smooth outcome density (Silverman bandwidth).", "statistical", f"{_V3}.kernel_density:KernelDensityModel"),
    _math("lasso_ridge", "Lasso / ridge regression", "L1 / L2 regularised least squares.", "statistical", f"{_V3}.lasso_ridge:LassoRidgeModel"),
    _math("lstm", "LSTM network", "Recurrent sequence regressor.", "machine_learning", f"{_V3}.lstm_numpy:LSTMModel"),
    _math("nash_equilibrium", "Nash equilibrium", "Zero-sum game solution by linear programming.", "game_theory", f"{_V3}.nash_equilibrium:NashEquilibriumModel"),
    _math("ornstein_uhlenbeck", "Ornstein-Uhlenbeck", "Mean-reverting forecast of the line.", "stochastic", f"{_V3}.ornstein_uhlenbeck:OrnsteinUhlenbeckModel", live=True),
    _math("particle_filter", "Particle filter", "Sequential Monte Carlo on implied probability.", "stochastic", f"{_V3}.particle_filter:ParticleFilterModel"),
    _math("markowitz", "Markowitz mean-variance", "Quadratic-programming allocation.", "optimisation", f"{_V3}.quadratic_programming:MarkowitzModel", aliases=("quadratic programming", "mean variance")),
    _math("queueing_theory", "Queueing theory (M/M/c)", "Erlang-C queue metrics.", "operations", f"{_V3}.queueing_theory:QueueingTheoryModel"),
    _math("jump_diffusion", "Merton jump-diffusion", "Jump-diffusion calibration and simulation.", "stochastic", f"{_V3}.sde_jump_diffusion:JumpDiffusionModel", aliases=("sde jump diffusion",)),
    _math("shapley_values", "Shapley values", "Attribution of ensemble signals.", "explainability", f"{_V3}.shapley_values:ShapleyValuesModel"),
    _math("simplex_optimization", "Simplex optimisation", "Linear-programming capital allocation.", "optimisation", f"{_V3}.simplex_optimization:SimplexOptimizationModel", aliases=("linear programming",)),
    _math("survival_analysis", "Cox proportional hazards", "Time-to-event survival analysis.", "statistical", f"{_V3}.survival_analysis:CoxPHModel", aliases=("cox",)),
    _math("transformer_attention", "Transformer attention", "Causal multi-head self-attention encoder.", "machine_learning", f"{_V3}.transformer_attention:TransformerAttentionModel"),
    _math("var_model", "Vector autoregression VAR(p)", "Multivariate autoregression across lines.", "time_series", f"{_V3}.var_model:VARModel", aliases=("vector autoregression",)),
    # ---------------------------------------------------------------- math: the Aryabhata quant engine
    _math("devig_shin", "Shin de-vig", "Fair probabilities with Shin's insider-trading margin model.", "quant_engine", f"{_ARYA}:shin_probabilities", live=True, aliases=("shin",)),
    _math("devig_mpo", "MPO de-vig", "Margin proportional to odds.", "quant_engine", f"{_ARYA}:mpo_probabilities", live=True, aliases=("margin proportional to odds",)),
    _math("devig_multiplicative", "Multiplicative de-vig", "Proportional normalisation of implied probabilities.", "quant_engine", f"{_ARYA}:multiplicative_probabilities", live=True),
    _math("consensus", "Robust consensus", "Trimmed median of every fresh book's fair price.", "quant_engine", f"{_ARYA}:consensus_probabilities", live=True, aliases=("consensus probability",)),
    _math("ema_steam", "EMA steam detector", "60s EMA of the consensus: follows sharp money.", "quant_engine", f"{_ARYA}:update_ema", live=True, aliases=("steam move", "ema")),
    _math("kelly_criterion", "Kelly criterion (staking)", "Fractional Kelly stake from the pipeline's conviction.", "staking", f"{_ARYA}:kelly_fraction", live=True, aliases=("kelly",)),
    # ---------------------------------------------------------------- risk models
    _risk("black_litterman", "Black-Litterman", "Blend market-implied returns with views.", "portfolio", f"{_RISK}.black_litterman:black_litterman_adjust"),
    _risk("component_var", "Component VaR", "Euler-allocated VaR per position.", "portfolio", f"{_RISK}.component_var:component_var"),
    _risk("concentration", "Concentration (HHI)", "Herfindahl index of open stakes across fixtures.", "exposure", f"{_RISK}.concentration:calculate_hhi", live=True, aliases=("hhi", "herfindahl")),
    _risk("copula_clayton", "Clayton copula tail", "Lower-tail dependence of losses.", "dependence", f"{_RISK}.copula_clayton:clayton_lower_tail_dependence"),
    _risk("copula_gumbel", "Gumbel copula tail", "Upper-tail dependence.", "dependence", f"{_RISK}.copula_gumbel:gumbel_upper_tail_dependence"),
    _risk("correlation_risk", "Correlation risk", "Eigenvalue dispersion / absorption ratio.", "portfolio", f"{_RISK}.correlation_risk:eigen_dispersion"),
    _risk("credit_risk", "Counterparty credit risk", "Expected loss on bookmaker counterparties.", "counterparty", f"{_RISK}.credit_risk:expected_shortfall_counterparty", aliases=("counterparty",)),
    _risk("cvar", "CVaR (expected shortfall)", "Mean of the worst 5% of the bot's returns.", "tail", f"{_RISK}.cvar:calculate_cvar", live=True, aliases=("expected shortfall",)),
    _risk("drawdown", "Drawdown", "Current decline from the bot's equity peak.", "capital", f"{_RISK}.drawdown:calculate_current_drawdown", live=True, aliases=("max drawdown",)),
    _risk("entropic_risk", "Entropic risk", "Exponential-utility risk of the bot's returns.", "tail", f"{_RISK}.entropic_risk:entropic_risk_measure", live=True),
    _risk("evt", "Extreme value tail risk", "Generalised Pareto tail VaR / ES of losses.", "tail", f"{_RISK}.evt:evt_tail_risk", aliases=("extreme value",)),
    _risk("exposure", "Exposure limit", "Open stakes as a share of the sub-bankroll.", "exposure", f"{_RISK}.exposure:calculate_exposure", live=True),
    _risk("kelly_portfolio", "Portfolio Kelly", "Simultaneous Kelly fractions scaled to a total cap.", "staking", f"{_RISK}.kelly_portfolio:portfolio_kelly", live=True),
    _risk("liquidity_risk", "Liquidity-adjusted VaR", "VaR plus spread and volume impact.", "liquidity", f"{_RISK}.liquidity_risk:liquidity_adjusted_var"),
    _risk("model_risk", "Model edge decay", "Half-life decay of a pipeline's edge since it last changed.", "model", f"{_RISK}.model_risk:edge_decay_penalty", live=True),
    _risk("mpt", "Modern portfolio theory", "Mean-variance portfolio optimisation.", "portfolio", f"{_RISK}.mpt:optimize_portfolio", aliases=("modern portfolio",)),
    _risk("operational_risk", "Operational risk (LDA)", "Compound Poisson-lognormal loss quantile.", "operational", f"{_RISK}.operational_risk:lda_percentile"),
    _risk("risk_parity", "Risk parity", "Equal-risk-contribution weights.", "portfolio", f"{_RISK}.parity:risk_parity_weights", aliases=("parity",)),
    _risk("performance_ratios", "Sharpe / Sortino / Omega", "Performance-ratio gate on the bot's returns.", "performance", f"{_RISK}.ratios:sortino_ratio", live=True, aliases=("sharpe", "sortino", "omega", "calmar")),
    _risk("reverse_stress", "Reverse stress test", "Volatility that makes ruin a confidence-level event.", "stress", f"{_RISK}.reverse_stress:implied_ruin_volatility"),
    _risk("scenario_stress", "Scenario stress matrix", "Deterministic scenario stress testing.", "stress", f"{_RISK}.scenario_matrix:stress_test_portfolio", aliases=("scenario matrix",)),
    _risk("spectral_risk", "Spectral risk", "Exponentially weighted tail risk of returns.", "tail", f"{_RISK}.spectral:exponential_spectral_risk", live=True),
    _risk("stop_loss", "Stop-loss engine", "Daily loss and losing-streak stops (account level).", "capital", f"{_RISK}.stop_loss:StopLossEngine"),
    _risk("var", "Value at risk", "Parametric and historical VaR of the bot's returns.", "tail", f"{_RISK}.var:calculate_historical_var", live=True, aliases=("value at risk",)),
    # ---------------------------------------------------------------- bet types: markets
    _bet("match_winner_1x2", "Match winner (1X2)", "Home / draw / away.", "market", f"{_BETS}:MarketType.MATCH_WINNER_1X2", live=True, aliases=("1x2", "match odds", "moneyline", "h2h")),
    _bet("asian_handicap", "Asian handicap", "Handicap lines with half/quarter splits.", "market", f"{_BETS}:MarketType.ASIAN_HANDICAP", live=True),
    _bet("asian_over_under", "Asian over/under", "Asian goal-total lines.", "market", f"{_BETS}:MarketType.ASIAN_OVER_UNDER"),
    _bet("over_under_goals", "Over/under goals", "Total goals over or under a line.", "market", f"{_BETS}:MarketType.OVER_UNDER_GOALS", live=True, aliases=("totals", "over under")),
    _bet("btts", "Both teams to score", "Yes / no.", "market", f"{_BETS}:MarketType.BTTS", aliases=("both teams to score",)),
    _bet("double_chance", "Double chance", "Two of the three 1X2 outcomes.", "market", f"{_BETS}:MarketType.DOUBLE_CHANCE"),
    _bet("draw_no_bet", "Draw no bet", "Stake returned on a draw.", "market", f"{_BETS}:MarketType.DRAW_NO_BET"),
    _bet("correct_score", "Correct score", "Exact final score.", "market", f"{_BETS}:MarketType.CORRECT_SCORE"),
    _bet("goalscorer", "Goalscorer", "First / last / anytime scorer.", "market", f"{_BETS}:MarketType.GOALSCORER"),
    _bet("half_time_full_time", "Half-time / full-time", "Both results.", "market", f"{_BETS}:MarketType.HALF_TIME_FULL_TIME", aliases=("ht/ft",)),
    _bet("corners_over_under", "Corners over/under", "Total corners.", "market", f"{_BETS}:MarketType.CORNERS_OVER_UNDER"),
    _bet("cards_over_under", "Cards over/under", "Total bookings.", "market", f"{_BETS}:MarketType.CARDS_OVER_UNDER"),
    _bet("player_props", "Player props", "Player statistics lines.", "market", f"{_BETS}:MarketType.PLAYER_PROPS"),
    # ---------------------------------------------------------------- bet types: structures
    _bet("single", "Single", "One selection.", "structure", f"{_BETS}:SingleBet", live=True),
    _bet("parlay", "Parlay / accumulator", "Every leg must win.", "structure", f"{_BETS}:ParlayBet", aliases=("accumulator", "acca")),
    _bet("system", "System bet", "Every k-fold combination.", "structure", f"{_BETS}:SystemBet"),
    _bet("trixie", "Trixie", "3 doubles and a treble.", "structure", f"{_BETS}:TrixieBet"),
    _bet("patent", "Patent", "Trixie plus 3 singles.", "structure", f"{_BETS}:PatentBet"),
    _bet("yankee", "Yankee", "11 bets on 4 selections.", "structure", f"{_BETS}:YankeeBet"),
    _bet("lucky_15", "Lucky 15", "15 bets on 4 selections.", "structure", f"{_BETS}:Lucky15Bet"),
    _bet("canadian", "Canadian (Super Yankee)", "26 bets on 5 selections.", "structure", f"{_BETS}:CanadianBet", aliases=("super yankee",)),
    _bet("lucky_31", "Lucky 31", "31 bets on 5 selections.", "structure", f"{_BETS}:Lucky31Bet"),
    _bet("heinz", "Heinz", "57 bets on 6 selections.", "structure", f"{_BETS}:HeinzBet"),
    _bet("lucky_63", "Lucky 63", "63 bets on 6 selections.", "structure", f"{_BETS}:Lucky63Bet"),
    _bet("super_heinz", "Super Heinz", "120 bets on 7 selections.", "structure", f"{_BETS}:SuperHeinzBet"),
    _bet("goliath", "Goliath", "247 bets on 8 selections.", "structure", f"{_BETS}:GoliathBet"),
    _bet("each_way", "Each way", "Win and place parts.", "structure", f"{_BETS}:EachWayBet"),
    _bet("lay", "Lay (exchange)", "Back the outcome not to happen.", "structure", f"{_BETS}:LayBet"),
)

# The live signal stream's market types -> the bet-type component that covers them
SIGNAL_MARKETS: dict[str, str] = {
    "Match Odds": "bet.match_winner_1x2",
    "Over/Under 2.5": "bet.over_under_goals",
    "Asian Handicap": "bet.asian_handicap",
}
STAKING_MODEL = "math.kelly_criterion"


def catalogue_by_key() -> dict[str, Component]:
    return {c.key: c for c in CATALOGUE}


# ---------------------------------------------------------------- the blueprint documents
_KIND_HEADINGS = (
    (ComponentKind.MATH_MODEL, re.compile(r"\bmath(?:ematical)?\s+models?\b|\bquant(?:itative)?\s+models?\b", re.I)),
    (ComponentKind.RISK_MODEL, re.compile(r"\brisk\s+models?\b|\brisk\s+management\s+models?\b", re.I)),
    (ComponentKind.BET_TYPE, re.compile(r"\bbet\s*types?\b|\bbetting\s+markets?\b|\bmarkets?\s+and\s+bet", re.I)),
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MARKUP = re.compile(r"[*_`]|\[([^\]]*)\]\([^)]*\)")
_SPLIT = re.compile(r"\s+[-–—:]\s+|:\s+|\s+\(")


def _clean(text: str) -> str:
    text = _MARKUP.sub(lambda m: m.group(1) or "", text).strip()
    return _SPLIT.split(text, maxsplit=1)[0].strip().rstrip(".:")


def parse_blueprint(markdown: str) -> dict[ComponentKind, list[str]]:
    """Every list item (or first table cell) under a heading that names a component kind, until
    the next heading of the same or a higher level."""
    found: dict[ComponentKind, list[str]] = {kind: [] for kind, _ in _KIND_HEADINGS}
    active: tuple[ComponentKind, int] | None = None
    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
            kind = next((k for k, pattern in _KIND_HEADINGS if pattern.search(title)), None)
            if kind is not None:
                active = (kind, level)
            elif active is not None and level <= active[1]:
                active = None
            continue
        if active is None:
            continue
        item = _ITEM.match(line)
        text = item.group(1) if item else None
        if text is None:
            row = _TABLE_ROW.match(line)
            if row and not re.fullmatch(r"[\s|:\-]+", row.group(1)):
                cells = [c.strip() for c in row.group(1).split("|")]
                text = cells[1] if cells and re.fullmatch(r"\d+", cells[0]) and len(cells) > 1 else cells[0]
                if text.lower() in {"model", "name", "bet type", "risk model", "math model", "#", "component"}:
                    text = None
        if text:
            name = _clean(text)
            if name and name not in found[active[0]]:
                found[active[0]].append(name)
    return found


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(w for w in text.split() if w not in {"the", "model", "models", "a", "of", "and"})


@dataclass(slots=True)
class Reconciliation:
    matched: dict[ComponentKind, list[tuple[str, str]]] = field(default_factory=dict)  # (doc name, catalogue key)
    documented_only: dict[ComponentKind, list[str]] = field(default_factory=dict)
    code_only: dict[ComponentKind, list[str]] = field(default_factory=dict)

    def counts(self) -> dict[str, dict[str, int]]:
        return {
            kind.value: {
                "matched": len(self.matched.get(kind, [])),
                "documented_only": len(self.documented_only.get(kind, [])),
                "code_only": len(self.code_only.get(kind, [])),
            }
            for kind in EXPECTED
        }


def reconcile(documented: Mapping[ComponentKind, Sequence[str]], catalogue: Iterable[Component] = CATALOGUE) -> Reconciliation:
    result = Reconciliation()
    entries = list(catalogue)
    for kind in EXPECTED:
        pool = [c for c in entries if c.kind is kind]
        names = {c.key: {_norm(c.name), _norm(c.key.split(".", 1)[1].replace("_", " ")), *(_norm(a) for a in c.aliases)} for c in pool}
        used: set[str] = set()
        for doc_name in documented.get(kind, []):
            wanted = _norm(doc_name)
            hit = next((key for key, forms in names.items() if key not in used and wanted in forms), None)
            if hit is None:  # a looser match: every word of the shorter name appears in the longer one
                hit = next(
                    (key for key, forms in names.items() if key not in used and any(set(f.split()) <= set(wanted.split()) or set(wanted.split()) <= set(f.split()) for f in forms if f)),
                    None,
                )
            if hit is None:
                result.documented_only.setdefault(kind, []).append(doc_name)
            else:
                used.add(hit)
                result.matched.setdefault(kind, []).append((doc_name, hit))
        result.code_only[kind] = [c.key for c in pool if c.key not in used] if documented.get(kind) else []
    return result


def documented_components(rec: Reconciliation) -> list[Component]:
    """Components the documents name that no code implements: seeded, never live."""
    out: list[Component] = []
    for kind, names in rec.documented_only.items():
        prefix = {ComponentKind.MATH_MODEL: "math", ComponentKind.RISK_MODEL: "risk", ComponentKind.BET_TYPE: "bet"}[kind]
        for name in names:
            slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:72] or "unnamed"
            out.append(Component(kind, f"{prefix}.doc_{slug}", name[:100], "Documented in the blueprint; not implemented yet.", "documented", None, False, "blueprint"))
    return out


# ---------------------------------------------------------------- seeding
@dataclass(slots=True)
class SeedReport:
    inserted: int = 0
    updated: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    live_by_kind: dict[str, int] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=lambda: {k.value: v for k, v in EXPECTED.items()})

    def as_dict(self) -> dict[str, Any]:
        return {"inserted": self.inserted, "updated": self.updated, "by_kind": self.by_kind, "live_by_kind": self.live_by_kind, "expected": self.expected}


def _display_name(component: Component, taken: set[str]) -> str:
    """Registry names are unique across The Core's pipelines too: prefix the kind."""
    label = {ComponentKind.MATH_MODEL: "Math", ComponentKind.RISK_MODEL: "Risk", ComponentKind.BET_TYPE: "Bet"}[component.kind]
    name = f"{label} · {component.name}"[:120]
    return name if name not in taken else f"{name[:100]} ({component.key})"[:120]


async def seed_registry(session: AsyncSession, components: Sequence[Component] = CATALOGUE, now: datetime | None = None) -> SeedReport:
    """Upsert every component by its key (idempotent: run it as often as you like). The caller commits."""
    now = now or datetime.now(UTC)
    report = SeedReport()
    existing = {row.component_key: row for row in (await session.execute(select(SmallcaseRegistryModel).where(SmallcaseRegistryModel.component_key.is_not(None)))).scalars()}
    taken = set((await session.execute(select(SmallcaseRegistryModel.name))).scalars())
    for component in components:
        row = existing.get(component.key)
        if row is None:
            name = _display_name(component, taken)
            taken.add(name)
            session.add(
                SmallcaseRegistryModel(
                    name=name,
                    description=component.description,
                    pipeline_config=[component.key],
                    status=SmallcaseStatus.STANDBY,
                    component_kind=component.kind,
                    component_key=component.key,
                    category=component.category,
                    implementation=component.implementation,
                    live_capable=component.live_capable,
                    catalogue_source=component.source,
                    created_at=now,
                    updated_at=now,
                )
            )
            report.inserted += 1
        else:
            row.description = component.description
            row.category = component.category
            row.implementation = component.implementation
            row.live_capable = component.live_capable
            row.catalogue_source = component.source
            row.component_kind = component.kind
            report.updated += 1
        report.by_kind[component.kind.value] = report.by_kind.get(component.kind.value, 0) + 1
        if component.live_capable:
            report.live_by_kind[component.kind.value] = report.live_by_kind.get(component.kind.value, 0) + 1
    await session.flush()
    return report


async def registry_components(session: AsyncSession) -> dict[str, SmallcaseRegistryModel]:
    rows = (await session.execute(select(SmallcaseRegistryModel).where(SmallcaseRegistryModel.component_kind != "PIPELINE"))).scalars()
    return {row.component_key: row for row in rows if row.component_key}
