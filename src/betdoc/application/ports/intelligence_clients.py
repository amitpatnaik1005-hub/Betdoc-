"""Ports for the intelligence layer. The domain depends on these, not on I/O.



Every adapter that fetches account state or evaluates news satisfies one of

these ABCs, which is what lets the Twin be tested entirely with in-memory

fakes and lets a bookmaker be swapped without the advisor changing.



Error taxonomy lives here, not in the adapters. Retry, circuit-breaking and

alerting policy must be decided in one place, so adapters are required to

translate their transport-specific failures into these types.

"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from betdoc.domain.intelligence.account_models import (
    AccountState,
    BetRecord,
    LinkedBookmaker,
)
from betdoc.domain.intelligence.news_models import (
    LiveNewsAlert,
    ProbabilityModifier,
)

__all__ = [
    "AccountStateAdapter",
    "AccountUnavailableError",
    "IntelligenceError",
    "NewsImpactEvaluator",
    "NewsRateLimitError",
    "ReconciliationBreachError",
    "StaleAccountStateError",
    "TransientIntelligenceError",
    "UnparseableEntityError",
]


class IntelligenceError(Exception):
    """Base class for every fault raised by an intelligence adapter."""

    def __init__(self, message: str, **context: Any) -> None:

        super().__init__(message)

        self.context: dict[str, Any] = context

    def __str__(self) -> str:

        base = super().__str__()

        if not self.context:
            return base

        detail = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))

        return f"{base} [{detail}]"


class TransientIntelligenceError(IntelligenceError):
    """Retryable: timeout, connection reset, upstream 5xx.



    The only category the resilience layer is permitted to retry. Everything

    else is either a logic fault or a state that retrying cannot repair.

    """


class NewsRateLimitError(TransientIntelligenceError):
    """The news or NLP provider throttled us.



    Carries the provider's own backoff hint. Ignoring ``Retry-After`` and

    retrying on our own schedule is the fastest route to a hard block, so the

    hint must be respected when present.

    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retry_after_seconds: float | None = None,
        requests_remaining: int | None = None,
    ) -> None:

        super().__init__(
            message,
            provider=provider,
            retry_after_seconds=retry_after_seconds,
            requests_remaining=requests_remaining,
        )

        self.provider = provider

        self.retry_after_seconds = retry_after_seconds

        self.requests_remaining = requests_remaining


class UnparseableEntityError(IntelligenceError):
    """The alert could not be resolved to a fixture or team we know.



    Explicitly **not** transient. Retrying identical text against an

    unchanged entity registry produces an identical failure, so the caller must

    quarantine the alert rather than redeliver it. Acting on a guessed

    resolution is worse than dropping the alert: it applies one fixture's news

    to another fixture's prices.

    """

    def __init__(
        self,
        message: str,
        *,
        alert_id: str,
        raw_text_excerpt: str = "",
        best_candidate: str | None = None,
        best_confidence: float | None = None,
    ) -> None:

        super().__init__(
            message,
            alert_id=alert_id,
            raw_text_excerpt=raw_text_excerpt[:160],
            best_candidate=best_candidate,
            best_confidence=best_confidence,
        )

        self.alert_id = alert_id

        self.raw_text_excerpt = raw_text_excerpt

        self.best_candidate = best_candidate

        self.best_confidence = best_confidence


class AccountUnavailableError(IntelligenceError):
    """The bookmaker account cannot be read: auth failure, block, or maintenance.



    Not transient by default. An expired session or a restricted account will

    not fix itself, and repeated attempts against a flagged account accelerate

    the restriction.

    """

    def __init__(
        self, message: str, *, bookmaker: LinkedBookmaker, is_recoverable: bool = False
    ) -> None:

        super().__init__(message, bookmaker=bookmaker.value, is_recoverable=is_recoverable)

        self.bookmaker = bookmaker

        self.is_recoverable = is_recoverable


class StaleAccountStateError(IntelligenceError):
    """The snapshot is too old to size a bet against.



    Sizing on a stale balance is how an account gets over-staked: the cash the

    optimiser is allocating may already be committed to a bet placed since the

    snapshot was taken.

    """

    def __init__(
        self,
        message: str,
        *,
        bookmaker: LinkedBookmaker,
        age_seconds: float,
        max_age_seconds: float,
    ) -> None:

        super().__init__(
            message,
            bookmaker=bookmaker.value,
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
        )

        self.bookmaker = bookmaker

        self.age_seconds = age_seconds

        self.max_age_seconds = max_age_seconds


class ReconciliationBreachError(IntelligenceError):
    """Reported balances cannot be reconciled with known cash flows.



    A hard stop. Either the adapter mis-parsed the account or funds are

    unaccounted for, and both demand a human before any further placement.

    """

    def __init__(
        self,
        message: str,
        *,
        bookmaker: LinkedBookmaker,
        discrepancy_paise: int,
    ) -> None:

        super().__init__(message, bookmaker=bookmaker.value, discrepancy_paise=discrepancy_paise)

        self.bookmaker = bookmaker

        self.discrepancy_paise = discrepancy_paise


