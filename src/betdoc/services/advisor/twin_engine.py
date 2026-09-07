"""The AI Bet Twin: an emotionless risk firewall and sizing advisor.



Pipeline, in strict order. The ordering is the design, not an implementation

detail, and it must not be rearranged:



#. **State ingestion.** Profile plus live account state. A stale snapshot is

   refused outright, because sizing against a balance that has already moved

   is how an account gets over-staked.

#. **Loss-budget check.** A hard capital constraint, evaluated before any

   expected value is computed. Capital constraints outrank edge

   unconditionally: ruin is absorbing, and no edge survives it.

#. **News overlay.** Probability shifts composed in log-odds space, variance

   penalties composed multiplicatively.

#. **Math-engine hook.** Kelly on the news-adjusted probability, divided by the

   composed uncertainty penalty, then scaled by the profile's fractional-Kelly

   multiplier.

#. **Output.** A :class:`TwinRecommendation` for every opportunity, including

   refusals, plus one :class:`SessionForecast`.



The central mathematical mechanism

----------------------------------

In the mean-variance approximation of expected log wealth, the growth-optimal

staked fraction of a binary bet is::



    f* = mu / sigma^2        mu = p*b - q        sigma^2 = p*q*o^2



So inflating the variance by ``lambda`` divides the stake by exactly

``lambda``::



    f*_lambda = mu / (lambda * sigma^2) = f* / lambda



That single identity is how unstructured text reaches the bankroll. A verified

club announcement carries ``lambda = 1.0`` and changes nothing about sizing. An

anonymous rumour carries ``lambda = 4.6``, and the same nominal edge is staked

at roughly 22% of size. The user is not protected by a hand-tuned rule that

says "be careful with rumours"; they are protected because unreliable

information mathematically widens the outcome distribution, and a wider

distribution provably justifies a smaller stake.



The same identity gives the minimum-edge gate its correct form. Requiring a

floor on the *stake* rather than on the raw edge means::



    f*_lambda >= f_min   <=>   mu >= f_min * lambda * sigma^2



so the edge a bet must clear scales with the uncertainty attached to it. This

is why :attr:`RejectionReason.FAILS_ROBUST_EV` fires on thin edges under

rumour conditions that would pass comfortably on verified news.



Why not average over a posterior instead

----------------------------------------

Because for this objective it provably achieves nothing. Expected log wealth

``g(f, p) = p*log(1 + f*b) + (1 - p)*log(1 - f)`` is **linear in p**, so

integrating over any posterior on ``p`` returns the posterior mean and leaves

``f*`` unchanged. The two defensible mechanisms are a quantile (robust)

criterion on ``p``, used by ``ev_filter``, and a variance penalty on the

sizing, used here. They compose cleanly and are applied at different stages.



Sequential, not joint

---------------------

This service allocates greedily in descending edge order, decrementing the

loss budget and per-sport headroom as it commits. That is an approximation.

The exact simultaneous solution maximises

``sum_w P(w) * log(1 + sum_j x_j r_j(w))`` over the whole candidate set and

lives in ``betdoc.domain.math.portfolio_kelly``. Inject it through

:class:`KellySizer` when the joint solve is wanted; the greedy path exists so

the firewall still functions when the solver is unavailable, and because

greedy is strictly conservative here (every commitment shrinks the budget seen

by later candidates, so the total can never exceed the joint optimum's caps).



Purity

------

No I/O, no database, no network, no HTTP client, no browser automation. The

service is a function of its inputs. structlog emits an event on every

rejection for searchability, but every decision is *also* returned as typed

data, and callers and tests must assert on the returned models rather than on

log output.

"""



from __future__ import annotations



import math

import uuid

from collections import Counter

from collections.abc import Sequence

from datetime import UTC, datetime

from typing import Final, Protocol, runtime_checkable



import structlog

from pydantic import BaseModel, ConfigDict, Field, model_validator

from typing_extensions import Self



from betdoc.domain.intelligence.account_models import (

    AccountState,

    BettorProfile,

    SportType,

    utc_now,

)

from betdoc.domain.intelligence.advisor_models import (

    ActionType,

    ForecastVerdict,

    MarketOpportunity,

    RejectionReason,

    SessionForecast,

    TwinRecommendation,

    VolatilityRegime,

)

from betdoc.domain.intelligence.news_models import (

    ProbabilityModifier,

    compose_modifiers,

)



__all__ = [

    "KellySizer",

    "SizingOutcome",

    "TwinAdvisorConfig",

    "TwinAdvisorService",

    "TwinSessionResult",

    "protective_kelly_fraction",

]



_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(

    component="services.advisor.twin_engine"

)



_EPS: Final[float] = 1e-12





# --------------------------------------------------------------------------- #

# Math-engine hook

# --------------------------------------------------------------------------- #





@runtime_checkable

class KellySizer(Protocol):

    """Injection point for the real math engine.



    Satisfied by an adapter over ``betdoc.domain.math.portfolio_kelly`` or

    ``ev_filter``. A Protocol rather than an ABC so the math engine needs no

    knowledge of the intelligence layer, keeping the dependency arrow pointing

    one way only.

    """



    def kelly_fraction(self, probability: float, decimal_odds: float) -> float:

        """Full-Kelly fraction for one binary bet, in ``[0, 1]``."""

        ...





