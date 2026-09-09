from __future__ import annotations
import asyncio
import math
from typing import Final

import structlog

# FIXING IMPORTS FOR OUR ARCHITECTURE
from betdoc.domain.intelligence.account_models import AccountState, BettorProfile
from betdoc.domain.intelligence.advisor_models import RejectionReason, MarketOpportunity
from betdoc.domain.risk.models import (
    ConstraintBreakdown, RiskDecision, RiskPolicy, RiskVerdict, StakeRequest
)
from betdoc.domain.risk.ports import ExposureLedgerPort, VolatilityOraclePort
from betdoc.domain.shared.clock import Clock, SystemClock
from betdoc.domain.shared.money import BPS_DENOMINATOR, apply_bps, clamp_non_negative

_NO_HEADROOM: Final[ConstraintBreakdown] = ConstraintBreakdown(
    single_stake_cap_paise=0, sport_exposure_headroom_paise=0,
    daily_loss_headroom_paise=0, settled_liquidity_paise=0,
)

class RiskGovernanceService:
    __slots__ = ("_clock", "_ledger", "_log", "_policy", "_volatility")

    def __init__(self, *, ledger: ExposureLedgerPort, volatility: VolatilityOraclePort,
                 policy: RiskPolicy | None = None, clock: Clock | None = None) -> None:
        self._ledger = ledger
        self._volatility = volatility
        self._policy = policy if policy is not None else RiskPolicy()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._log: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger("betdoc.risk")

    async def evaluate(self, *, profile: BettorProfile, account: AccountState, request: StakeRequest) -> RiskDecision:
        now = self._clock.now()
        opportunity = request.opportunity
        log = self._log.bind(
            profile_id=profile.profile_id, opportunity_id=opportunity.opportunity_id,
            bookmaker=account.bookmaker, sport_type=opportunity.sport_type,
            selection_label=opportunity.selection_label,
        )
        expected_value_bps = self._expected_value_bps(opportunity)

        quote_age_seconds = (now - opportunity.quoted_at).total_seconds()
        if quote_age_seconds > self._policy.max_quote_age.total_seconds():
            return self._reject(
                reason=RejectionReason.STALE_QUOTE if hasattr(RejectionReason, 'STALE_QUOTE') else RejectionReason.UNACCEPTABLE_VOLATILITY, # Fallback mapping
                detail=f"quote age {quote_age_seconds:.3f}s exceeds permitted {self._policy.max_quote_age.total_seconds():.3f}s",
                request=request, expected_value_bps=expected_value_bps, constraints=_NO_HEADROOM, now=now, log=log,
            )

        if expected_value_bps < self._policy.min_expected_value_bps:
            return self._reject(
                reason=RejectionReason.NEGATIVE_EV,
                detail=f"edge {expected_value_bps}bps below floor {self._policy.min_expected_value_bps}bps",
                request=request, expected_value_bps=expected_value_bps, constraints=_NO_HEADROOM, now=now, log=log,
            )

        volatility_detail = self._volatility_breach(instrument_key=request.instrument_key, tolerance=profile.volatility_tolerance, log=log)
        if volatility_detail is not None:
            return self._reject(
                reason=RejectionReason.UNACCEPTABLE_VOLATILITY,
                detail=volatility_detail, request=request, expected_value_bps=expected_value_bps,
                constraints=_NO_HEADROOM, now=now, log=log,
            )

        realized_loss_paise, sport_exposure_paise = await asyncio.gather(
            self._ledger.realized_loss_today_paise(profile_id=profile.profile_id, as_of=now),
            self._ledger.open_exposure_paise(profile_id=profile.profile_id, sport_type=opportunity.sport_type),
        )
        constraints = self._headroom(
            profile=profile, account=account,
            realized_loss_paise=self._sanitise_ledger_value(realized_loss_paise, field="realized_loss_today_paise", log=log),
            sport_exposure_paise=self._sanitise_ledger_value(sport_exposure_paise, field="open_exposure_paise", log=log),
        )

        caps: tuple[tuple[int, RejectionReason], ...] = (
            (constraints.daily_loss_headroom_paise, RejectionReason.DAILY_LOSS_LIMIT),
            (constraints.sport_exposure_headroom_paise, RejectionReason.EXPOSURE_LIMIT),
            (constraints.settled_liquidity_paise, RejectionReason.EXPOSURE_LIMIT),
            (constraints.single_stake_cap_paise, RejectionReason.SINGLE_STAKE_LIMIT),
        )
        permitted_paise, binding_reason = min(caps, key=lambda cap: cap[0])

        if permitted_paise < self._policy.min_viable_stake_paise:
            return self._reject(
                reason=binding_reason,
                detail=f"permitted stake {permitted_paise} paise below minimum viable {self._policy.min_viable_stake_paise} paise",
                request=request, expected_value_bps=expected_value_bps, constraints=constraints, now=now, log=log,
            )

        approved_paise = min(request.requested_stake_paise, permitted_paise)
        was_reduced = approved_paise < request.requested_stake_paise

        if was_reduced and not self._policy.allow_partial_downsizing:
            return self._reject(
                reason=binding_reason,
                detail=f"requested {request.requested_stake_paise} paise exceeds permitted {permitted_paise} paise and downsizing is disabled",
                request=request, expected_value_bps=expected_value_bps, constraints=constraints, now=now, log=log,
            )

        decision = RiskDecision(
            verdict=RiskVerdict.APPROVED_REDUCED if was_reduced else RiskVerdict.APPROVED,
            approved_stake_paise=approved_paise, requested_stake_paise=request.requested_stake_paise,
            rejection_reason=None, binding_constraint=binding_reason if was_reduced else None,
            detail=f"stake reduced to {approved_paise} paise by {binding_reason.value}" if was_reduced else "stake approved in full",
            expected_value_bps=expected_value_bps, constraints=constraints, decided_at=now,
        )
        log.info(
            "risk.decision", verdict=decision.verdict.value, approved_stake_paise=decision.approved_stake_paise,
            requested_stake_paise=decision.requested_stake_paise, expected_value_bps=decision.expected_value_bps,
            binding_constraint=binding_reason.value, permitted_stake_paise=permitted_paise,
        )
        return decision

    def _headroom(self, *, profile: BettorProfile, account: AccountState, realized_loss_paise: int, sport_exposure_paise: int) -> ConstraintBreakdown:
        remaining_daily_budget_paise = clamp_non_negative(profile.daily_loss_limit_paise - realized_loss_paise)
        return ConstraintBreakdown(
            single_stake_cap_paise=clamp_non_negative(profile.max_single_stake_paise),
            sport_exposure_headroom_paise=clamp_non_negative(profile.max_exposure_per_sport_paise - sport_exposure_paise),
            daily_loss_headroom_paise=apply_bps(remaining_daily_budget_paise, self._policy.daily_budget_utilisation_bps),
            settled_liquidity_paise=clamp_non_negative(account.realized_balance_paise - account.unsettled_exposure_paise),
        )

    @staticmethod
    def _expected_value_bps(opportunity: MarketOpportunity) -> int:
        expected_value = (opportunity.fair_probability * opportunity.offered_odds) - 1.0 # Added missing property logic directly here
        if not math.isfinite(expected_value):
            return -BPS_DENOMINATOR
        return math.floor(expected_value * BPS_DENOMINATOR)

    def _volatility_breach(self, *, instrument_key: str, tolerance: float, log: structlog.stdlib.BoundLogger) -> str | None:
        assessment = self._volatility.assess(instrument_key)
        if assessment is None:
            if self._policy.require_volatility_signal:
                return f"no volatility signal for instrument {instrument_key!r}; failing closed"
            log.warning("risk.volatility_signal_missing", instrument_key=instrument_key)
            return None
        if assessment.is_stale:
            return f"volatility window stale; newest tick at {assessment.assessed_at.isoformat()} exceeds freshness budget"
        if assessment.volatility_per_minute > tolerance:
            return f"realised volatility {assessment.volatility_per_minute:.6f}/min exceeds tolerance {tolerance:.6f} over {assessment.sample_count} ticks"
        return None

    @staticmethod
    def _sanitise_ledger_value(value_paise: int, *, field: str, log: structlog.stdlib.BoundLogger) -> int:
        if value_paise < 0:
            log.warning("risk.ledger_negative_value", field=field, value=value_paise)
            return 0
        return value_paise

    def _reject(self, *, reason: RejectionReason, detail: str, request: StakeRequest, expected_value_bps: int, constraints: ConstraintBreakdown, now: datetime, log: structlog.stdlib.BoundLogger) -> RiskDecision:
        decision = RiskDecision(
            verdict=RiskVerdict.REJECTED, approved_stake_paise=0, requested_stake_paise=request.requested_stake_paise,
            rejection_reason=reason, binding_constraint=reason, detail=detail,
            expected_value_bps=expected_value_bps, constraints=constraints, decided_at=now,
        )
        log.info(
            "risk.decision", verdict=RiskVerdict.REJECTED.value, rejection_reason=reason.value,
            detail=detail, requested_stake_paise=request.requested_stake_paise, expected_value_bps=expected_value_bps,
        )
        return decision
