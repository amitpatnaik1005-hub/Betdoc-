"""The Smart Order Router and multi-venue execution slicer (Group 71).

``SmartOrderRouter.route(order)`` takes an ``ExecutionOrder`` (Ashoka's vetted slip, a betslip, a bot)
and carries it across the Vault's bookmaker accounts:

1. **Claim** the parent ``order_id`` (unique): a retried order is the same order, never a second one.
2. **Discover and lock.** The active Vault accounts of the target bookmakers that settle in the order's
   currency are locked in one deterministic order, ``SELECT ... WHERE id IN (...) ORDER BY id ASC FOR
   UPDATE``: two orders that need overlapping accounts always queue on the same first row, so they can
   never hold one row each while waiting for the other (no PostgreSQL deadlock, by construction).
   Free funds are read from the locked rows: ``balance - reserved``.
3. **Plan.** One account per bookmaker (never two accounts of one book: their limits are the book's to
   set); a venue whose live price fails the slippage guard, or whose breaker is open, is left out. If
   the best-priced venue can carry the whole stake it gets one slice; otherwise the stake is sliced
   across the venues in proportion to what each can carry (free funds, the user's stake cap, the
   venue's own maximum), every slice at least the venue's minimum, whole rupees, the remainder to the
   best price. Not enough across the whole fleet: refused with the deficit
   (``InsufficientFleetBalance: required ₹50,000, available ₹32,000``), nothing held.
4. **Reserve** every slice's stake on its account (a guarded ``UPDATE``: ``balance - reserved >= stake``)
   under the slice's deterministic key ``{order_id}_{venue_id}_{slice_index}`` (its UUID is the Vault
   reservation's ``order_ref`` and the ledger's idempotency key): a retry can never hold twice.
5. **Guard.** The quote stream is read again; any slice below the floor, past the slippage tolerance,
   stale, or short of the order net of its venue's commission aborts the whole order and releases
   every hold. A venue whose breaker tripped meanwhile has its slice released.
6. **Dispatch** every slice at once (``app.workers.execution_dispatcher``). A rejected slice's hold is
   released at once; a partial fill keeps only the matched stake; an unanswered slice keeps it all.
   Two consecutive venue failures within 60 s trip the venue's breaker (paused 5 min, its other
   reserved slices released, the Sentinel paged CRITICAL).
7. **Finish.** FILLED, PARTIAL (legged: handed to the Active Portfolio as hedge-eligible, the Sentinel
   warned), REJECTED or UNCONFIRMED. A filled or partly filled order gets a SHA-256 receipt over the
   parent and every child slice with its bookmaker confirmation, mirrored into Nalanda's hash chain.

Every state change is a compare-and-set on the row's status, and every multi-account release runs in
account-id order: the same lock order as step 2.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.execution.venue import CircuitPolicy, VenueStakeRules
from app.core.config import Settings
from app.models.execution_router import (
    OPEN_SLICE_STATUSES,
    RoutedOrder,
    RoutedOrderStatus,
    RoutedSlice,
    SliceStatus,
)
from app.models.omni_vault import VaultAccountReservation, VaultBookmakerAccount, VerificationStatus
from app.services.execution import circuit_breaker
from app.services.execution.slippage_guard import GuardReport, LiveQuote, QuoteSource, SlippageGuard, SliceCheck, book_key
from app.services.vault import account_rotator
from app.services.venue_costs import BookmakerTerms
from app.workers.execution_dispatcher import ExecutionDispatcher, SliceOutcome, SliceOutcomeKind, SliceTicket

logger = logging.getLogger("betdoc.router")

ZERO = Decimal(0)
ODDS_QUANTUM = Decimal("0.0001")
ROUTER_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://betdoc.local/execution/smart-order-router")
RESERVE_ATTEMPTS = 4
HEDGE_ELIGIBLE = "ELIGIBLE"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def slice_key(order_id: str, venue_id: str, index: int) -> str:
    """The slice's idempotency key: ``{parent_order_id}_{venue_id}_{slice_index}``."""
    return f"{order_id}_{venue_id}_{index}"


def slice_ref(key: str) -> uuid.UUID:
    """The slice's UUID, derived from its key: the Vault reservation's ``order_ref`` and the ledger's key."""
    return uuid.uuid5(ROUTER_NAMESPACE, key)


def money_text(amount: Decimal, currency: str) -> str:
    whole = amount.quantize(Decimal(1)) if amount == amount.to_integral_value() else amount.quantize(Decimal("0.01"))
    return f"₹{whole:,}" if currency.upper() == "INR" else f"{whole:,} {currency.upper()}"