class _BuiltinKellySizer:

    """Closed-form binary Kelly. Used when no engine is injected.



    Deliberately the exact discrete formula ``(p*b - q) / b`` rather than the

    mean-variance form ``mu / sigma^2``. The uncertainty penalty is then

    applied as a division of the resulting fraction, which is the exact

    mean-variance-equivalent of inflating the variance, without inheriting the

    Taylor-expansion error of the quadratic form at larger stakes.

    """



    @staticmethod

    def kelly_fraction(probability: float, decimal_odds: float) -> float:

        if not 0.0 < probability < 1.0:

            msg = f"probability must lie strictly in (0, 1), got {probability!r}"

            raise ValueError(msg)

        if not decimal_odds > 1.0 or not math.isfinite(decimal_odds):

            msg = f"decimal_odds must be finite and above 1.0, got {decimal_odds!r}"

            raise ValueError(msg)

        b = decimal_odds - 1.0

        fraction = (probability * b - (1.0 - probability)) / b

        return min(max(fraction, 0.0), 1.0)





def protective_kelly_fraction(

    *,

    probability: float,

    decimal_odds: float,

    kelly_multiplier: float,

    uncertainty_penalty: float,

    sizer: KellySizer | None = None,

) -> tuple[float, float]:

    """Full Kelly and the protective fraction actually authorised.



    ``f_final = f_full * kelly_multiplier / lambda``



    Two independent reductions, deliberately kept separate so a post-mortem

    can attribute the size of a stake to the right cause:



    * ``kelly_multiplier`` is **policy**. It comes from the bettor's volatility

      tolerance and is constant for the session. It exists because expected log

      growth is concave with its root at twice the optimum, so over-staking

      costs far more than under-staking, and ``p`` is always estimated.

    * ``lambda`` is **information quality**. It comes from the news overlay and

      varies per opportunity. Dividing by it is exactly equivalent to

      multiplying the return variance by it in the mean-variance optimum.



    Returns:

        ``(full_kelly_fraction, protective_fraction)``, both in ``[0, 1]``.



    Raises:

        ValueError: Any input is outside its permitted domain.

    """

    if not 0.0 < kelly_multiplier <= 1.0:

        msg = f"kelly_multiplier must lie in (0, 1], got {kelly_multiplier!r}"

        raise ValueError(msg)

    if uncertainty_penalty < 1.0 or not math.isfinite(uncertainty_penalty):

        msg = f"uncertainty_penalty must be finite and at least 1.0, got {uncertainty_penalty!r}"

        raise ValueError(msg)



    engine = sizer or _BuiltinKellySizer()

    full = engine.kelly_fraction(probability, decimal_odds)

    protective = (full * kelly_multiplier) / uncertainty_penalty

    return full, min(max(protective, 0.0), 1.0)





# --------------------------------------------------------------------------- #

# Configuration and results

# --------------------------------------------------------------------------- #



_STRICT: Final[ConfigDict] = ConfigDict(

    frozen=True,

    extra="forbid",

    validate_default=True,

    revalidate_instances="never",

)





class TwinAdvisorConfig(BaseModel):

    """Thresholds for the firewall. Every gate is explicit and auditable."""



    model_config = _STRICT



    min_stake_fraction: float = Field(

        default=0.002,

        gt=0.0,

        le=0.5,

        description=(

            "Minimum authorised fraction of deployable capital. Doubles as the "

            "robust-edge gate: mu must clear f_min * lambda * sigma^2, so the "

            "required edge scales with the uncertainty attached to it."

        ),

    )

    min_edge_per_unit: float = Field(

        default=0.005,

        ge=0.0,

        le=1.0,

        description="Absolute EV floor per unit staked, before uncertainty scaling.",

    )

    extreme_penalty_threshold: float = Field(

        default=4.0,

        gt=1.0,

        le=10.0,

        description=(

            "Composed penalty at which the Twin stops trading the market "

            "entirely. At 4.0 the authorised stake is already a quarter of "

            "nominal; beyond it the number is inside execution noise and the "

            "honest answer is to wait for confirmation."

        ),

    )

    max_odds_age_seconds: float = Field(default=15.0, gt=0.0, le=3_600.0)

    max_account_state_age_seconds: float = Field(default=120.0, gt=0.0, le=3_600.0)

    min_entity_resolution_confidence: float = Field(default=0.80, ge=0.0, le=1.0)

    min_stake_paise: int = Field(

        default=1_000,

        gt=0,

        description="Below one bookmaker minimum, a stake is not placeable.",

    )

    session_capital_utilisation_cap: float = Field(

        default=0.25,

        gt=0.0,

        le=1.0,

        description="Ceiling on total session stake as a fraction of settled cash.",

    )



    @model_validator(mode="after")

    def _gates_are_coherent(self) -> Self:

        if self.min_stake_fraction >= self.session_capital_utilisation_cap:

            msg = (

                "min_stake_fraction must sit below the session utilisation cap, "

                "otherwise a single bet could exhaust the entire session budget"

            )

            raise ValueError(msg)

        return self