class AccountStateAdapter(abc.ABC):
    """Read-only interface to one real-world bookmaker account.



    Read-only by design. Nothing in this port can place, amend or cancel a bet.

    Placement is a separate capability with a separate port, a separate risk

    gate and separate credentials, so an intelligence bug cannot move money.



    Implementation rules:



    #. Return integer paise. Any parsing of a decimal string happens in the

       adapter and the result is exact.

    #. Translate every transport fault into the taxonomy above.

    #. Never mutate a returned model. Every domain model here is frozen.

    #. Honour cancellation: ``asyncio.CancelledError`` must propagate.

    """

    bookmaker: LinkedBookmaker

    def __init__(self, bookmaker: LinkedBookmaker) -> None:

        self.bookmaker = bookmaker

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish the session. Must be idempotent."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources. Must be safe to call twice and after failure."""

    async def __aenter__(self) -> Self:

        await self.connect()

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:

        await self.close()

    @abc.abstractmethod
    async def fetch_account_state(self) -> AccountState:
        """Current balances and exposure.



        Returns:

            A frozen :class:`AccountState` with realized balance and unsettled

            exposure reported separately.



        Raises:

            AccountUnavailableError: Auth failure, restriction, or maintenance.

            TransientIntelligenceError: Retryable transport fault.

            ReconciliationBreachError: Balances contradict known cash flows.

        """

    @abc.abstractmethod
    async def fetch_active_bets(self) -> tuple[BetRecord, ...]:
        """Every unsettled bet on the account.



        The authoritative source for exposure. Deriving exposure from our own

        placement log instead would miss any bet placed manually, outside the

        system, which is exactly the exposure the firewall most needs to see.

        """

    @abc.abstractmethod
    async def fetch_settled_bets(
        self, *, since: datetime, limit: int = 500
    ) -> tuple[BetRecord, ...]:
        """Settled bets from ``since`` onwards, for profile construction.



        Args:

            since: Inclusive lower bound. Must be timezone-aware.

            limit: Maximum records to return.

        """

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Cheap liveness probe. Must not raise; report ``False`` instead."""

    async def fetch_max_accepted_stake_paise(self) -> int | None:
        """Current stake ceiling, when the venue exposes one.



        Default returns ``None``. Override where available: a declining

        ceiling is the earliest reliable signal of account limitation, and it

        appears weeks before an outright ban.

        """

        return None


class NewsImpactEvaluator(abc.ABC):
    """Turns a resolved news alert into a quantified probability modifier.



    This is the only component permitted to convert text into numbers, and it

    is bound by two hard rules that exist to protect the bankroll:



    #. **The uncertainty penalty may never fall below the alert's own

       credibility floor** (:meth:`LiveNewsAlert.baseline_uncertainty_penalty`).

       An evaluator that is confident about the *magnitude* of a rumour is

       still not entitled to be confident about its *truth*. Allowing the model

       to undercut the floor would let a well-written rumour be staked as

       though it were a club announcement, which is the exact failure the

       penalty exists to prevent.

    #. **An unreliable entity resolution must raise, not guess.** Returning a

       modifier for a fixture we are not confident about does not produce a

       slightly wrong price, it applies one match's injury news to a different

       match's odds. Silence is strictly safer than a plausible guess.



    Implementation rules:



    * Pure output: return a frozen :class:`ProbabilityModifier` and mutate

      nothing.

    * Deterministic where possible. If the underlying model is stochastic, seed

      it, because a risk figure that changes between two runs of the same input

      is not auditable.

    * Never let a single unparseable alert abort a batch. Use

      :meth:`evaluate_batch`, which quarantines individually.

    """

    provider_name: str

    def __init__(self, provider_name: str) -> None:

        self.provider_name = provider_name

    @abc.abstractmethod
    async def evaluate(self, alert: LiveNewsAlert) -> ProbabilityModifier:
        """Quantify one alert's effect on the affected market probabilities.



        Args:

            alert: A resolved alert. Callers should check

                :attr:`LiveNewsAlert.is_actionable` first.



        Returns:

            A frozen :class:`ProbabilityModifier` whose

            ``uncertainty_penalty`` is at least

            ``alert.baseline_uncertainty_penalty()``.



        Raises:

            UnparseableEntityError: The alert cannot be tied to a known

                fixture or team with sufficient confidence. Permanent: the

                caller must quarantine rather than retry.

            NewsRateLimitError: The provider throttled the request. Retryable,

                honouring ``retry_after_seconds``.

            TransientIntelligenceError: Retryable transport fault.

        """

    async def evaluate_batch(
        self, alerts: Sequence[LiveNewsAlert]
    ) -> tuple[tuple[ProbabilityModifier, ...], tuple[UnparseableEntityError, ...]]:
        """Evaluate many alerts, quarantining individual failures.



        Returns both the successes and the failures rather than raising,

        because a single malformed alert must never discard the other

        nineteen good ones in the same batch.



        Returns:

            ``(modifiers, quarantined_errors)``.

        """

        modifiers: list[ProbabilityModifier] = []

        failures: list[UnparseableEntityError] = []

        for alert in alerts:
            try:
                modifiers.append(await self.evaluate(alert))

            except UnparseableEntityError as exc:
                failures.append(exc)

        return tuple(modifiers), tuple(failures)

    def enforce_penalty_floor(
        self, alert: LiveNewsAlert, modifier: ProbabilityModifier
    ) -> ProbabilityModifier:
        """Raise the penalty to the credibility floor if the model undercut it.



        Concrete implementations should call this on their own output as the

        last step. It is a belt-and-braces guard: the rule is stated in the

        contract, and enforced here so a model regression cannot quietly

        violate it.

        """

        floor = alert.baseline_uncertainty_penalty()

        if modifier.uncertainty_penalty >= floor:
            return modifier

        return modifier.model_copy(update={"uncertainty_penalty": floor})

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Cheap liveness probe. Must not raise; report ``False`` instead."""