# ------------------------------------------------------------------------------------------ the order
class ExecutionOrder(BaseModel):
    """One order to route: a stake at a price on a selection, across the bookmakers it may use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    match_id: str = Field(min_length=1, max_length=128)
    market: str = Field(default="h2h", min_length=1, max_length=64)
    selection: str = Field(min_length=1, max_length=128)
    odds: Decimal = Field(gt=1, max_digits=10, decimal_places=4)
    desired_total_stake: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    max_slippage_pct: Decimal = Field(default=Decimal("2"), ge=0, le=50, max_digits=6, decimal_places=3)
    min_acceptable_odds: Decimal = Field(gt=1, max_digits=10, decimal_places=4)
    target_bookmakers: tuple[str, ...] = Field(min_length=1, max_length=16)
    currency: str = Field(default="INR", min_length=3, max_length=5)
    true_prob: Decimal | None = Field(default=None, gt=0, lt=1, max_digits=8, decimal_places=6)

    @field_validator("target_bookmakers")
    @classmethod
    def _books(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        books = tuple(dict.fromkeys(book_key(b) for b in value if b and b.strip()))
        if not books:
            raise ValueError("name at least one bookmaker")
        if any(len(b) > 64 for b in books):
            raise ValueError("a bookmaker id is at most 64 characters")
        return books

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _floor(self) -> ExecutionOrder:
        if self.min_acceptable_odds > self.odds:
            raise ValueError("min_acceptable_odds cannot be above the odds asked")
        return self


# ------------------------------------------------------------------------------------------ refusals
class RouterRefusal(Exception):
    """The order was refused before anything was held. ``view``: the persisted (REJECTED) order."""

    reason = "ROUTER_REFUSED"
    status_code = 422

    def __init__(self, message: str, *, view: dict[str, Any] | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.view = view
        self.detail = detail or {}


class InsufficientFleetBalance(RouterRefusal):
    reason = "INSUFFICIENT_FLEET_BALANCE"
    status_code = 409


class NoExecutableVenue(RouterRefusal):
    reason = "NO_EXECUTABLE_VENUE"
    status_code = 422


class OrderInFlight(RouterRefusal):
    reason = "ORDER_IN_FLIGHT"
    status_code = 409


# ------------------------------------------------------------------------------------------ planning
@dataclass(frozen=True, slots=True)
class VenueCapacity:
    """What one venue can carry for this order: its chosen account, and the price it shows."""

    venue_id: str
    account_id: uuid.UUID
    account_label: str
    priority: int
    capacity: Decimal  # min(free funds, the user's stake cap, the venue's maximum)
    min_stake: Decimal
    odds: Decimal
    net_odds: Decimal
    commission: Decimal
    net_ev: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PlannedSlice:
    index: int
    venue: VenueCapacity
    stake: Decimal


@dataclass(frozen=True, slots=True)
class Deficit:
    required: Decimal
    available: Decimal


def plan_slices(desired: Decimal, venues: Sequence[VenueCapacity], quantum: Decimal) -> list[PlannedSlice] | Deficit:
    """Split ``desired`` across the venues (pure: no I/O). One slice on the best price when it can carry
    it all; otherwise proportional to capacity, each slice >= its venue's minimum and <= its capacity."""
    usable = [v for v in venues if v.capacity >= v.min_stake and v.capacity > 0]
    by_price = sorted(usable, key=lambda v: (-v.net_odds, -v.capacity, v.priority, v.venue_id))
    available = sum((v.capacity for v in usable), ZERO)
    if available < desired:
        return Deficit(desired, available)
    if by_price and by_price[0].capacity >= desired and desired >= by_price[0].min_stake:
        return [PlannedSlice(0, by_price[0], desired)]
    active = list(usable)
    while active:
        total = sum((v.capacity for v in active), ZERO)
        if total < desired:
            return Deficit(desired, available)
        shares = {v.venue_id: (desired * v.capacity / total).quantize(quantum, rounding=ROUND_DOWN) for v in active}
        short = [v for v in active if shares[v.venue_id] < v.min_stake]
        if short:  # too small a share for this venue's minimum: drop the smallest such venue and re-split
            active.remove(min(short, key=lambda v: (v.capacity, v.venue_id)))
            continue
        left = desired - sum(shares.values(), ZERO)
        for v in sorted(active, key=lambda v: (-v.net_odds, -v.capacity, v.venue_id)):  # the remainder to the best price
            if left <= 0:
                break
            room = v.capacity - shares[v.venue_id]
            give = min(room, left)
            if give > 0:
                shares[v.venue_id] += give
                left -= give
        if left > 0:
            return Deficit(desired, available)
        ordered = sorted(active, key=lambda v: v.venue_id)
        return [PlannedSlice(i, v, shares[v.venue_id]) for i, v in enumerate(ordered) if shares[v.venue_id] > 0]
    return Deficit(desired, available)


def free_funds(row: VaultBookmakerAccount) -> Decimal:
    return max(ZERO, Decimal(row.balance or 0) - Decimal(row.reserved or 0))


def account_capacity(row: VaultBookmakerAccount, rules: VenueStakeRules) -> Decimal:
    limits = [free_funds(row)] + ([Decimal(row.stake_cap)] if row.stake_cap is not None else [])
    return rules.ceiling(min(limits))


# ------------------------------------------------------------------------------------------ events
class RouterEvents(Protocol):
    async def legged(self, user_id: uuid.UUID | None, position: dict[str, Any]) -> None: ...
    async def alert(self, alert: Any) -> None: ...


class RedisRouterEvents:
    """The Active Portfolio's legged-position hand-off and the Sentinel's alert stream."""

    def __init__(self, redis: Redis | None, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def legged(self, user_id: uuid.UUID | None, position: dict[str, Any]) -> None:
        if user_id is None:
            return
        from app.services.portfolio_positions import flag_legged_position  # noqa: PLC0415

        await flag_legged_position(self.redis, self.settings, user_id, position)

    async def alert(self, alert: Any) -> None:
        from app.services.sentinel_bus import emit_alert  # noqa: PLC0415

        await emit_alert(self.redis, self.settings, alert)


def _alert(kind: str, severity: str, title: str, body: str, dedupe: str, detail: dict[str, Any], *, resolves: bool = False) -> Any:
    from app.models.sentinel import Severity  # noqa: PLC0415
    from app.services.sentinel_bus import AlertKind, SentinelAlert  # noqa: PLC0415

    return SentinelAlert(kind=AlertKind(kind), severity=Severity(severity), title=title[:200], body=body[:4000], source="smart_router",
                         dedupe_key=dedupe[:200], resolves=resolves, detail=detail)


# ------------------------------------------------------------------------------------------ views
def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def slice_view(row: RoutedSlice, *, now: datetime | None = None, orphan_after: float | None = None) -> dict[str, Any]:
    reserved_at = _aware(row.reserved_at)
    orphaned = (
        row.status == SliceStatus.RESERVED.value and now is not None and orphan_after is not None and reserved_at is not None
        and (now - reserved_at).total_seconds() > orphan_after
    )
    return {
        "id": str(row.id), "slice_index": row.slice_index, "idempotency_key": row.idempotency_key, "client_ref": str(row.client_ref),
        "venue_id": row.venue_id, "account_id": None if row.account_id is None else str(row.account_id), "stake": _s(row.stake), "currency": row.currency,
        "quoted_odds": _s(row.quoted_odds), "guard_odds": _s(row.guard_odds), "commission": _s(row.commission), "net_ev": _s(row.net_ev),
        "status": row.status, "filled_stake": _s(row.filled_stake), "matched_odds": _s(row.matched_odds), "remote_bet_id": row.remote_bet_id,
        "ledger_id": None if row.ledger_id is None else str(row.ledger_id), "reason": row.reason, "orphaned": orphaned,
        "reserved_at": reserved_at.isoformat() if reserved_at else None,
        "dispatched_at": row.dispatched_at.isoformat() if row.dispatched_at else None,
        "settled_at": row.settled_at.isoformat() if row.settled_at else None, "released_at": row.released_at.isoformat() if row.released_at else None,
    }


def order_view(row: RoutedOrder, slices: Sequence[RoutedSlice], *, now: datetime | None = None, orphan_after: float | None = None) -> dict[str, Any]:
    return {
        "id": str(row.id), "order_id": row.order_id, "user_id": None if row.user_id is None else str(row.user_id), "match_id": row.match_id,
        "market": row.market, "selection": row.selection, "odds": _s(row.odds), "min_acceptable_odds": _s(row.min_acceptable_odds),
        "max_slippage_pct": _s(row.max_slippage_pct), "true_prob": _s(row.true_prob), "desired_total_stake": _s(row.desired_total_stake),
        "currency": row.currency, "target_bookmakers": list(row.target_bookmakers or []), "status": row.status, "filled_stake": _s(row.filled_stake),
        "blended_odds": _s(row.blended_odds), "reason": row.reason, "detail": row.detail or {}, "hedge_state": row.hedge_state,
        "receipt_sha256": row.receipt_sha256, "nalanda_seq": row.nalanda_seq, "nalanda_hash": row.nalanda_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None, "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "slices": [slice_view(s, now=now, orphan_after=orphan_after) for s in sorted(slices, key=lambda s: s.slice_index)],
    }


def receipt_payload(row: RoutedOrder, slices: Sequence[RoutedSlice]) -> dict[str, Any]:
    """What the receipt proves: the parent, every child slice and the bookmaker's confirmation of each."""
    return {
        "order_id": row.order_id, "routed_order_id": str(row.id), "user_id": None if row.user_id is None else str(row.user_id),
        "match_id": row.match_id, "market": row.market, "selection": row.selection, "currency": row.currency,
        "requested_stake": str(row.desired_total_stake), "filled_stake": str(row.filled_stake), "blended_odds": _s(row.blended_odds),
        "status": row.status, "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "slices": [
            {"idempotency_key": s.idempotency_key, "client_ref": str(s.client_ref), "venue_id": s.venue_id,
             "account_id": None if s.account_id is None else str(s.account_id), "stake": str(s.stake), "filled_stake": str(s.filled_stake),
             "matched_odds": _s(s.matched_odds), "remote_bet_id": s.remote_bet_id, "ledger_id": None if s.ledger_id is None else str(s.ledger_id),
             "status": s.status, "reason": s.reason}
            for s in sorted(slices, key=lambda s: s.slice_index)
        ],
    }