class SizingOutcome(BaseModel):

    """Intermediate sizing result, retained for audit and for the forecast."""



    model_config = _STRICT



    opportunity_id: str

    news_adjusted_probability: float = Field(gt=0.0, lt=1.0)

    uncertainty_penalty: float = Field(ge=1.0)

    edge_per_unit: float

    full_kelly_fraction: float = Field(ge=0.0, le=1.0)

    protective_fraction: float = Field(ge=0.0, le=1.0)

    uncapped_stake_paise: int = Field(ge=0)

    final_stake_paise: int = Field(ge=0)

    binding_constraint: str





class TwinSessionResult(BaseModel):

    """Everything the Twin produced for one evaluation pass."""



    model_config = _STRICT



    forecast: SessionForecast

    recommendations: tuple[TwinRecommendation, ...]

    sizing_audit: tuple[SizingOutcome, ...]



    @property

    def actionable(self) -> tuple[TwinRecommendation, ...]:

        return tuple(

            item for item in self.recommendations if item.action_type.requires_stake

        )



    @property

    def refusals(self) -> tuple[TwinRecommendation, ...]:

        return tuple(

            item

            for item in self.recommendations

            if item.action_type is ActionType.PASS

        )





# --------------------------------------------------------------------------- #

# The service

# --------------------------------------------------------------------------- #





