"""Two-phase trade execution: reserve under a row lock, call the bookmaker, then commit or roll back.

Order (``TradeExecutor.execute``):

1. Idempotency: ``SET betdoc:idempotency:{key} NX EX 60``; a second click with the same key is
   refused as a duplicate (the ledger's unique key backs this up beyond the 60s).
   TTL: an order from an Aryabhata signal must still match a live edge at no worse a price.
2. The five risk guards (``risk_guard``), kill switch first, and the Control Panel stake limits.
   Then an EXECUTION_ATTEMPT audit row is committed: an attempt with no outcome row after it is
   exactly what reconciliation looks for if the process dies mid-call.
3. One transaction: ``SELECT ... FOR UPDATE NOWAIT`` on the bankroll row, the stateful guards
   again (now that nothing else can move this bankroll), the stake caps against locked equity.
4. Reserve: the bet is written PENDING and its stake moves AVAILABLE -> EXPOSURE.
5. The bookmaker call, with the reservation uncommitted and the row still locked.
6. ACCEPTED: commit. The bet and its reserved stake are now durable.
7. REJECTED: roll back. The reservation never existed; the stake is back in AVAILABLE.
   UNKNOWN (the request left and no trustworthy answer came back): the reservation is committed
   with ``reconcile_required`` so the stake stays in EXPOSURE. Rolling back here would hand the
   money back while the bet may be live: the one loophole a timeout must not open.

Nothing else writes while the locked transaction is open: audit rows go in before it starts or
after it ends, so they can never commit (or roll back) the reservation by sharing its connection.
The locked section runs to completion even if the HTTP request is cancelled mid-flight.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.cfo_vault import AuditEvent, AuditLog
from app.schemas.aryabhata import EdgeSignal
from app.schemas.cfo_vault import ExecuteTradeRequest, ExecutionReceipt
from app.services.aryabhata_engine import MAX_STAKE_PCT, MIN_STAKE_PCT, RiskLimits
from app.services.aryabhata_pipeline import AryabhataKeys, load_risk_limits
from app.services.bookmaker_gateway import BookmakerGateway, BookmakerOrder, BookmakerOutcome, BookmakerResult
from app.services.cfo_ledger import (
    CfoError,
    DuplicateExecutionError,
    LedgerInvariantError,
    OrderTicket,
    audit_row,
    lock_bankroll,
    read_account,
    reserve,
    write_audit,
)
from app.services.risk_guard import GuardReport, RiskGuard, RiskGuardViolation

logger = logging.getLogger("betdoc.cfo")

T = TypeVar("T")
_HUNDRED = Decimal(100)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _run_to_completion(coro: Coroutine[Any, Any, T]) -> T:
    """Await ``coro`` in its own task, shielded: cancelling the caller never abandons a held lock
    or a reservation halfway (the task finishes its commit or rollback regardless)."""
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        def _report(done: asyncio.Task[T]) -> None:
            if not done.cancelled() and done.exception() is not None:
                logger.error("Locked execution finished after its request was cancelled: %r", done.exception())

        task.add_done_callback(_report)
        raise


@dataclass(slots=True)
class _Outcome:
    """What the locked section decided, carried out of it so the audit row is written afterwards."""

    result: BookmakerResult
    receipt: ExecutionReceipt | None
    ledger_id: uuid.UUID | None
    error: CfoError | None = None


class TradeExecutor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis | None,
        settings: Settings,
        gateway: BookmakerGateway,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.gateway = gateway
        self.clock = clock
        self.guard = RiskGuard(redis, settings, clock)

    # -------------------------------------------------------------- entry point
    async def execute(self, user_id: uuid.UUID, request: ExecuteTradeRequest) -> ExecutionReceipt:
        ticket = OrderTicket(
            user_id=user_id,
            idempotency_key=request.idempotency_key,
            fixture_id=request.fixture_id,
            market=request.market,
            selection=request.selection,
            bookmaker_id=request.bookmaker_id,
            stake_inr=request.stake_inr,
            odds=request.odds,
            true_prob=request.true_prob,
            signal_id=request.signal_id,
        )
        try:
            await self._claim(ticket)
            await self._check_signal(request)
            report = await self._pre_guards(ticket)
            limits = await load_risk_limits(self.redis, self.session_factory, self.settings)
            self._check_limits(ticket, limits, equity=None)
            await self._record(self._audit(AuditEvent.EXECUTION_ATTEMPT, "GUARDS_PASSED", ticket, detail={"risk": report.as_dict()}), required=True)
            outcome = await _run_to_completion(self._locked(ticket, report, limits))
        except CfoError as exc:
            if not exc.audited:
                await self._record(self._audit(AuditEvent.BLOCKED, exc.reason, ticket, detail={"message": exc.message, **exc.detail}))
            raise
        return await self._conclude(ticket, outcome)

    # -------------------------------------------------------------- 1. idempotency + TTL
    async def _claim(self, ticket: OrderTicket) -> None:
        if self.redis is None:
            raise RiskGuardViolation("RISK_SERVICES_UNAVAILABLE", "Execution needs Redis for idempotency; nothing was executed", status_code=503)
        key = f"{self.settings.CFO_IDEMPOTENCY_KEY_PREFIX}:{ticket.idempotency_key}"
        try:
            claimed = await self.redis.set(key, str(ticket.user_id), nx=True, ex=self.settings.CFO_IDEMPOTENCY_TTL_SECONDS)
        except (RedisError, OSError) as exc:
            raise RiskGuardViolation("RISK_SERVICES_UNAVAILABLE", "Execution needs Redis for idempotency; nothing was executed", status_code=503) from exc
        if not claimed:
            raise DuplicateExecutionError("DUPLICATE_REQUEST", "This order was already submitted (duplicate click)")

    async def _check_signal(self, request: ExecuteTradeRequest) -> None:
        now = self.clock()
        if request.signal_expires_at is not None:
            expires = request.signal_expires_at if request.signal_expires_at.tzinfo else request.signal_expires_at.replace(tzinfo=UTC)
            if expires <= now:
                raise CfoError("SIGNAL_EXPIRED", "This signal's 15 seconds are up; the price may have moved", status_code=410)
        if request.signal_id is None or self.redis is None:
            return
        field = f"{request.fixture_id}|{request.selection}"
        try:
            raw = await self.redis.hget(AryabhataKeys(self.settings.ARYABHATA_PREFIX).active, field)
        except (RedisError, OSError) as exc:
            raise RiskGuardViolation("RISK_SERVICES_UNAVAILABLE", "The live edge cannot be confirmed right now", status_code=503) from exc
        try:
            edge = EdgeSignal.model_validate_json(raw) if raw else None
        except ValidationError:
            edge = None
        if edge is None or edge.expires_at <= now:
            raise CfoError("SIGNAL_EXPIRED", "This edge has closed", status_code=410)
        if edge.odds < request.odds:
            raise CfoError(
                "PRICE_MOVED",
                f"The price has moved to {edge.odds}",
                status_code=409,
                detail={"current_odds": str(edge.odds), "requested_odds": str(request.odds)},
            )

    # -------------------------------------------------------------- 2. the five guards + stake limits
    async def _pre_guards(self, ticket: OrderTicket) -> GuardReport:
        async with self.session_factory() as session:
            account = await read_account(session, ticket.user_id)
            return await self.guard.check(session, ticket, account)

    @staticmethod
    def _check_limits(ticket: OrderTicket, limits: RiskLimits, equity: Decimal | None) -> None:
        """Group 61's limits bind executions too: the bankroll % cap and execution's absolute max bet."""
        if limits.halted:
            raise RiskGuardViolation("BLOCKED_BY_KILL_SWITCH", "Trading is halted by the emergency stop")
        if limits.max_bet_size is not None and ticket.stake_inr > limits.max_bet_size:
            raise RiskGuardViolation(
                "STAKE_ABOVE_MAX_BET",
                f"Stake exceeds the Control Panel max bet ({limits.max_bet_size})",
                status_code=409,
                detail={"max_bet_size": str(limits.max_bet_size)},
            )
        if equity is None:
            return
        pct = max(MIN_STAKE_PCT, min(MAX_STAKE_PCT, limits.max_stake_pct))
        cap = (equity * pct / _HUNDRED).quantize(Decimal("0.01"))
        if ticket.stake_inr > cap:
            raise RiskGuardViolation("STAKE_ABOVE_CAP", f"Stake exceeds the {pct}% bankroll cap ({cap})", status_code=409, detail={"cap": str(cap)})

    # -------------------------------------------------------------- 3-7. the locked section
    async def _locked(self, ticket: OrderTicket, report: GuardReport, limits: RiskLimits) -> _Outcome:
        async with self.session_factory() as session:
            try:
                account = await lock_bankroll(session, ticket.user_id, self.settings, nowait=True)
                await self.guard.recheck_locked(session, ticket, account, report)
                self._check_limits(ticket, limits, equity=account.equity)
                entry = await reserve(session, account, ticket)
                ledger_id, potential = entry.id, entry.potential_pnl
                available, exposure = account.available_balance, account.exposure_balance

                result = await self.gateway.place(
                    BookmakerOrder(
                        client_ref=str(ticket.idempotency_key),
                        bookmaker_id=ticket.bookmaker_id,
                        fixture_id=ticket.fixture_id,
                        market=ticket.market,
                        selection=ticket.selection,
                        odds=ticket.odds,
                        stake_inr=ticket.stake_inr,
                    )
                )

                if result.outcome is BookmakerOutcome.REJECTED:
                    await session.rollback()  # the reservation never happened: the stake is back in AVAILABLE
                    error = CfoError(result.reason, "The bookmaker refused the order. Nothing was placed and your funds were not moved.", status_code=502)
                    return _Outcome(result, None, ledger_id, error)

                if result.outcome is BookmakerOutcome.ACCEPTED:
                    entry.bookmaker_ref = result.reference
                    status, message = "EXECUTED", "Placed. The stake is now in exposure until the market settles."
                else:
                    entry.reconcile_required = True
                    status, message = (
                        "UNKNOWN",
                        "The bookmaker did not confirm in time. The bet may be live, so its stake stays in exposure until it is reconciled.",
                    )
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.critical("Ledger commit failed after the bookmaker answered %s (ref %s)", result.outcome, result.reference)
                    error = LedgerInvariantError("LEDGER_COMMIT_FAILED", "The bet may be placed but the ledger could not record it; it needs manual reconciliation")
                    return _Outcome(result, None, ledger_id, error)

                receipt = ExecutionReceipt(
                    status=status,  # type: ignore[arg-type]
                    message=message,
                    ledger_id=ledger_id,
                    bookmaker_ref=result.reference,
                    bookmaker_id=ticket.bookmaker_id,
                    fixture_id=ticket.fixture_id,
                    selection=ticket.selection,
                    stake_inr=ticket.stake_inr,
                    odds=ticket.odds,
                    potential_pnl=potential,
                    available_balance=available,
                    exposure_balance=exposure,
                    execution_mode=self.settings.CFO_EXECUTION_MODE,
                )
                return _Outcome(result, receipt, ledger_id)
            except BaseException:
                if session.in_transaction():
                    await session.rollback()
                raise

    async def _conclude(self, ticket: OrderTicket, outcome: _Outcome) -> ExecutionReceipt:
        """The locked transaction is over: record what happened, then answer."""
        result, detail = outcome.result, {"bookmaker_ref": outcome.result.reference, "http_status": outcome.result.http_status}
        if outcome.error is not None:
            event = AuditEvent.COMMIT_FAILED if outcome.error.reason == "LEDGER_COMMIT_FAILED" else AuditEvent.BOOKMAKER_REJECTED
            await self._record(self._audit(event, outcome.error.reason, ticket, ledger_id=outcome.ledger_id, detail=detail))
            outcome.error.audited = True
            raise outcome.error
        assert outcome.receipt is not None
        event = AuditEvent.EXECUTED if result.outcome is BookmakerOutcome.ACCEPTED else AuditEvent.EXECUTION_UNKNOWN
        await self._record(self._audit(event, result.reason, ticket, ledger_id=outcome.ledger_id, detail=detail))
        return outcome.receipt

    # -------------------------------------------------------------- helpers
    async def _record(self, row: AuditLog, *, required: bool = False) -> None:
        try:
            await write_audit(self.session_factory, row, required=required)
        except Exception as exc:  # only reachable when required
            raise CfoError("AUDIT_UNAVAILABLE", "The audit trail cannot be written; nothing was placed", status_code=503) from exc

    @staticmethod
    def _audit(event: AuditEvent, reason: str, ticket: OrderTicket, **fields: Any) -> AuditLog:
        return audit_row(
            event,
            reason,
            user_id=ticket.user_id,
            idempotency_key=ticket.idempotency_key,
            fixture_id=ticket.fixture_id,
            selection=ticket.selection,
            stake_inr=ticket.stake_inr,
            odds=ticket.odds,
            **fields,
        )