def receipt_digest(payload: dict[str, Any]) -> str:
    from app.services.nalanda_chain import canonical_json  # noqa: PLC0415

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------ helpers
class _ReserveConflict(Exception):
    """A guarded reservation lost a race (only possible where FOR UPDATE is not enforced, e.g. SQLite)."""


def _retryable(exc: DBAPIError) -> bool:
    text = str(getattr(exc, "orig", exc)).lower()
    return any(m in text for m in ("database is locked", "deadlock", "could not serialize", "lock not available"))


async def _load(session: AsyncSession, order_id: str) -> tuple[RoutedOrder | None, list[RoutedSlice]]:
    row = (await session.execute(select(RoutedOrder).where(RoutedOrder.order_id == order_id).execution_options(populate_existing=True))).scalars().first()
    if row is None:
        return None, []
    slices = list((await session.execute(
        select(RoutedSlice).where(RoutedSlice.routed_order_id == row.id).order_by(RoutedSlice.slice_index).execution_options(populate_existing=True)
    )).scalars())
    return row, slices


async def _cas_order(session: AsyncSession, row_id: uuid.UUID, expect: Sequence[RoutedOrderStatus], **values: Any) -> bool:
    result = await session.execute(
        update(RoutedOrder).where(RoutedOrder.id == row_id, RoutedOrder.status.in_([s.value for s in expect])).values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _cas_slice(session: AsyncSession, slice_id: uuid.UUID, expect: Sequence[SliceStatus], **values: Any) -> bool:
    result = await session.execute(
        update(RoutedSlice).where(RoutedSlice.id == slice_id, RoutedSlice.status.in_([s.value for s in expect])).values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1  # type: ignore[attr-defined]


def _by_account(slices: Sequence[RoutedSlice]) -> list[RoutedSlice]:
    """The lock order every multi-account write uses (the same as the reservation's ``ORDER BY id``)."""
    return sorted(slices, key=lambda s: (str(s.account_id) if s.account_id is not None else "", s.slice_index))


async def release_slices(session: AsyncSession, slices: Sequence[RoutedSlice], reason: str, *, now: datetime,
                         expect: Sequence[SliceStatus] = (SliceStatus.RESERVED,)) -> list[RoutedSlice]:
    """RESERVED -> RELEASED and the Vault hold given back, account by account in id order (the caller commits)."""
    done: list[RoutedSlice] = []
    for row in _by_account(slices):
        if await _cas_slice(session, row.id, expect, status=SliceStatus.RELEASED.value, released_at=now, reason=reason[:64]):
            await account_rotator.release(session, str(row.client_ref), reason[:32], now=now)
            done.append(row)
    return done


# ------------------------------------------------------------------------------------------ the router
@dataclass(slots=True)
class _Planned:
    slices: list[RoutedSlice] = field(default_factory=list)
    refusal: RouterRefusal | None = None


class SmartOrderRouter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        quotes: QuoteSource,
        dispatcher: ExecutionDispatcher,
        events: RouterEvents,
        terms: Callable[[str], BookmakerTerms] | None = None,
        clock: Callable[[], datetime] = _utcnow,
        lock_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.sessions = session_factory
        self.lock_hook = lock_hook  # tests: held inside the account lock, to make two orders contend for it
        self.settings = settings
        self.guard = SlippageGuard(quotes, settings, terms)
        self.dispatcher = dispatcher
        self.events = events
        self.clock = clock
        self.policy = CircuitPolicy.from_settings(settings)

    # -------------------------------------------------------------- entry point
    async def route(self, order: ExecutionOrder, *, user_id: uuid.UUID | None = None) -> dict[str, Any]:
        """Route one order end to end. Returns the order's view; a refusal raises (with the view attached)."""
        claimed = await self._claim(order, user_id)
        if not claimed:
            return await self._existing(order.order_id)
        quotes = await self.guard.read(order, order.target_bookmakers)
        planned = await self._reserve(order, quotes)
        if planned.refusal is not None:
            raise planned.refusal
        report = await self.guard.verify(order, sorted({s.venue_id for s in planned.slices}), now=self.clock())
        if not report.ok:
            await self._abort(order, planned.slices, report)
            return await self.view(order.order_id)
        await self._dispatch(order, planned.slices, report, user_id)
        return await self.view(order.order_id)

    async def _claim(self, order: ExecutionOrder, user_id: uuid.UUID | None) -> bool:
        async with self.sessions() as session:
            session.add(RoutedOrder(
                id=uuid.uuid4(), order_id=order.order_id, user_id=user_id, match_id=order.match_id, market=order.market, selection=order.selection,
                odds=order.odds, min_acceptable_odds=order.min_acceptable_odds, max_slippage_pct=order.max_slippage_pct, true_prob=order.true_prob,
                desired_total_stake=order.desired_total_stake, currency=order.currency, target_bookmakers=list(order.target_bookmakers),
                status=RoutedOrderStatus.ROUTING.value, filled_stake=ZERO, detail={}, created_at=self.clock(), updated_at=self.clock(),
            ))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        return True

    async def _existing(self, order_id: str) -> dict[str, Any]:
        """A retried ``order_id``: its current state, never a second reservation."""
        view = await self.view(order_id)
        if view["status"] == RoutedOrderStatus.REJECTED.value and view["reason"] == InsufficientFleetBalance.reason:
            raise InsufficientFleetBalance(view["detail"].get("message", "InsufficientFleetBalance"), view=view, detail=view["detail"])
        return view

    # -------------------------------------------------------------- discovery, planning, reservation
    async def _lock_accounts(self, session: AsyncSession, order: ExecutionOrder) -> list[VaultBookmakerAccount]:
        """The candidate accounts, locked in primary-key order: the deadlock-free lock."""
        ids = list((await session.execute(
            select(VaultBookmakerAccount.id).where(
                VaultBookmakerAccount.bookmaker_id.in_(order.target_bookmakers), VaultBookmakerAccount.is_active.is_(True),
                VaultBookmakerAccount.currency == order.currency, VaultBookmakerAccount.balance.is_not(None),
                VaultBookmakerAccount.verification_status != VerificationStatus.FAILED.value,
            )
        )).scalars())
        if not ids:
            return []
        # SELECT * FROM vault_bookmaker_accounts WHERE id IN (...) ORDER BY id ASC FOR UPDATE
        rows = (await session.execute(
            select(VaultBookmakerAccount).where(VaultBookmakerAccount.id.in_(ids)).order_by(VaultBookmakerAccount.id.asc()).with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all()
        return [r for r in rows if r.is_active and r.balance is not None and r.currency.upper() == order.currency]  # re-read after the wait

    def _capacities(self, order: ExecutionOrder, rows: Sequence[VaultBookmakerAccount], checks: dict[str, SliceCheck],
                    paused: dict[str, datetime]) -> tuple[list[VenueCapacity], dict[str, dict[str, Any]]]:
        """One account per venue (the most it can carry, then priority), and why every other venue is out."""
        excluded: dict[str, dict[str, Any]] = {}
        venues: list[VenueCapacity] = []
        by_book: dict[str, list[VaultBookmakerAccount]] = {}
        for row in rows:
            by_book.setdefault(row.bookmaker_id, []).append(row)
        for book in order.target_bookmakers:
            accounts = by_book.get(book, [])
            rules = VenueStakeRules.for_venue(book, self.settings)
            # one account per book: the one that can carry the most (the user's priority breaks ties)
            best = sorted(accounts, key=lambda r: (-account_capacity(r, rules), int(r.priority or 100), str(r.id)))
            capacity = account_capacity(best[0], rules) if best else ZERO
            check = checks.get(book)
            if not accounts:
                excluded[book] = {"reason": "NO_ACCOUNT", "capacity": "0"}
            elif book in paused:
                excluded[book] = {"reason": "VENUE_PAUSED", "capacity": str(capacity), "paused_until": paused[book].isoformat()}
            elif check is None or not check.ok:
                excluded[book] = {"reason": check.reason if check else "QUOTE_UNAVAILABLE", "capacity": str(capacity)}
            elif capacity < rules.min_stake:
                excluded[book] = {"reason": "BELOW_MIN_STAKE", "capacity": str(capacity), "min_stake": str(rules.min_stake)}
            else:
                assert check.live_odds is not None and check.net_odds is not None
                venues.append(VenueCapacity(book, best[0].id, best[0].label, int(best[0].priority or 100), capacity, rules.min_stake,
                                            check.live_odds, check.net_odds, check.commission, check.net_ev))
        return venues, excluded

    async def _reserve(self, order: ExecutionOrder, quotes: dict[str, LiveQuote]) -> _Planned:
        for attempt in range(RESERVE_ATTEMPTS):
            try:
                return await self._reserve_once(order, quotes)
            except _ReserveConflict:
                pass
            except DBAPIError as exc:
                if not _retryable(exc):
                    raise
            await asyncio.sleep(0.02 * (attempt + 1) + random.random() * 0.03)  # noqa: S311 - jitter, not security
        return _Planned(refusal=await self._refuse(order, OrderInFlight, "the accounts stayed contended: nothing was held, try again", {}))

    async def _reserve_once(self, order: ExecutionOrder, quotes: dict[str, LiveQuote]) -> _Planned:
        now = self.clock()
        async with self.sessions() as session, session.begin():
            row, _ = await _load(session, order.order_id)
            assert row is not None
            accounts = await self._lock_accounts(session, order)
            if self.lock_hook is not None:
                await self.lock_hook()
            paused = await circuit_breaker.paused_venues(session, order.target_bookmakers, now=now)
            checks = {c.venue_id: c for c in self.guard.judge(order, order.target_bookmakers, quotes, now=now).checks}
            venues, excluded = self._capacities(order, accounts, checks, paused)
            plan = plan_slices(order.desired_total_stake, venues, Decimal(self.settings.ROUTER_STAKE_QUANTUM))
            if isinstance(plan, Deficit):
                report = {
                    "required": str(plan.required), "available": str(plan.available), "currency": order.currency,
                    "message": f"InsufficientFleetBalance: required {money_text(plan.required, order.currency)}, available {money_text(plan.available, order.currency)}",
                    "venues": {v.venue_id: {"capacity": str(v.capacity), "odds": str(v.odds), "account": v.account_label} for v in venues},
                    "excluded": excluded,
                }
                priced_out = [b for b, e in excluded.items() if e["reason"] not in ("NO_ACCOUNT", "BELOW_MIN_STAKE")]
                if not venues and priced_out:  # funds exist, but no venue may take the order right now
                    why = ", ".join(f"{b} {excluded[b]['reason']}" for b in priced_out)
                    report["message"] = f"NoExecutableVenue: no target venue can take the order now ({why})"
                    cls: type[RouterRefusal] = NoExecutableVenue
                else:
                    cls = InsufficientFleetBalance
                await _cas_order(session, row.id, (RoutedOrderStatus.ROUTING,), status=RoutedOrderStatus.REJECTED.value, reason=cls.reason,
                                 detail=report, completed_at=now, updated_at=now)
                view = order_view(row, [])
                view.update(status=RoutedOrderStatus.REJECTED.value, reason=cls.reason, detail=report)
                return _Planned(refusal=cls(report["message"], view=view, detail=report))
            made: list[RoutedSlice] = []
            for planned in plan:
                v = planned.venue
                key = slice_key(order.order_id, v.venue_id, planned.index)
                ref = slice_ref(key)
                stake = planned.stake
                guarded = await session.execute(
                    update(VaultBookmakerAccount)
                    .where(VaultBookmakerAccount.id == v.account_id, VaultBookmakerAccount.is_active.is_(True),
                           VaultBookmakerAccount.balance - VaultBookmakerAccount.reserved >= stake,
                           or_(VaultBookmakerAccount.stake_cap.is_(None), VaultBookmakerAccount.stake_cap >= stake))
                    .values(reserved=VaultBookmakerAccount.reserved + stake, last_used_at=now)
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount != 1:  # type: ignore[attr-defined]
                    raise _ReserveConflict(v.venue_id)  # rolls the whole plan back
                session.add(VaultAccountReservation(account_id=v.account_id, order_ref=str(ref), amount=stake, currency=order.currency, created_at=now))
                piece = RoutedSlice(
                    id=uuid.uuid4(), routed_order_id=row.id, slice_index=planned.index, idempotency_key=key, client_ref=ref, venue_id=v.venue_id,
                    account_id=v.account_id, stake=stake, currency=order.currency, quoted_odds=v.odds, commission=v.commission, net_ev=v.net_ev,
                    status=SliceStatus.RESERVED.value, filled_stake=ZERO, reserved_at=now,
                )
                session.add(piece)
                made.append(piece)
            plan_detail = {"plan": [{"venue_id": p.venue.venue_id, "stake": str(p.stake), "capacity": str(p.venue.capacity), "odds": str(p.venue.odds)} for p in plan],
                           "excluded": excluded}
            if not await _cas_order(session, row.id, (RoutedOrderStatus.ROUTING,), status=RoutedOrderStatus.RESERVED.value, detail=plan_detail, updated_at=now):
                raise _ReserveConflict("order")
            await session.flush()
        return _Planned(slices=made)

    async def _refuse(self, order: ExecutionOrder, cls: type[RouterRefusal], message: str, detail: dict[str, Any]) -> RouterRefusal:
        now = self.clock()
        async with self.sessions() as session, session.begin():
            row, _ = await _load(session, order.order_id)
            assert row is not None
            await _cas_order(session, row.id, (RoutedOrderStatus.ROUTING,), status=RoutedOrderStatus.REJECTED.value, reason=cls.reason,
                             detail={**detail, "message": message}, completed_at=now, updated_at=now)
        view = await self.view(order.order_id)
        return cls(message, view=view, detail=view["detail"])

    # -------------------------------------------------------------- the guard said no
    async def _abort(self, order: ExecutionOrder, slices: Sequence[RoutedSlice], report: GuardReport) -> None:
        now = self.clock()
        failures = [c.as_dict() for c in report.failures]
        async with self.sessions() as session, session.begin():
            row, current = await _load(session, order.order_id)
            assert row is not None
            await release_slices(session, current, "SLIPPAGE_GUARD", now=now)
            detail = {**(row.detail or {}), "guard": report.as_dict(), "message": "aborted before dispatch: " + ", ".join(f"{f['venue_id']} {f['reason']}" for f in failures)}
            await _cas_order(session, row.id, (RoutedOrderStatus.RESERVED,), status=RoutedOrderStatus.ABORTED.value, reason="SLIPPAGE_GUARD",
                             detail=detail, completed_at=now, updated_at=now)
        logger.warning("Router: order %s aborted by the slippage guard (%s); every hold released", order.order_id, failures)

    # -------------------------------------------------------------- dispatch
    async def _dispatch(self, order: ExecutionOrder, slices: Sequence[RoutedSlice], report: GuardReport, user_id: uuid.UUID | None) -> None:
        now = self.clock()
        live = {c.venue_id: c for c in report.checks}
        tickets: list[tuple[RoutedSlice, SliceTicket]] = []
        async with self.sessions() as session, session.begin():
            row, current = await _load(session, order.order_id)
            assert row is not None
            paused = await circuit_breaker.paused_venues(session, [s.venue_id for s in current], now=now)
            await release_slices(session, [s for s in current if s.venue_id in paused], "VENUE_PAUSED", now=now)
            for piece in current:
                if piece.venue_id in paused:
                    continue
                check = live[piece.venue_id]
                if not await _cas_slice(session, piece.id, (SliceStatus.RESERVED,), status=SliceStatus.DISPATCHED.value, dispatched_at=now,
                                        guard_odds=check.live_odds):
                    continue  # released meanwhile (a breaker trip, an operator): never sent
                tickets.append((piece, SliceTicket(
                    client_ref=piece.client_ref, idempotency_key=piece.idempotency_key, routed_order_id=row.id, user_id=user_id, venue_id=piece.venue_id,
                    account_id=piece.account_id, match_id=order.match_id, market=order.market, selection=order.selection,
                    odds=check.live_odds or order.odds, min_odds=order.min_acceptable_odds, stake=Decimal(piece.stake), currency=order.currency,
                    commission=Decimal(piece.commission), true_prob=order.true_prob,
                )))
            await _cas_order(session, row.id, (RoutedOrderStatus.RESERVED,), status=RoutedOrderStatus.DISPATCHING.value, updated_at=now)
        outcomes = await self.dispatcher.dispatch([t for _, t in tickets]) if tickets else []
        await self._apply(order, [(s, o) for (s, _), o in zip(tickets, outcomes, strict=True)])
        await self._breakers([(s.venue_id, o) for (s, _), o in zip(tickets, outcomes, strict=True)])
        await self.finish(order.order_id)

    async def _apply(self, order: ExecutionOrder, results: Sequence[tuple[RoutedSlice, SliceOutcome]]) -> None:
        now = self.clock()
        async with self.sessions() as session, session.begin():
            for piece, outcome in sorted(results, key=lambda r: (str(r[0].account_id), r[0].slice_index)):
                ref = str(piece.client_ref)
                common = {"reason": outcome.reason[:64], "remote_bet_id": outcome.remote_bet_id, "ledger_id": outcome.ledger_id, "settled_at": now}
                if outcome.kind is SliceOutcomeKind.FILLED:
                    await _cas_slice(session, piece.id, (SliceStatus.DISPATCHED,), status=SliceStatus.FILLED.value, filled_stake=Decimal(piece.stake),
                                     matched_odds=_odds(outcome.matched_odds), **common)
                elif outcome.kind is SliceOutcomeKind.PARTIAL:
                    filled = min(max(outcome.filled_stake, ZERO), Decimal(piece.stake))
                    if await _cas_slice(session, piece.id, (SliceStatus.DISPATCHED,), status=SliceStatus.PARTIAL.value, filled_stake=filled,
                                        matched_odds=_odds(outcome.matched_odds), **common):
                        await account_rotator.shrink(session, ref, filled, "partial_fill", now=now)  # the unmatched rest lapsed: give it back
                elif outcome.kind is SliceOutcomeKind.REJECTED:
                    if await _cas_slice(session, piece.id, (SliceStatus.DISPATCHED,), status=SliceStatus.REJECTED.value, **common):
                        await account_rotator.release(session, ref, "rejected", now=now)  # released immediately
                else:
                    await _cas_slice(session, piece.id, (SliceStatus.DISPATCHED,), status=SliceStatus.UNKNOWN.value,
                                     reason=outcome.reason[:64], ledger_id=outcome.ledger_id)  # may be live: the hold stays
        for piece, outcome in results:
            logger.info("Router: %s -> %s (%s)", piece.idempotency_key, outcome.kind, outcome.reason)

    # -------------------------------------------------------------- circuit breakers
    async def _breakers(self, results: Sequence[tuple[str, SliceOutcome]]) -> None:
        now = self.clock()
        tripped: list[tuple[str, str]] = []
        for venue, outcome in sorted(results, key=lambda r: r[0]):
            async with self.sessions() as session, session.begin():
                if outcome.kind in (SliceOutcomeKind.FILLED, SliceOutcomeKind.PARTIAL):
                    await circuit_breaker.record_success(session, venue, now=now)
                elif outcome.venue_fault and await circuit_breaker.record_failure(session, venue, outcome.reason, self.policy, now=now):
                    tripped.append((venue, outcome.reason))
        for venue, reason in tripped:
            await self.on_trip(venue, reason)

    async def on_trip(self, venue: str, reason: str) -> int:
        """A venue's breaker tripped: release every slice still waiting to go there, page the Sentinel."""
        now = self.clock()
        async with self.sessions() as session, session.begin():
            waiting = list((await session.execute(
                select(RoutedSlice).where(RoutedSlice.venue_id == venue, RoutedSlice.status == SliceStatus.RESERVED.value)
            )).scalars())
            released = await release_slices(session, waiting, "VENUE_PAUSED", now=now)
        pause = int(self.policy.pause.total_seconds())
        await self.events.alert(_alert(
            "VENUE_CIRCUIT_OPEN", "CRITICAL", f"Venue paused: {venue}",
            f"{self.policy.failures} consecutive slices to {venue} were rejected or timed out within {int(self.policy.window.total_seconds())} s "
            f"(last: {reason}). Paused for {pause // 60} min; {len(released)} pending reservation(s) released.",
            f"router:venue:{venue}", {"venue_id": venue, "reason": reason, "pause_seconds": pause, "released": [s.idempotency_key for s in released]},
        ))
        logger.critical("Router: venue %s paused for %ss after consecutive failures (%s); %d hold(s) released", venue, pause, reason, len(released))
        return len(released)

    # -------------------------------------------------------------- the end of an order
    async def finish(self, order_id: str) -> dict[str, Any]:
        """Settle the order's status from its slices; receipt + Nalanda for a fill; the portfolio for a legged one."""
        now = self.clock()
        legged: dict[str, Any] | None = None
        async with self.sessions() as session, session.begin():
            row, slices = await _load(session, order_id)
            if row is None:
                raise KeyError(order_id)
            if row.status not in (RoutedOrderStatus.DISPATCHING.value, RoutedOrderStatus.UNCONFIRMED.value):
                return order_view(row, slices)
            filled = [s for s in slices if s.status in (SliceStatus.FILLED.value, SliceStatus.PARTIAL.value)]
            total = sum((Decimal(s.filled_stake) for s in filled), ZERO)
            weighted = sum((Decimal(s.filled_stake) * Decimal(s.matched_odds or s.guard_odds or s.quoted_odds or row.odds) for s in filled), ZERO)
            blended = (weighted / total).quantize(ODDS_QUANTUM) if total > 0 else None
            if any(s.status in (SliceStatus.UNKNOWN.value, SliceStatus.DISPATCHED.value) for s in slices):
                status = RoutedOrderStatus.UNCONFIRMED
            elif total >= Decimal(row.desired_total_stake):
                status = RoutedOrderStatus.FILLED
            elif total > 0:
                status = RoutedOrderStatus.PARTIAL
            else:
                status = RoutedOrderStatus.REJECTED
            values: dict[str, Any] = {"status": status.value, "filled_stake": total, "blended_odds": blended, "updated_at": now}
            if status is not RoutedOrderStatus.UNCONFIRMED:
                values["completed_at"] = now
            if status is RoutedOrderStatus.REJECTED:
                values["reason"] = "ALL_VENUES_REJECTED"
            if status is RoutedOrderStatus.PARTIAL:
                values["hedge_state"] = HEDGE_ELIGIBLE
                values["reason"] = "LEGGED"
            if not await _cas_order(session, row.id, (RoutedOrderStatus(row.status),), **values):
                return await self.view(order_id)
            await session.refresh(row)  # the row as the compare-and-set left it
            digest: str | None = None
            if status in (RoutedOrderStatus.FILLED, RoutedOrderStatus.PARTIAL):
                digest = receipt_digest(receipt_payload(row, slices))
                await session.execute(update(RoutedOrder).where(RoutedOrder.id == row.id).values(receipt_sha256=digest)
                                      .execution_options(synchronize_session=False))
            user_id, desired, currency, selection, match_id = row.user_id, Decimal(row.desired_total_stake), row.currency, row.selection, row.match_id
            if status is RoutedOrderStatus.PARTIAL:
                legged = {
                    "order_id": row.order_id, "match_id": row.match_id, "market": row.market, "selection": row.selection, "currency": row.currency,
                    "requested_stake": str(row.desired_total_stake), "filled_stake": str(total), "unfilled_stake": str(Decimal(row.desired_total_stake) - total),
                    "blended_odds": _s(blended), "hedge_eligible": True, "flagged_at": now.isoformat(),
                    "legs": [{"venue_id": s.venue_id, "status": s.status, "stake": str(s.stake), "filled_stake": str(s.filled_stake),
                              "matched_odds": _s(s.matched_odds), "remote_bet_id": s.remote_bet_id, "reason": s.reason} for s in slices],
                }
        if digest is not None:
            await self.mirror(order_id)
        if legged is not None:
            await self.events.legged(user_id, legged)
            await self.events.alert(_alert(
                "LEGGED_POSITION", "WARNING", f"Legged position: {selection} ({match_id})",
                f"{money_text(Decimal(legged['filled_stake']), currency)} of {money_text(desired, currency)} matched; "
                "the rest was refused. The position is flagged for hedging in the Active Portfolio.",
                f"router:legged:{order_id}", legged,
            ))
        return await self.view(order_id)

    async def mirror(self, order_id: str) -> int | None:
        """The receipt into Nalanda's hash chain (idempotent: a receipt is archived once). Returns its chain seq."""
        from app.models.nalanda_lake import MirrorIndex, SettlementArchive  # noqa: PLC0415
        from app.services.nalanda_chain import ArchiveRecord, append_records  # noqa: PLC0415

        try:
            async with self.sessions() as session, session.begin():
                row, slices = await _load(session, order_id)
                if row is None or row.receipt_sha256 is None:
                    return None
                payload = receipt_payload(row, slices)
                digest = receipt_digest(payload)
                source_id = f"routed_order:{row.order_id}"
                record = ArchiveRecord(
                    kind="ROUTED_ORDER", source="smart_router", source_id=source_id, payload={**payload, "receipt_sha256": digest}, user_id=row.user_id,
                    fixture_id=row.match_id[:128], amount_inr=Decimal(row.filled_stake) if row.currency == "INR" else None,
                    occurred_at=_aware(row.completed_at),
                )
                await append_records(session, [record], now=self.clock())
                seq = (await session.execute(select(MirrorIndex.seq).where(MirrorIndex.record_kind == "ROUTED_ORDER", MirrorIndex.source_id == source_id))).scalar_one()
                chained = (await session.execute(select(SettlementArchive.row_hash).where(SettlementArchive.seq == seq))).scalars().first()
                await session.execute(update(RoutedOrder).where(RoutedOrder.id == row.id).values(receipt_sha256=digest, nalanda_seq=int(seq), nalanda_hash=chained)
                                      .execution_options(synchronize_session=False))
            return int(seq)
        except Exception:  # noqa: BLE001 - the order is final either way; the sweep mirrors it again
            logger.exception("Router: receipt for %s not mirrored to Nalanda yet", order_id)
            return None

    # -------------------------------------------------------------- reads and operator actions
    async def view(self, order_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row, slices = await _load(session, order_id)
        if row is None:
            raise KeyError(order_id)
        return order_view(row, slices, now=self.clock(), orphan_after=float(self.settings.ROUTER_ORPHAN_SECONDS))

    async def release_orphan(self, slice_id: uuid.UUID, *, force: bool = False) -> dict[str, Any]:
        """The operator gives back a reservation that was never dispatched (only RESERVED: a sent slice may be live)."""
        now = self.clock()
        async with self.sessions() as session, session.begin():
            piece = await session.get(RoutedSlice, slice_id, populate_existing=True)
            if piece is None:
                raise KeyError(str(slice_id))
            if piece.status != SliceStatus.RESERVED.value:
                raise ValueError(f"the slice is {piece.status}: only a reservation that was never dispatched can be released")
            age = (now - (_aware(piece.reserved_at) or now)).total_seconds()
            if not force and age <= float(self.settings.ROUTER_ORPHAN_SECONDS):
                raise ValueError(f"the reservation is {int(age)} s old: an order may still dispatch it (orphaned after {int(self.settings.ROUTER_ORPHAN_SECONDS)} s)")
            await release_slices(session, [piece], "MANUAL_RELEASE", now=now)
            order = await session.get(RoutedOrder, piece.routed_order_id, populate_existing=True)
            assert order is not None
            order_id, order_status = order.order_id, order.status
            remaining = list((await session.execute(select(RoutedSlice.status).where(RoutedSlice.routed_order_id == order.id))).scalars())
            if all(s == SliceStatus.RELEASED.value for s in remaining):  # nothing left to send: the order is over
                await _cas_order(session, order.id, (RoutedOrderStatus.RESERVED, RoutedOrderStatus.ROUTING), status=RoutedOrderStatus.ABORTED.value,
                                 reason="MANUAL_RELEASE", completed_at=now, updated_at=now)
        if order_status == RoutedOrderStatus.DISPATCHING.value:
            await self.finish(order_id)  # its other slices already answered: settle it with this one released
        return await self.view(order_id)


def _odds(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(ODDS_QUANTUM)


# ------------------------------------------------------------------------------------------ listing
async def list_orders(session: AsyncSession, settings: Settings, *, active: bool = False, limit: int = 50, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _utcnow()
    query = select(RoutedOrder).order_by(RoutedOrder.created_at.desc()).limit(limit)
    if active:
        open_ids = select(RoutedSlice.routed_order_id).where(RoutedSlice.status.in_([s.value for s in OPEN_SLICE_STATUSES]))
        query = query.where(or_(
            RoutedOrder.status.in_([RoutedOrderStatus.ROUTING.value, RoutedOrderStatus.RESERVED.value, RoutedOrderStatus.DISPATCHING.value,
                                    RoutedOrderStatus.UNCONFIRMED.value]),
            RoutedOrder.hedge_state == HEDGE_ELIGIBLE, RoutedOrder.id.in_(open_ids),
        ))
    orders = list((await session.execute(query)).scalars())
    if not orders:
        return []
    slices: dict[uuid.UUID, list[RoutedSlice]] = {}
    for piece in (await session.execute(select(RoutedSlice).where(RoutedSlice.routed_order_id.in_([o.id for o in orders])))).scalars():
        slices.setdefault(piece.routed_order_id, []).append(piece)
    return [order_view(o, slices.get(o.id, []), now=now, orphan_after=float(settings.ROUTER_ORPHAN_SECONDS)) for o in orders]


# ------------------------------------------------------------------------------------------ the sweep
async def sweep_router(session_factory: async_sessionmaker[AsyncSession], settings: Settings, events: RouterEvents, *, now: datetime | None = None) -> dict[str, Any]:
    """What the dispatcher worker runs every ``ROUTER_SWEEP_INTERVAL_SECONDS`` (see its docstring)."""
    from app.models.cfo_vault import PhantomLedger  # noqa: PLC0415

    now = now or _utcnow()
    out: dict[str, Any] = {"synced_released": 0, "reconciled": 0, "venues_live": [], "mirrored": 0, "orphans": 0}
    router = SmartOrderRouter(session_factory, settings, quotes=_NoQuotes(), dispatcher=ExecutionDispatcher(_NoExecutor(), settings), events=events, clock=lambda: now)
    async with session_factory() as session, session.begin():
        # 1. the Vault's sweeper gave a never-dispatched slice's hold back (its TTL): the slice follows
        for piece in await _stale_reserved(session):
            if await _cas_slice(session, piece.id, (SliceStatus.RESERVED,), status=SliceStatus.RELEASED.value, released_at=now, reason="HOLD_EXPIRED"):
                out["synced_released"] += 1
        cutoff = now - timedelta(seconds=float(settings.ROUTER_ORPHAN_SECONDS))
        out["orphans"] = len(list((await session.execute(
            select(RoutedSlice.id).where(RoutedSlice.status == SliceStatus.RESERVED.value, RoutedSlice.reserved_at < cutoff)
        )).scalars()))
    # 2. slices sent without an answer that the ledger has since confirmed
    cutoff_unknown = now - timedelta(seconds=float(settings.ROUTER_SLICE_TIMEOUT_SECONDS))
    async with session_factory() as session:
        waiting = list((await session.execute(
            select(RoutedSlice).where(RoutedSlice.status.in_([SliceStatus.UNKNOWN.value, SliceStatus.DISPATCHED.value]),
                                      RoutedSlice.dispatched_at < cutoff_unknown)
        )).scalars())
        confirmed = {}
        if waiting:
            confirmed = {
                key: (status, stake, odds, ledger_id)
                for key, status, stake, odds, ledger_id in (await session.execute(
                    select(PhantomLedger.idempotency_key, PhantomLedger.status, PhantomLedger.stake_inr, PhantomLedger.odds, PhantomLedger.id)
                    .where(PhantomLedger.idempotency_key.in_([w.client_ref for w in waiting]))
                )).all()
            }
    touched: set[uuid.UUID] = set()
    for piece in waiting:
        hit = confirmed.get(piece.client_ref)
        if hit is None:
            continue
        status, stake, odds, ledger_id = hit
        state = str(getattr(status, "value", status))
        if state in ("REJECTED", "REQUIRES_MANUAL_INTERVENTION"):
            continue
        async with session_factory() as session, session.begin():
            filled = min(Decimal(stake), Decimal(piece.stake))
            kind = SliceStatus.FILLED if filled >= Decimal(piece.stake) else SliceStatus.PARTIAL
            if await _cas_slice(session, piece.id, (SliceStatus.UNKNOWN, SliceStatus.DISPATCHED), status=kind.value, filled_stake=filled,
                                matched_odds=_odds(Decimal(odds)), ledger_id=ledger_id, reason="CONFIRMED_BY_LEDGER", settled_at=now):
                if kind is SliceStatus.PARTIAL:
                    await account_rotator.shrink(session, str(piece.client_ref), filled, "partial_fill", now=now)
                out["reconciled"] += 1
                touched.add(piece.routed_order_id)
    for routed_id in touched:
        async with session_factory() as session:
            order = await session.get(RoutedOrder, routed_id)
        if order is not None:
            await router.finish(order.order_id)
    # 3. orders claimed and never planned (the process died between the claim and the reservation)
    async with session_factory() as session, session.begin():
        stuck = list((await session.execute(
            select(RoutedOrder).where(RoutedOrder.status == RoutedOrderStatus.ROUTING.value, RoutedOrder.created_at < cutoff)
        )).scalars())
        for order in stuck:
            if (await session.execute(select(RoutedSlice.id).where(RoutedSlice.routed_order_id == order.id).limit(1))).first() is None:
                await _cas_order(session, order.id, (RoutedOrderStatus.ROUTING,), status=RoutedOrderStatus.REJECTED.value, reason="ROUTING_ABANDONED",
                                 completed_at=now, updated_at=now)
    # 4. pauses that ran out
    async with session_factory() as session, session.begin():
        expired = await circuit_breaker.expired_pauses(session, now=now)
    for venue in expired:
        await events.alert(_alert("VENUE_CIRCUIT_CLOSED", "INFO", f"Venue live again: {venue}", f"The {venue} pause ran out; the router routes to it again.",
                                  f"router:venue:{venue}", {"venue_id": venue}, resolves=True))
    out["venues_live"] = expired
    # 5. receipts that missed Nalanda
    async with session_factory() as session:
        unmirrored = list((await session.execute(
            select(RoutedOrder.order_id).where(RoutedOrder.receipt_sha256.is_not(None), RoutedOrder.nalanda_seq.is_(None)).limit(200)
        )).scalars())
    for order_id in unmirrored:
        if await router.mirror(order_id) is not None:
            out["mirrored"] += 1
    return out


async def _stale_reserved(session: AsyncSession) -> list[RoutedSlice]:
    """RESERVED slices whose Vault hold is already released (references matched in Python: portable)."""
    reserved = list((await session.execute(select(RoutedSlice).where(RoutedSlice.status == SliceStatus.RESERVED.value))).scalars())
    if not reserved:
        return []
    refs = {str(s.client_ref): s for s in reserved}
    released = set((await session.execute(
        select(VaultAccountReservation.order_ref).where(VaultAccountReservation.order_ref.in_(list(refs)), VaultAccountReservation.released_at.is_not(None))
    )).scalars())
    return [refs[r] for r in released]


class _NoQuotes:
    async def quotes(self, match_id: str, market: str, selection: str, bookmakers: Sequence[str]) -> dict[str, LiveQuote]:  # noqa: ARG002
        return {}


class _NoExecutor:
    async def execute(self, ticket: SliceTicket) -> SliceOutcome:  # noqa: ARG002
        return SliceOutcome(SliceOutcomeKind.UNKNOWN, "SWEEP_NEVER_DISPATCHES")
