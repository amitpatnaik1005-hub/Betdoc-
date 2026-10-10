"""The execution dispatcher (Group 71): fires a routed order's slices at their venues, and sweeps up after.

``ExecutionDispatcher.dispatch`` sends every slice of an order at once (one per venue), each under
``ROUTER_SLICE_TIMEOUT_SECONDS``, and classifies each answer four ways:

* FILLED / PARTIAL: the venue holds the bet (a partial fill matched part of the stake; the rest lapsed);
* REJECTED: the bet does not exist; its reservation is released at once;
* UNKNOWN: no trustworthy answer (a timeout, a duplicate submission, an executor crash). The stake stays
  held: the bet may be live. Never released by the dispatcher, only by reconciliation or a person.

``CfoSliceExecutor`` runs each slice through the CFO's two-phase ``TradeExecutor.execute_leg`` (risk
guards, bankroll lock, the bookmaker gateway, the ledger row the Active Portfolio reads), keyed by the
slice's UUID: the same key the Vault reservation carries, so the Sniper's own account routing finds
this slice's hold instead of taking a second one.

The sweep (``router.sweep``, every ``ROUTER_SWEEP_INTERVAL_SECONDS``): slices whose hold the Vault's
sweeper already released are marked RELEASED; UNKNOWN / DISPATCHED slices the ledger since confirmed are
filled and their orders finished; expired venue pauses are announced; receipts that missed Nalanda are
mirrored again. Orphaned reservations are left for the operator (the Control Panel's release button).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    from app.services.cfo_execution import TradeExecutor

logger = logging.getLogger("betdoc.router.dispatch")

# Refusals that say the venue (or the route to it) failed, not the user's own limits: these count
# toward the venue's circuit breaker. A kill switch or a drawdown block never pauses a bookmaker.
VENUE_FAULT_PREFIXES = (
    "BOOKMAKER_", "UNMAPPED_", "NO_EXECUTION_VENUE", "NO_VAULT_ACCOUNT", "VAULT_ACCOUNT", "CURRENCY_MISMATCH", "SLIPPAGE_VIOLATION", "TIMEOUT",
)


class SliceOutcomeKind(StrEnum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SliceTicket:
    """One slice as a venue sees it: the live price the guard approved, the order's floor, the stake."""

    client_ref: uuid.UUID  # the slice's UUID: reservation order_ref and ledger idempotency key
    idempotency_key: str  # {order_id}_{venue_id}_{slice_index}
    routed_order_id: uuid.UUID
    user_id: uuid.UUID | None
    venue_id: str
    account_id: uuid.UUID | None
    match_id: str
    market: str
    selection: str
    odds: Decimal
    min_odds: Decimal
    stake: Decimal
    currency: str
    commission: Decimal
    true_prob: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SliceOutcome:
    kind: SliceOutcomeKind
    reason: str
    filled_stake: Decimal = Decimal(0)
    matched_odds: Decimal | None = None
    remote_bet_id: str | None = None
    ledger_id: uuid.UUID | None = None
    venue_fault: bool = False  # counts toward the venue's circuit breaker


def venue_fault(reason: str) -> bool:
    return reason.startswith(VENUE_FAULT_PREFIXES)


class SliceExecutor(Protocol):
    async def execute(self, ticket: SliceTicket) -> SliceOutcome: ...


