"""Cross-section orchestrator: wires Vault, Oracle, Arena and Lab with math and risk engines."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.encryption import EncryptionService, mask_api_key
from app.domain.integration.adapters import PredictionModelPort, ResearchInput, RiskEnginePort
from app.domain.integration.models import BetRecord, BetStatus, PredictionRecord, utcnow
from app.domain.integration.repositories import (
    ARENA_LIVE_BETTING_FLAG,
    ARENA_SUBSYSTEM,
    ORACLE_AUTOMATION_FLAG,
    ORACLE_SUBSYSTEM,
    ArenaRepository,
    ConstraintRepository,
    LabRepository,
    OracleRepository,
    VaultRepository,
)
from app.domain.integration.schemas import (
    BetResult,
    CascadeResult,
    ConstraintChange,
    PredictionResult,
    StoredCredential,
    SubsystemMetrics,
    SubsystemName,
    SubsystemStatus,
    TelemetryResponse,
)

logger = logging.getLogger(__name__)

BetOutcome = Literal["won", "lost", "void"]


class PredictionUnavailableError(LookupError):
    """No research exists for the requested event."""


class BettingHaltedError(RuntimeError):
    """Live betting is suspended by an active constraint (e.g. stop-loss)."""


class InvalidBetError(ValueError):
    """The bet request violates domain rules."""


class OrchestratorService:
    """All collaborators are injected. The service never builds connections or settings itself.

    ``session`` is the request-scoped unit of work for writes. ``session_factory`` lets the
    concurrent telemetry fan-out use one short-lived session per subsystem, because an
    ``AsyncSession`` must never be used by concurrent tasks.
    """

    def __init__(
        self,
        session: AsyncSession,
        encryption: EncryptionService,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        vault: VaultRepository,
        oracle: OracleRepository,
        arena: ArenaRepository,
        lab: LabRepository,
        constraints: ConstraintRepository,
        prediction_model: PredictionModelPort,
        risk_engine: RiskEnginePort,
    ) -> None:
        self._session = session
        self._encryption = encryption
        self._session_factory = session_factory
        self._settings = settings
        self._vault = vault
        self._oracle = oracle
        self._arena = arena
        self._lab = lab
        self._constraints = constraints
        self._prediction_model = prediction_model
        self._risk_engine = risk_engine

    # ------------------------------------------------------------------ Vault
    async def store_api_key(self, provider: str, plain_key: str) -> StoredCredential:
        cipher = self._encryption.encrypt_data(plain_key)
        try:
            row = await self._vault.add_credential(self._session, provider=provider, encrypted_key=cipher)
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            raise
        return StoredCredential(id=row.id, provider=row.provider, masked_key=mask_api_key(plain_key, self._settings.mask_visible_chars))

    # ------------------------------------------------------------------ Telemetry
    async def gather_dashboard_telemetry(self) -> TelemetryResponse:
        fetchers: tuple[tuple[SubsystemName, Callable[[AsyncSession], Awaitable[SubsystemMetrics]]], ...] = (
            ("vault", self._vault.metrics),
            ("oracle", self._oracle_metrics),
            ("arena", self._arena_metrics),
            ("lab", self._lab.metrics),
        )
        results = await asyncio.gather(*(self._timed_fetch(fetch) for _, fetch in fetchers), return_exceptions=True)

        statuses: list[SubsystemStatus] = []
        realized_pnl: float | None = None
        for (name, _), outcome in zip(fetchers, results, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome  # never swallow cancellation / interpreter shutdown
                message = "Timeout" if isinstance(outcome, TimeoutError) else f"{type(outcome).__name__}: {outcome}"
                logger.error("Telemetry fetch failed for subsystem=%s: %s", name, message, exc_info=outcome)
                statuses.append(SubsystemStatus(name=name, status="error", last_error=message))
                continue
            metrics, latency_ms = outcome
            if metrics.kind == "arena":
                realized_pnl = metrics.realized_pnl
            statuses.append(SubsystemStatus(name=name, status="ok", latency_ms=latency_ms, metrics=metrics))

        failures = sum(s.status == "error" for s in statuses)
        overall: Literal["ok", "degraded", "down"] = "ok" if failures == 0 else ("down" if failures == len(statuses) else "degraded")
        return TelemetryResponse(generated_at=datetime.now(UTC), overall_status=overall, realized_pnl=realized_pnl, subsystems=statuses)

    async def _timed_fetch(self, fetch: Callable[[AsyncSession], Awaitable[SubsystemMetrics]]) -> tuple[SubsystemMetrics, float]:
        start = time.perf_counter()
        metrics = await asyncio.wait_for(self._with_isolated_session(fetch), timeout=self._settings.telemetry_timeout_seconds)
        return metrics, (time.perf_counter() - start) * 1000.0

    async def _with_isolated_session(self, fetch: Callable[[AsyncSession], Awaitable[SubsystemMetrics]]) -> SubsystemMetrics:
        async with self._session_factory() as session:
            return await fetch(session)

    async def _oracle_metrics(self, session: AsyncSession) -> SubsystemMetrics:
        enabled = await self._constraints.is_enabled(session, ORACLE_SUBSYSTEM, ORACLE_AUTOMATION_FLAG)
        return await self._oracle.metrics(session, automated_enabled=enabled)

    async def _arena_metrics(self, session: AsyncSession) -> SubsystemMetrics:
        enabled = await self._constraints.is_enabled(session, ARENA_SUBSYSTEM, ARENA_LIVE_BETTING_FLAG)
        return await self._arena.metrics(session, live_enabled=enabled)

    # ------------------------------------------------------------------ Oracle
    async def execute_oracle_prediction_cycle(self, event_id: str) -> PredictionResult:
        research = await self._oracle.get_research(self._session, event_id)
        if research is None:
            raise PredictionUnavailableError(f"No research available for event_id={event_id!r}.")
        automated = await self._constraints.is_enabled(self._session, ORACLE_SUBSYSTEM, ORACLE_AUTOMATION_FLAG)

        probability = await self._prediction_model.predict_probability(
            ResearchInput(expected_margin=research.expected_margin, spread_line=research.spread_line)
        )
        bankroll = self._settings.starting_bankroll + await self._arena.realized_pnl(self._session)
        exposure = await self._arena.open_exposure(self._session)
        decision = self._risk_engine.evaluate(
            probability=probability, decimal_odds=research.decimal_odds, bankroll=bankroll, open_exposure=exposure
        )
        if not automated:
            decision = replace(decision, approved=False, stake=0.0, reason="rejected: automated predictions suspended")

        record = PredictionRecord(
            event_id=event_id,
            model_name=self._prediction_model.name,
            probability=probability,
            implied_probability=decision.implied_probability,
            edge=decision.edge,
            decimal_odds=research.decimal_odds,
            recommended_stake=decision.stake,
            approved=decision.approved,
            risk_reason=decision.reason,
        )
        try:
            record = await self._oracle.add_prediction(self._session, record)
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            raise
        return PredictionResult.model_validate(record)

    # ------------------------------------------------------------------ Arena
    async def place_bet(self, prediction: PredictionResult) -> BetResult:
        if not prediction.approved or prediction.recommended_stake <= 0.0:
            raise InvalidBetError("Only risk-approved predictions with a positive stake can be placed.")
        if not await self._constraints.is_enabled(self._session, ARENA_SUBSYSTEM, ARENA_LIVE_BETTING_FLAG):
            raise BettingHaltedError("Live betting is halted.")
        stored = await self._oracle.get_prediction(self._session, prediction.prediction_id)
        if stored is None or not stored.approved:
            raise InvalidBetError(f"Prediction {prediction.prediction_id} not found or not approved.")
        bet = BetRecord(
            prediction_id=stored.id, event_id=stored.event_id, stake=stored.recommended_stake, decimal_odds=stored.decimal_odds
        )
        try:
            bet = await self._arena.add_bet(self._session, bet)
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            raise
        return BetResult.model_validate(bet)

    async def settle_bet(self, bet_id: int, outcome: BetOutcome) -> BetResult:
        bet = await self._arena.get_bet(self._session, bet_id)
        if bet is None:
            raise InvalidBetError(f"Bet {bet_id} not found.")
        if bet.status != BetStatus.OPEN:
            raise InvalidBetError(f"Bet {bet_id} is already settled.")
        pnl_by_outcome: dict[BetOutcome, tuple[BetStatus, float]] = {
            "won": (BetStatus.WON, bet.stake * (bet.decimal_odds - 1.0)),
            "lost": (BetStatus.LOST, -bet.stake),
            "void": (BetStatus.VOID, 0.0),
        }
        bet.status, bet.pnl = pnl_by_outcome[outcome]
        bet.settled_at = utcnow()
        try:
            await self._session.flush()
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            raise
        return BetResult.model_validate(bet)

    # ------------------------------------------------------------------ Risk
    async def trigger_stop_loss_cascade(self, limit_breached: float) -> CascadeResult:
        if not math.isfinite(limit_breached):
            raise ValueError("limit_breached must be a finite number.")
        reason = f"stop-loss breached at {limit_breached:.6f}"
        targets = ((ORACLE_SUBSYSTEM, ORACLE_AUTOMATION_FLAG), (ARENA_SUBSYSTEM, ARENA_LIVE_BETTING_FLAG))
        changes: list[ConstraintChange] = []
        try:
            for subsystem, flag in targets:  # single transaction => both constraints apply atomically
                row = await self._constraints.set_flag(self._session, subsystem=subsystem, flag=flag, enabled=False, reason=reason)
                changes.append(ConstraintChange.model_validate(row))
            await self._session.commit()
        except SQLAlchemyError:
            await self._session.rollback()
            logger.exception("Stop-loss cascade failed to persist constraints (limit=%s).", limit_breached)
            raise
        logger.warning("Stop-loss cascade triggered: %s", reason)
        return CascadeResult(triggered=True, limit_breached=limit_breached, changes=changes, triggered_at=datetime.now(UTC))
