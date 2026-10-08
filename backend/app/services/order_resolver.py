"""Order resolution: what happened to every live order after it left?

``sniper.resolve_pending_orders`` (Celery beat) asks each venue's "my bets" endpoint about every
PENDING order that is due, grouped per venue:

* Confirmed bets (we hold a ``remote_bet_id``): WON / LOST / VOID settle through the CFO ledger's
  ``settle_markets`` engine (the bookmaker's statement is what will actually be paid); a bet the
  venue cancelled is settled VOID (stake returned); OPEN is re-checked every
  ``SNIPER_OPEN_POLL_SECONDS``.
* Unconfirmed orders (the placement answer never arrived): looked up by our client reference.
  Found: the bet is confirmed and keeps its reserved stake. Refused by the venue: the stake returns.

Failures back off exponentially (``base * 2^(n-1)``, capped, with jitter). The dead-letter queue
takes an order out of rotation with status ``REQUIRES_MANUAL_INTERVENTION`` (stake still in
exposure, nothing retried) when the venue stays unreachable for ``SNIPER_RESOLVE_MAX_FAILURES``
polls, or when an order is still unresolved ``SNIPER_DLQ_AFTER_HOURS`` past kick-off (or past
placement, for a bet whose kick-off is unknown or that was never confirmed).
"""

from __future__ import annotations

import logging
import random
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.execution.factory import RemoteBet, RemoteStatus, VenueUnavailableError
from app.core.config import Settings
from app.models.cfo_vault import AuditEvent, LedgerStatus, PhantomLedger
from app.services.cfo_ledger import BookmakerGrade, CfoError, audit_row, resolve_manually, settle_markets
from app.services.sniper import SniperGateway

logger = logging.getLogger("betdoc.sniper")

BATCH = 50
PAPER_PREFIX = "paper_"  # paper fills never left the process: only graded markets settle them


def _utcnow() -> datetime:
    return datetime.now(UTC)


def backoff_seconds(attempts: int, settings: Settings, jitter: float | None = None) -> float:
    """``base * 2^(attempts-1)``, capped, +/-10% jitter so a fleet of retries never synchronises."""
    raw = settings.SNIPER_RESOLVE_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0))
    capped = min(raw, settings.SNIPER_RESOLVE_BACKOFF_MAX_SECONDS)
    spread = random.uniform(-0.1, 0.1) if jitter is None else jitter
    return max(1.0, capped * (1 + spread))