class CfoSliceExecutor:
    """A slice through the CFO's two-phase execution (paper or live, whatever the gateway is)."""

    def __init__(self, executor: TradeExecutor) -> None:
        self.executor = executor

    async def execute(self, ticket: SliceTicket) -> SliceOutcome:
        from app.services.cfo_ledger import CfoError, OrderTicket  # noqa: PLC0415 - the CFO stack is heavy

        if ticket.user_id is None:
            return SliceOutcome(SliceOutcomeKind.REJECTED, "NO_USER")
        if ticket.currency.upper() != "INR":
            return SliceOutcome(SliceOutcomeKind.REJECTED, "CURRENCY_UNSUPPORTED")  # the CFO books rupees; FX legs are not routed
        order = OrderTicket(
            user_id=ticket.user_id, idempotency_key=ticket.client_ref, fixture_id=ticket.match_id, market=ticket.market, selection=ticket.selection,
            bookmaker_id=ticket.venue_id, stake_inr=ticket.stake, odds=ticket.odds, true_prob=ticket.true_prob, strategy="routed",
            group_id=ticket.routed_order_id, commission=ticket.commission,
        )
        try:
            receipt = await self.executor.execute_leg(order, min_odds=ticket.min_odds)
        except CfoError as exc:
            if exc.reason == "DUPLICATE_REQUEST":  # submitted before (a retry): it may have filled, so it is held, not released
                return SliceOutcome(SliceOutcomeKind.UNKNOWN, "DUPLICATE_SUBMISSION")
            return SliceOutcome(SliceOutcomeKind.REJECTED, exc.reason[:64], venue_fault=bool(getattr(exc, "audited", False)) or venue_fault(exc.reason))
        if receipt.status == "UNKNOWN":
            return SliceOutcome(SliceOutcomeKind.UNKNOWN, "NO_CONFIRMATION", ledger_id=receipt.ledger_id, venue_fault=True)
        filled = Decimal(receipt.stake_inr)
        kind = SliceOutcomeKind.PARTIAL if receipt.partial_fill and filled < ticket.stake else SliceOutcomeKind.FILLED
        return SliceOutcome(kind, "PARTIAL_FILL" if kind is SliceOutcomeKind.PARTIAL else "FILLED", min(filled, ticket.stake), Decimal(receipt.odds),
                            receipt.remote_bet_id, receipt.ledger_id)


class ExecutionDispatcher:
    """Every slice of one order at once, each with its own deadline; an answer is always one of four kinds."""

    def __init__(self, executor: SliceExecutor, settings: Settings) -> None:
        self.executor = executor
        self.timeout = float(settings.ROUTER_SLICE_TIMEOUT_SECONDS)

    async def _one(self, ticket: SliceTicket) -> SliceOutcome:
        try:
            outcome = await asyncio.wait_for(self.executor.execute(ticket), self.timeout)
        except TimeoutError:
            return SliceOutcome(SliceOutcomeKind.UNKNOWN, "TIMEOUT", venue_fault=True)
        except Exception:  # noqa: BLE001 - an executor crash after the request may have left: held, never released
            logger.exception("Router: slice %s at %s raised", ticket.idempotency_key, ticket.venue_id)
            return SliceOutcome(SliceOutcomeKind.UNKNOWN, "EXECUTOR_ERROR")
        if outcome.filled_stake > ticket.stake:
            outcome = replace(outcome, filled_stake=ticket.stake)  # never more than was asked
        return outcome

    async def dispatch(self, tickets: Sequence[SliceTicket]) -> list[SliceOutcome]:
        return list(await asyncio.gather(*(self._one(t) for t in tickets)))


# ------------------------------------------------------------------------------------------ the sweep
async def sweep(session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings) -> dict[str, Any]:
    from app.services.execution.smart_router import RedisRouterEvents, sweep_router  # noqa: PLC0415 - the router imports this module

    return await sweep_router(session_factory, settings, RedisRouterEvents(redis, settings))


@asynccontextmanager
async def _resources() -> AsyncIterator[tuple[Redis, async_sessionmaker[AsyncSession], Settings]]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        yield redis, async_sessionmaker(engine, expire_on_commit=False), settings
    finally:
        await redis.aclose()
        await engine.dispose()


async def _sweep() -> dict[str, Any]:
    async with _resources() as (redis, sessions, settings):
        return await sweep(sessions, redis, settings)


@celery_app.task(name="router.sweep", ignore_result=True)
def router_sweep() -> dict[str, Any]:
    return asyncio.run(_sweep())


__all__ = ["CfoSliceExecutor", "ExecutionDispatcher", "SliceExecutor", "SliceOutcome", "SliceOutcomeKind", "SliceTicket", "sweep", "venue_fault"]