class TwinAdvisorService:

    """The master filter pipeline. Pure, synchronous, deterministic.



    Deterministic given its inputs plus an injected ``now``, which is what

    makes the firewall testable: the same market and the same profile always

    produce the same verdicts, so a rejection can be reproduced exactly during

    a post-mortem.

    """



    __slots__ = ("_config", "_sizer")



    def __init__(

        self,

        config: TwinAdvisorConfig | None = None,

        sizer: KellySizer | None = None,

    ) -> None:

        """

        Args:

            config: Firewall thresholds. Cautious defaults.

            sizer: Math-engine adapter. Falls back to closed-form binary Kelly

                so the firewall keeps working if the solver is unavailable.

        """

        self._config = config or TwinAdvisorConfig()

        self._sizer = sizer or _BuiltinKellySizer()



    # ---------------------------- public entry point --------------------- #



    def evaluate_session(

        self,

        *,

        profile: BettorProfile,

        account_state: AccountState,

        opportunities: Sequence[MarketOpportunity],

        modifiers: Sequence[ProbabilityModifier] = (),

        realized_pnl_today_paise: int = 0,

        trace_id: str = "",

        now: datetime | None = None,

    ) -> TwinSessionResult:

        """Run the full pipeline and return recommendations plus a forecast.



        Args:

            profile: The bettor's declared policy and measured history.

            account_state: Live snapshot for the account being traded.

            opportunities: Candidates from the scanner, already devigged.

            modifiers: Active news modifiers. Expired ones are ignored safely.

            realized_pnl_today_paise: Settled profit or loss so far today.

                Negative means a loss. Consumes the daily loss budget.

            trace_id: Correlation id, bound to every emitted log event.

            now: Evaluation instant. Injected for deterministic tests.



        Returns:

            A :class:`TwinSessionResult`. Every opportunity receives a verdict;

            nothing is silently dropped.

        """

        moment = now or utc_now()

        resolved_trace = trace_id or uuid.uuid4().hex

        log = _log.bind(

            trace_id=resolved_trace,

            bookmaker=account_state.bookmaker.value,

            profile_id=profile.profile_id,

        )



        # ---- Stage 1: state ingestion ---------------------------------- #

        gate = self._check_account_usability(profile, account_state, moment, log)

        if gate is not None:

            return self._halted_session(

                reason=gate,

                profile=profile,

                account_state=account_state,

                opportunities=opportunities,

                modifiers=modifiers,

                realized_pnl_today_paise=realized_pnl_today_paise,

                trace_id=resolved_trace,

                now=moment,

                log=log,

            )



        # ---- Stage 2: loss budget (hard capital constraint) ------------- #

        loss_budget = self.remaining_loss_budget_paise(

            profile=profile,

            account_state=account_state,

            realized_pnl_today_paise=realized_pnl_today_paise,

        )

        if loss_budget <= 0:

            log.warning(

                "twin.session_halted",

                reason=RejectionReason.DAILY_LOSS_LIMIT_REACHED.value,

                daily_loss_limit_paise=profile.daily_loss_limit_paise,

                realized_pnl_today_paise=realized_pnl_today_paise,

                unsettled_exposure_paise=account_state.unsettled_exposure_paise,

                detail="worst case of pending bets already exhausts the budget",

            )

            return self._halted_session(

                reason=RejectionReason.DAILY_LOSS_LIMIT_REACHED,

                profile=profile,

                account_state=account_state,

                opportunities=opportunities,

                modifiers=modifiers,

                realized_pnl_today_paise=realized_pnl_today_paise,

                trace_id=resolved_trace,

                now=moment,

                log=log,

            )



        # ---- Stages 3 to 5, greedily in descending edge order ----------- #

        deployable = account_state.deployable_capital_paise

        session_budget = min(

            deployable,

            int(deployable * self._config.session_capital_utilisation_cap),

            loss_budget,

        )



        committed_by_sport: dict[SportType, int] = dict(

            account_state.exposure_by_sport_paise

        )

        session_committed = 0



        recommendations: list[TwinRecommendation] = []

        audits: list[SizingOutcome] = []

        penalties: list[float] = []



        ranked = sorted(opportunities, key=lambda item: item.ev_per_unit, reverse=True)

        for opportunity in ranked:

            scoped = self._scope_modifiers(opportunity, modifiers, moment)

            adjusted_probability, penalty = compose_modifiers(

                opportunity.fair_probability, scoped, now=moment

            )

            penalties.append(penalty)



            verdict = self._evaluate_opportunity(

                opportunity=opportunity,

                profile=profile,

                account_state=account_state,

                adjusted_probability=adjusted_probability,

                uncertainty_penalty=penalty,

                scoped_modifiers=scoped,

                deployable_paise=deployable,

                remaining_session_budget_paise=session_budget - session_committed,

                committed_by_sport=committed_by_sport,

                trace_id=resolved_trace,

                now=moment,

                log=log,

            )

            recommendation, audit = verdict

            recommendations.append(recommendation)

            if audit is not None:

                audits.append(audit)



            if recommendation.action_type.requires_stake:

                stake = recommendation.suggested_stake_paise

                session_committed += stake

                committed_by_sport[opportunity.sport_type] = (

                    committed_by_sport.get(opportunity.sport_type, 0) + stake

                )



        forecast = self._build_forecast(

            recommendations=tuple(recommendations),

            penalties=tuple(penalties),

            opportunities_screened=len(opportunities),

            deployable_paise=deployable,

            remaining_loss_budget_paise=max(loss_budget - session_committed, 0),

            active_news_alerts=len(

                [item for item in modifiers if item.is_active(now=moment)]

            ),

            trace_id=resolved_trace,

            now=moment,

        )



        log.info(

            "twin.session_evaluated",

            verdict=forecast.verdict.value,

            volatility_regime=forecast.volatility_regime.value,

            screened=forecast.opportunities_screened,

            issued=forecast.recommendations_issued,

            total_stake_paise=forecast.total_recommended_stake_paise,

            mean_uncertainty_penalty=round(forecast.mean_uncertainty_penalty, 4),

            remaining_loss_budget_paise=forecast.remaining_loss_budget_paise,

        )

        return TwinSessionResult(

            forecast=forecast,

            recommendations=tuple(recommendations),

            sizing_audit=tuple(audits),

        )



    # ------------------------------ stage 1 ------------------------------ #



    def _check_account_usability(

        self,

        profile: BettorProfile,

        account_state: AccountState,

        now: datetime,

        log: structlog.stdlib.BoundLogger,

    ) -> RejectionReason | None:

        """Refuse to trade against an unusable account snapshot.



        Staleness is a hard gate rather than a warning. Sizing against a

        balance that has already moved is how an account ends up over-staked,

        and the optimiser has no way to detect that from the numbers alone.

        """

        age = account_state.staleness_seconds(now=now)

        if age > self._config.max_account_state_age_seconds:

            log.warning(

                "twin.rejected",

                reason=RejectionReason.STALE_ACCOUNT_STATE.value,

                age_seconds=round(age, 3),

                max_age_seconds=self._config.max_account_state_age_seconds,

            )

            return RejectionReason.STALE_ACCOUNT_STATE



        if account_state.is_limited:

            log.warning(

                "twin.rejected",

                reason=RejectionReason.ACCOUNT_LIMITED_OR_UNAVAILABLE.value,

                detail="venue has restricted accepted stakes on this account",

            )

            return RejectionReason.ACCOUNT_LIMITED_OR_UNAVAILABLE



        if account_state.deployable_capital_paise < self._config.min_stake_paise:

            log.warning(

                "twin.rejected",

                reason=RejectionReason.INSUFFICIENT_DEPLOYABLE_CAPITAL.value,

                deployable_paise=account_state.deployable_capital_paise,

                min_stake_paise=self._config.min_stake_paise,

                detail="settled cash below the minimum placeable stake",

            )

            return RejectionReason.INSUFFICIENT_DEPLOYABLE_CAPITAL



        if profile.daily_loss_limit_paise <= 0:  # pragma: no cover - validated upstream

            return RejectionReason.DAILY_LOSS_LIMIT_REACHED

        return None



    # ------------------------------ stage 2 ------------------------------ #



    @staticmethod

    def remaining_loss_budget_paise(

        *,

        profile: BettorProfile,

        account_state: AccountState,

        realized_pnl_today_paise: int,

    ) -> int:

        """Loss budget left today, counting pending bets at their worst case.



        ``budget = limit - realized_loss_today - unsettled_exposure``



        The third term is the loophole this closes. A naive check compares the

        limit against realized loss only, which lets a bettor who is flat on

        the day but holding their entire limit in pending stakes keep placing

        bets. If those pending bets lose, the limit is breached by a multiple,

        and it is breached in a single settlement window with no opportunity to

        intervene. Counting unsettled exposure as an already-incurred worst

        case is the only formulation that makes the limit binding.



        All arithmetic is exact integer paise, and the result is floored at

        zero so callers never see a negative budget.



        Returns:

            Remaining budget in paise. Zero means stop.

        """

        realized_loss = max(0, -realized_pnl_today_paise)

        budget = (

            profile.daily_loss_limit_paise

            - realized_loss

            - account_state.unsettled_exposure_paise

        )

        return max(budget, 0)



    # ------------------------------ stage 3 ------------------------------ #



    def _scope_modifiers(

        self,

        opportunity: MarketOpportunity,

        modifiers: Sequence[ProbabilityModifier],

        now: datetime,

    ) -> tuple[ProbabilityModifier, ...]:

        """Select the modifiers that legitimately apply to this opportunity.



        Filtered on three conditions, all of which must hold:



        #. Still active (decay factor above zero).

        #. Scoped to this fixture and market.

        #. Resolved with sufficient confidence.



        The third is the important one. A low-confidence entity resolution is

        discarded rather than applied at reduced weight, because the error mode

        is categorical, not proportional: a wrong ``fixture_id`` applies one

        match's news to another match's prices, and no amount of down-weighting

        makes that partially correct.

        """

        minimum = self._config.min_entity_resolution_confidence

        return tuple(

            modifier

            for modifier in modifiers

            if modifier.is_active(now=now)

            and modifier.applies_to(

                fixture_id=opportunity.fixture_id,

                market_key=opportunity.market_key,

            )

            and modifier.entities.resolution_confidence >= minimum

        )



    # --------------------------- stages 4 and 5 -------------------------- #



    def _evaluate_opportunity(

        self,

        *,

        opportunity: MarketOpportunity,

        profile: BettorProfile,

        account_state: AccountState,

        adjusted_probability: float,

        uncertainty_penalty: float,

        scoped_modifiers: tuple[ProbabilityModifier, ...],

        deployable_paise: int,

        remaining_session_budget_paise: int,

        committed_by_sport: dict[SportType, int],

        trace_id: str,

        now: datetime,

        log: structlog.stdlib.BoundLogger,

    ) -> tuple[TwinRecommendation, SizingOutcome | None]:

        """Apply every gate in order, then size what survives."""

        entry = log.bind(

            opportunity_id=opportunity.opportunity_id,

            sport=opportunity.sport_type.value,

            market=opportunity.market_key,

        )



        def refuse(reason: RejectionReason, detail: str) -> TwinRecommendation:

            entry.warning(

                "twin.rejected",

                reason=reason.value,

                detail=detail,

                uncertainty_penalty=round(uncertainty_penalty, 4),

                news_adjusted_probability=round(adjusted_probability, 6),

            )

            return self._pass_recommendation(

                opportunity=opportunity,

                reason=reason,

                detail=detail,

                adjusted_probability=adjusted_probability,

                uncertainty_penalty=uncertainty_penalty,

                trace_id=trace_id,

                now=now,

            )



        # --- profile eligibility --- #

        if not profile.is_sport_permitted(opportunity.sport_type):

            return refuse(

                RejectionReason.SPORT_EXCLUDED_BY_PROFILE,

                f"{opportunity.sport_type.value} is excluded by your profile",

            ), None



        if opportunity.is_parlay and not profile.allow_parlays:

            return refuse(

                RejectionReason.PARLAYS_DISABLED,

                "parlays are disabled in your profile",

            ), None



        if opportunity.is_parlay and opportunity.leg_count > profile.max_parlay_legs:

            return refuse(

                RejectionReason.PARLAYS_DISABLED,

                f"{opportunity.leg_count} legs exceeds your maximum of "

                f"{profile.max_parlay_legs}",

            ), None



        # --- freshness --- #

        odds_age = opportunity.age_seconds(now=now)

        if odds_age > self._config.max_odds_age_seconds:

            return refuse(

                RejectionReason.STALE_ODDS,

                f"quote is {odds_age:.1f}s old, above the "

                f"{self._config.max_odds_age_seconds:.0f}s limit",

            ), None



        # --- news volatility --- #

        if uncertainty_penalty >= self._config.extreme_penalty_threshold:

            return refuse(

                RejectionReason.EXTREME_NEWS_VOLATILITY,

                f"unverified news has inflated variance {uncertainty_penalty:.2f}x, "

                f"which would cut the stake to "

                f"{100.0 / uncertainty_penalty:.0f}% of nominal; wait for confirmation",

            ), None



        # --- expected value, post-news --- #

        edge = adjusted_probability * opportunity.offered_odds - 1.0

        if edge <= 0.0:

            return refuse(

                RejectionReason.NEGATIVE_EV,

                f"post-news EV is {edge:+.4f} per unit staked",

            ), None

        if edge < self._config.min_edge_per_unit:

            return refuse(

                RejectionReason.EV_BELOW_MINIMUM_EDGE,

                f"edge {edge:.4f} is below the {self._config.min_edge_per_unit:.4f} "

                "floor and would not survive execution friction",

            ), None



        # --- robust edge: the required edge scales with the uncertainty --- #

        # f*_lambda >= f_min  <=>  mu >= f_min * lambda * sigma^2

        variance = adjusted_probability * (1.0 - adjusted_probability) * (

            opportunity.offered_odds**2

        )

        required_edge = (

            self._config.min_stake_fraction * uncertainty_penalty * variance

        )

        if edge < required_edge:

            return refuse(

                RejectionReason.FAILS_ROBUST_EV,

                f"edge {edge:.4f} is below the {required_edge:.4f} required at a "

                f"{uncertainty_penalty:.2f}x variance penalty; the edge exists only "

                "if the probability estimate is exactly right",

            ), None



        # --- protective Kelly --- #

        full_fraction, protective_fraction = protective_kelly_fraction(

            probability=adjusted_probability,

            decimal_odds=opportunity.offered_odds,

            kelly_multiplier=profile.kelly_fraction_multiplier,

            uncertainty_penalty=uncertainty_penalty,

            sizer=self._sizer,

        )

        uncapped_stake = (deployable_paise * int(protective_fraction * 1_000_000)) // 1_000_000



        # --- caps, in exact integer paise, most binding wins --- #

        sport_cap = profile.exposure_cap_for_sport_paise(opportunity.sport_type)

        sport_headroom = max(

            sport_cap - committed_by_sport.get(opportunity.sport_type, 0), 0

        )

        venue_cap = (

            opportunity.max_accepted_stake_paise

            if opportunity.max_accepted_stake_paise is not None

            else account_state.max_accepted_stake_paise

        )



        candidates: list[tuple[str, int]] = [

            ("protective_kelly", uncapped_stake),

            ("single_stake_cap", profile.max_single_stake_paise),

            ("sport_exposure_headroom", sport_headroom),

            ("session_budget", max(remaining_session_budget_paise, 0)),

            ("deployable_capital", deployable_paise),

        ]

        if venue_cap is not None:

            candidates.append(("venue_accepted_stake", venue_cap))



        binding, final_stake = min(candidates, key=lambda item: item[1])



        if sport_headroom <= 0:

            return refuse(

                RejectionReason.OVEREXPOSURE_ON_SPORT,

                f"{opportunity.sport_type.value} exposure of "

                f"{committed_by_sport.get(opportunity.sport_type, 0) / 100:.2f} INR "

                f"already meets your cap of {sport_cap / 100:.2f} INR",

            ), None



        if final_stake < self._config.min_stake_paise:

            reason = (

                RejectionReason.STAKE_ROUNDS_TO_ZERO

                if binding == "protective_kelly"

                else RejectionReason.EXCEEDS_SINGLE_STAKE_CAP

            )

            return refuse(

                reason,

                f"the mathematically correct stake of {final_stake / 100:.2f} INR is "

                f"below the {self._config.min_stake_paise / 100:.2f} INR minimum "

                f"(binding constraint: {binding})",

            ), None



        audit = SizingOutcome(

            opportunity_id=opportunity.opportunity_id,

            news_adjusted_probability=adjusted_probability,

            uncertainty_penalty=uncertainty_penalty,

            edge_per_unit=edge,

            full_kelly_fraction=full_fraction,

            protective_fraction=protective_fraction,

            uncapped_stake_paise=uncapped_stake,

            final_stake_paise=final_stake,

            binding_constraint=binding,

        )

        recommendation = self._actionable_recommendation(

            opportunity=opportunity,

            profile=profile,

            adjusted_probability=adjusted_probability,

            uncertainty_penalty=uncertainty_penalty,

            scoped_modifiers=scoped_modifiers,

            full_fraction=full_fraction,

            protective_fraction=protective_fraction,

            stake_paise=final_stake,

            edge=edge,

            binding=binding,

            trace_id=trace_id,

            now=now,

        )

        entry.info(

            "twin.recommended",

            action=recommendation.action_type.value,

            stake_paise=final_stake,

            binding_constraint=binding,

            uncertainty_penalty=round(uncertainty_penalty, 4),

            stake_reduction_from_news=round(

                recommendation.stake_reduction_from_news, 4

            ),

            full_kelly_fraction=round(full_fraction, 6),

            final_kelly_fraction=round(protective_fraction, 6),

        )

        return recommendation, audit



    # ---------------------------- construction --------------------------- #



    def _actionable_recommendation(

        self,

        *,

        opportunity: MarketOpportunity,

        profile: BettorProfile,

        adjusted_probability: float,

        uncertainty_penalty: float,

        scoped_modifiers: tuple[ProbabilityModifier, ...],

        full_fraction: float,

        protective_fraction: float,

        stake_paise: int,

        edge: float,

        binding: str,

        trace_id: str,

        now: datetime,

    ) -> TwinRecommendation:

        """Assemble an actionable recommendation with a readable justification."""

        expected_value_paise = math.floor(stake_paise * edge)

        action = ActionType.PARLAY if opportunity.is_parlay else ActionType.SINGLE

        return TwinRecommendation(

            recommendation_id=uuid.uuid4().hex,

            opportunity_id=opportunity.opportunity_id,

            trace_id=trace_id,

            generated_at=now,

            action_type=action,

            bookmaker=opportunity.bookmaker,

            sport_type=opportunity.sport_type,

            selection_label=opportunity.selection_label or opportunity.outcome_key,

            suggested_stake_paise=stake_paise,

            target_odds=opportunity.offered_odds,

            justification_string=self._build_justification(

                opportunity=opportunity,

                profile=profile,

                adjusted_probability=adjusted_probability,

                uncertainty_penalty=uncertainty_penalty,

                scoped_modifiers=scoped_modifiers,

                stake_paise=stake_paise,

                edge=edge,

                binding=binding,

            ),

            rejection_reason=None,

            fair_probability=opportunity.fair_probability,

            news_adjusted_probability=adjusted_probability,

            applied_uncertainty_penalty=uncertainty_penalty,

            full_kelly_fraction=full_fraction,

            final_kelly_fraction=protective_fraction,

            expected_value_paise=expected_value_paise,

            binding_constraint=binding,

        )



    def _pass_recommendation(

        self,

        *,

        opportunity: MarketOpportunity,

        reason: RejectionReason,

        detail: str,

        adjusted_probability: float,

        uncertainty_penalty: float,

        trace_id: str,

        now: datetime,

    ) -> TwinRecommendation:

        """A refusal, carried as data with the reason and the arithmetic."""

        return TwinRecommendation(

            recommendation_id=uuid.uuid4().hex,

            opportunity_id=opportunity.opportunity_id,

            trace_id=trace_id,

            generated_at=now,

            action_type=ActionType.PASS,

            bookmaker=opportunity.bookmaker,

            sport_type=opportunity.sport_type,

            selection_label=opportunity.selection_label or opportunity.outcome_key,

            suggested_stake_paise=0,

            target_odds=opportunity.offered_odds,

            justification_string=f"{reason.user_message} Detail: {detail}.",

            rejection_reason=reason,

            fair_probability=opportunity.fair_probability,

            news_adjusted_probability=adjusted_probability,

            applied_uncertainty_penalty=max(uncertainty_penalty, 1.0),

            full_kelly_fraction=0.0,

            final_kelly_fraction=0.0,

            expected_value_paise=0,

            binding_constraint=reason.value,

        )



    @staticmethod

    def _build_justification(

        *,

        opportunity: MarketOpportunity,

        profile: BettorProfile,

        adjusted_probability: float,

        uncertainty_penalty: float,

        scoped_modifiers: tuple[ProbabilityModifier, ...],

        stake_paise: int,

        edge: float,

        binding: str,

    ) -> str:

        """Explain the number in the user's language, not the model's.



        Present because an unexplained refusal or an unexplained small stake

        gets overridden, and an overridden firewall provides no protection.

        The news contribution is stated as a percentage of stake withheld,

        which is the only form in which the uncertainty penalty is intuitive.

        """

        parts: list[str] = [

            f"Fair probability {opportunity.fair_probability:.1%} at "

            f"{opportunity.offered_odds:.2f} gives a {edge:+.2%} edge per unit staked."

        ]

        if scoped_modifiers:

            withheld = 1.0 - (1.0 / uncertainty_penalty)

            shift = adjusted_probability - opportunity.fair_probability

            parts.append(

                f"{len(scoped_modifiers)} news item(s) moved the probability by "

                f"{shift:+.2%} to {adjusted_probability:.1%} and inflated variance "

                f"{uncertainty_penalty:.2f}x, withholding {withheld:.0%} of the "

                "stake because the information is not fully verified."

            )

        parts.append(

            f"Quarter-Kelly policy at your volatility tolerance of "

            f"{profile.volatility_tolerance:.2f} scales the growth-optimal "

            f"fraction by {profile.kelly_fraction_multiplier:.2f}."

        )

        parts.append(

            f"Final stake {stake_paise / 100:.2f} INR, set by {binding.replace('_', ' ')}."

        )

        return " ".join(parts)



    # ------------------------------ forecast ----------------------------- #



    def _build_forecast(

        self,

        *,

        recommendations: tuple[TwinRecommendation, ...],

        penalties: tuple[float, ...],

        opportunities_screened: int,

        deployable_paise: int,

        remaining_loss_budget_paise: int,

        active_news_alerts: int,

        trace_id: str,

        now: datetime,

    ) -> SessionForecast:

        """Aggregate the pass into one session-level stance."""

        actionable = [

            item for item in recommendations if item.action_type.requires_stake

        ]

        total_stake = sum(item.suggested_stake_paise for item in actionable)

        total_ev = sum(item.expected_value_paise for item in actionable)



        mean_penalty = (

            sum(penalties) / len(penalties) if penalties else 1.0

        )

        max_penalty = max(penalties, default=1.0)

        regime = VolatilityRegime.from_mean_penalty(mean_penalty)



        counts = Counter(

            item.rejection_reason

            for item in recommendations

            if item.rejection_reason is not None

        )

        rejections: dict[RejectionReason, int] = dict(counts)



        warnings: list[str] = []

        if regime is VolatilityRegime.EXTREME:

            warnings.append(

                "Market is in an extreme news-volatility regime. Unverified "

                "information is widening outcome distributions faster than edges "

                "can be trusted."

            )

        if remaining_loss_budget_paise < deployable_paise // 20:

            warnings.append(

                "Less than 5% of your daily loss budget remains once pending bets "

                "are counted at their worst case."

            )

        if any(

            reason.is_hard_capital_constraint for reason in rejections

        ):

            warnings.append(

                "One or more opportunities were refused on capital constraints "

                "rather than on expected value."

            )



        verdict = self._session_verdict(

            actionable_count=len(actionable),

            regime=regime,

            remaining_loss_budget_paise=remaining_loss_budget_paise,

            total_ev_paise=total_ev,

        )

        if not verdict.should_trade:

            total_stake = 0



        return SessionForecast(

            forecast_id=uuid.uuid4().hex,

            generated_at=now,

            trace_id=trace_id,

            verdict=verdict,

            volatility_regime=regime,

            opportunities_screened=opportunities_screened,

            recommendations_issued=len(actionable) if verdict.should_trade else 0,

            rejections_by_reason=rejections,

            total_recommended_stake_paise=total_stake,

            aggregate_expected_value_paise=total_ev if verdict.should_trade else 0,

            remaining_loss_budget_paise=remaining_loss_budget_paise,

            deployable_capital_paise=deployable_paise,

            active_news_alerts=active_news_alerts,

            mean_uncertainty_penalty=max(mean_penalty, 1.0),

            max_uncertainty_penalty=max(max_penalty, 1.0),

            warnings=tuple(warnings),

        )



    @staticmethod

    def _session_verdict(

        *,

        actionable_count: int,

        regime: VolatilityRegime,

        remaining_loss_budget_paise: int,

        total_ev_paise: int,

    ) -> ForecastVerdict:

        """Map the aggregate picture onto a stance.



        Gate order matters: an exhausted budget or an extreme volatility regime

        forces a stand-down regardless of how attractive the expected value

        looks. Edge never overrides a capital constraint.

        """

        if remaining_loss_budget_paise <= 0:

            return ForecastVerdict.STAND_DOWN

        if regime is VolatilityRegime.EXTREME:

            return ForecastVerdict.STAND_DOWN

        if actionable_count == 0:

            return ForecastVerdict.CAUTION

        if regime is VolatilityRegime.ELEVATED:

            return ForecastVerdict.CAUTION

        if total_ev_paise <= 0:

            return ForecastVerdict.CAUTION

        if regime is VolatilityRegime.CALM and actionable_count >= 3:

            return ForecastVerdict.OPTIMAL

        return ForecastVerdict.ACCEPTABLE



    def _halted_session(

        self,

        *,

        reason: RejectionReason,

        profile: BettorProfile,

        account_state: AccountState,

        opportunities: Sequence[MarketOpportunity],

        modifiers: Sequence[ProbabilityModifier],

        realized_pnl_today_paise: int,

        trace_id: str,

        now: datetime,

        log: structlog.stdlib.BoundLogger,

    ) -> TwinSessionResult:

        """Refuse the whole session, giving every opportunity the same verdict.



        Every candidate still receives an explicit PASS. A halted session that

        returned an empty list would be indistinguishable from a session with

        no opportunities, and the difference matters enormously to the user.

        """

        refusals = tuple(

            self._pass_recommendation(

                opportunity=opportunity,

                reason=reason,

                detail="session halted before individual evaluation",

                adjusted_probability=opportunity.fair_probability,

                uncertainty_penalty=1.0,

                trace_id=trace_id,

                now=now,

            )

            for opportunity in opportunities

        )

        budget = self.remaining_loss_budget_paise(

            profile=profile,

            account_state=account_state,

            realized_pnl_today_paise=realized_pnl_today_paise,

        )

        forecast = SessionForecast(

            forecast_id=uuid.uuid4().hex,

            generated_at=now,

            trace_id=trace_id,

            verdict=ForecastVerdict.STAND_DOWN,

            volatility_regime=VolatilityRegime.NORMAL,

            opportunities_screened=len(opportunities),

            recommendations_issued=0,

            rejections_by_reason={reason: len(opportunities)} if opportunities else {},

            total_recommended_stake_paise=0,

            aggregate_expected_value_paise=0,

            remaining_loss_budget_paise=budget,

            deployable_capital_paise=account_state.deployable_capital_paise,

            active_news_alerts=len(

                [item for item in modifiers if item.is_active(now=now)]

            ),

            mean_uncertainty_penalty=1.0,

            max_uncertainty_penalty=1.0,

            warnings=(reason.user_message,),

        )

        log.warning(

            "twin.session_stand_down",

            reason=reason.value,

            opportunities_screened=len(opportunities),

            remaining_loss_budget_paise=budget,

        )

        return TwinSessionResult(

            forecast=forecast, recommendations=refusals, sizing_audit=()

        )