@dataclass(slots=True)
class ResolverSummary:
    polled: int = 0
    open: int = 0
    graded: int = 0
    confirmed: int = 0
    released: int = 0
    retried: int = 0
    dead_lettered: int = 0
    unreachable_venues: list[str] = field(default_factory=list)
    settlement: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Due:
    id: uuid.UUID
    user_id: uuid.UUID
    bookmaker_id: str
    remote_bet_id: str | None
    client_ref: str
    reconcile_required: bool
    resolve_attempts: int
    commence_time: datetime | None
    created_at: datetime
    fixture_id: str
    selection: str


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class OrderResolver:
    def __init__(
        self,
        gateway: SniperGateway,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Callable[[], datetime] = _utcnow,
        jitter: float | None = None,
    ) -> None:
        self.gateway = gateway
        self.session_factory = session_factory
        self.settings = settings
        self.clock = clock
        self.jitter = jitter
        self.redis = gateway.redis

    async def run(self) -> ResolverSummary:
        summary = ResolverSummary()
        now = self.clock()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(PhantomLedger)
                    .where(
                        PhantomLedger.status == LedgerStatus.PENDING,
                        (PhantomLedger.next_resolve_at.is_(None)) | (PhantomLedger.next_resolve_at <= now),
                        (PhantomLedger.remote_bet_id.is_(None)) | (~PhantomLedger.remote_bet_id.startswith(PAPER_PREFIX)),
                    )
                    .order_by(PhantomLedger.next_resolve_at.nulls_first(), PhantomLedger.created_at)
                    .limit(BATCH * 20)
                )
            ).scalars().all()
            due = [
                _Due(r.id, r.user_id, r.bookmaker_id, r.remote_bet_id, str(r.idempotency_key), r.reconcile_required, r.resolve_attempts,
                     r.commence_time, r.created_at, r.fixture_id, r.selection)
                for r in rows
            ]
        by_bookmaker: dict[str, list[_Due]] = defaultdict(list)
        for item in due:
            by_bookmaker[item.bookmaker_id].append(item)

        grades: dict[uuid.UUID, BookmakerGrade] = {}
        for bookmaker_id, items in by_bookmaker.items():
            venue = await self.gateway.venue_for(bookmaker_id)
            for start in range(0, len(items), BATCH):
                batch = items[start:start + BATCH]
                summary.polled += len(batch)
                if venue is None:
                    for item in batch:
                        await self._failed(item, "NO_EXECUTION_VENUE", summary)
                    continue
                adapter = self.gateway.adapter(venue)
                try:
                    statements = await adapter.fetch_statuses(
                        [i.remote_bet_id for i in batch if i.remote_bet_id and not i.reconcile_required],
                        [i.client_ref for i in batch if i.reconcile_required],
                    )
                except VenueUnavailableError as exc:
                    if venue.id not in summary.unreachable_venues:
                        summary.unreachable_venues.append(venue.id)
                    for item in batch:
                        await self._failed(item, f"VENUE_UNAVAILABLE:{exc}", summary)
                    continue
                by_remote = {s.remote_bet_id: s for s in statements if s.remote_bet_id}
                by_ref = {s.client_ref: s for s in statements if s.client_ref}
                for item in batch:
                    statement = by_ref.get(item.client_ref) if item.reconcile_required else by_remote.get(item.remote_bet_id or "")
                    await self._apply(item, statement, venue.id, grades, summary)

        if grades:
            settled = await settle_markets(self.session_factory, self.redis, self.settings, self.clock, bookmaker_grades=grades)
            summary.settlement = settled.as_dict()
            summary.graded = settled.won + settled.lost + settled.void
            for ledger_id in grades:  # anything still pending (e.g. its bankroll was mid-execution) is re-polled soon
                await self._schedule(ledger_id, self.settings.SNIPER_RESOLVE_BACKOFF_BASE_SECONDS)
        return summary

    # -------------------------------------------------------------- one order
    async def _apply(self, item: _Due, statement: RemoteBet | None, venue_id: str, grades: dict[uuid.UUID, BookmakerGrade], summary: ResolverSummary) -> None:
        if item.reconcile_required:
            if statement is not None and statement.status is RemoteStatus.REJECTED:
                await self._manual(item, "NOT_PLACED", None, summary, "released")
                return
            if statement is not None and statement.status is not RemoteStatus.NOT_FOUND and statement.remote_bet_id:
                await self._manual(item, "OPEN", statement.remote_bet_id, summary, "confirmed")
                return
            await self._failed(item, "NOT_FOUND_AT_VENUE", summary, deadline_from_kickoff=False)
            return

        if statement is None or statement.status is RemoteStatus.NOT_FOUND:
            await self._failed(item, "BET_NOT_FOUND_AT_VENUE", summary)
            return
        if statement.status in (RemoteStatus.WON, RemoteStatus.LOST, RemoteStatus.VOID, RemoteStatus.REJECTED):
            grade = "VOID" if statement.status in (RemoteStatus.VOID, RemoteStatus.REJECTED) else statement.status.value
            grades[item.id] = BookmakerGrade(grade, venue_id)  # type: ignore[arg-type]
            return
        # OPEN: alive and unsettled. Normal, unless it is still open long after kick-off
        if self._overdue(item, from_kickoff=True):
            await self._dead_letter(item, "PENDING_PAST_DEADLINE", summary)
            return
        summary.open += 1
        await self._update(item.id, resolve_attempts=0, last_resolve_error=None, next_resolve_at=self.clock() + timedelta(seconds=self.settings.SNIPER_OPEN_POLL_SECONDS))

    async def _failed(self, item: _Due, reason: str, summary: ResolverSummary, *, deadline_from_kickoff: bool = True) -> None:
        attempts = item.resolve_attempts + 1
        if attempts >= self.settings.SNIPER_RESOLVE_MAX_FAILURES:
            await self._dead_letter(item, f"{reason}:{attempts}_FAILURES", summary, attempts=attempts)
            return
        if self._overdue(item, from_kickoff=deadline_from_kickoff):
            await self._dead_letter(item, f"{reason}:PAST_DEADLINE", summary, attempts=attempts)
            return
        summary.retried += 1
        delay = backoff_seconds(attempts, self.settings, self.jitter)
        await self._update(item.id, resolve_attempts=attempts, last_resolve_error=reason[:255], next_resolve_at=self.clock() + timedelta(seconds=delay))

    def _overdue(self, item: _Due, *, from_kickoff: bool) -> bool:
        anchor = item.commence_time if (from_kickoff and item.commence_time is not None) else item.created_at
        return self.clock() > _aware(anchor) + timedelta(hours=self.settings.SNIPER_DLQ_AFTER_HOURS)

    async def _dead_letter(self, item: _Due, reason: str, summary: ResolverSummary, *, attempts: int | None = None) -> None:
        async with self.session_factory() as session:
            moved = await session.execute(
                update(PhantomLedger)
                .where(PhantomLedger.id == item.id, PhantomLedger.status == LedgerStatus.PENDING)
                .values(
                    status=LedgerStatus.REQUIRES_MANUAL_INTERVENTION,
                    next_resolve_at=None,
                    last_resolve_error=reason[:255],
                    resolve_attempts=attempts if attempts is not None else item.resolve_attempts,
                )
            )
            if moved.rowcount:
                session.add(
                    audit_row(
                        AuditEvent.DEAD_LETTERED,
                        reason.split(":")[0][:64],
                        user_id=item.user_id,
                        ledger_id=item.id,
                        idempotency_key=uuid.UUID(item.client_ref),
                        fixture_id=item.fixture_id,
                        selection=item.selection,
                        detail={"reason": reason, "remote_bet_id": item.remote_bet_id},
                    )
                )
            await session.commit()
        if moved.rowcount:
            summary.dead_lettered += 1
            await self.gateway.feed.emit(item.user_id, "dlq", f"Moved to manual intervention: {reason}", level="error", ref=item.client_ref)

    async def _manual(self, item: _Due, outcome: str, remote_bet_id: str | None, summary: ResolverSummary, counter: str) -> None:
        async with self.session_factory() as session:
            try:
                await resolve_manually(session, self.settings, item.id, outcome=outcome, remote_bet_id=remote_bet_id, actor=None)  # type: ignore[arg-type]
                await session.commit()
            except CfoError as exc:  # e.g. the bankroll is mid-execution: next sweep
                await session.rollback()
                await self._schedule(item.id, self.settings.SNIPER_RESOLVE_BACKOFF_BASE_SECONDS)
                logger.info("Resolver could not %s order %s yet: %s", counter, item.id, exc.reason)
                return
        setattr(summary, counter, getattr(summary, counter) + 1)
        message = f"Confirmed at the venue: remote_id {remote_bet_id}" if counter == "confirmed" else "The venue never struck it: stake returned"
        await self.gateway.feed.emit(item.user_id, "resolve", message, level="success" if counter == "confirmed" else "warning", ref=item.client_ref)

    async def _schedule(self, ledger_id: uuid.UUID, seconds: float) -> None:
        await self._update(ledger_id, next_resolve_at=self.clock() + timedelta(seconds=seconds))

    async def _update(self, ledger_id: uuid.UUID, **values: Any) -> None:
        async with self.session_factory() as session:
            await session.execute(update(PhantomLedger).where(PhantomLedger.id == ledger_id, PhantomLedger.status == LedgerStatus.PENDING).values(**values))
            await session.commit()
