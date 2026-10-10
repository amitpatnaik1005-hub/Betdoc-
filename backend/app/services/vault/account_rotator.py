"""Which Vault account carries an order, and the stake held on it until the order settles (Group 70).

``get_optimal_account(bookmaker_id, required_stake)`` picks ONE account for the whole order:

* only active accounts that are not FAILED verification, hold some credential, and settle in the
  order's currency;
* that can carry the stake: under the user's own per-order cap (``stake_cap``) and within the free
  funds (``balance - reserved``; an account without a recorded balance is not funds-limited);
* ranked by the user's priority (1 = primary), then by the most free funds, then least recently used.

``reserve`` then holds the stake on that account atomically (a conditional UPDATE: two orders racing
for the last funds cannot both win; the loser moves to the next account), keyed by the order's
idempotency key, so a retried order holds once. ``release`` gives it back when the order is refused,
and the sweep (``release_finished``) once the ledger settles it or it never reached the ledger.

An order is never split across accounts, and nothing here exists to keep a bookmaker from seeing
the user's activity: most of these books allow one account per person, and the books' own limits
are theirs to set. When no single account can carry the stake the answer says so.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.omni_vault import VaultAccountReservation, VaultBookmakerAccount, VerificationStatus

logger = logging.getLogger("betdoc.vault.rotator")

ZERO = Decimal(0)
_OPEN_LEDGER_STATES = ("PENDING", "REQUIRES_MANUAL_INTERVENTION")


@dataclass(frozen=True, slots=True)
class AccountChoice:
    account_id: uuid.UUID
    bookmaker_id: str
    label: str
    currency: str
    free_funds: Decimal | None  # None: no balance recorded
    stake_cap: Decimal | None
    priority: int
    order_ref: str | None = None  # set once reserved


@dataclass(frozen=True, slots=True)
class NoAccount:
    reason: str  # NO_ACCOUNTS | NONE_ACTIVE | CURRENCY | OVER_CAP | INSUFFICIENT_FUNDS
    message: str
    largest_capacity: Decimal | None = None


def _has_credentials(row: VaultBookmakerAccount) -> bool:
    return bool(row.encrypted_password or row.encrypted_api_key or row.encrypted_token)


def free_funds(row: VaultBookmakerAccount) -> Decimal | None:
    return None if row.balance is None else max(ZERO, Decimal(row.balance) - Decimal(row.reserved or 0))


def capacity(row: VaultBookmakerAccount) -> Decimal | None:
    """The most this account can carry on one order (None: unlimited as far as BetDoc knows)."""
    limits = [x for x in (free_funds(row), None if row.stake_cap is None else Decimal(row.stake_cap)) if x is not None]
    return min(limits) if limits else None


def _rank(row: VaultBookmakerAccount) -> tuple[int, Decimal, datetime]:
    funds = free_funds(row)
    used = row.last_used_at or datetime.min.replace(tzinfo=UTC)
    if used.tzinfo is None:
        used = used.replace(tzinfo=UTC)
    return (int(row.priority or 100), -(funds if funds is not None else Decimal("1e18")), used)


async def candidates(session: AsyncSession, bookmaker_id: str, required_stake: Decimal, *, currency: str | None = None) -> tuple[list[VaultBookmakerAccount], NoAccount | None]:
    rows = list((await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.bookmaker_id == bookmaker_id))).scalars())
    if not rows:
        return [], NoAccount("NO_ACCOUNTS", f"no {bookmaker_id} account in the Vault")
    usable = [r for r in rows if r.is_active and r.verification_status != VerificationStatus.FAILED.value and _has_credentials(r)]
    if not usable:
        return [], NoAccount("NONE_ACTIVE", f"every {bookmaker_id} account is disabled, failed verification or has no credentials")
    if currency is not None:
        usable = [r for r in usable if r.currency.upper() == currency.upper()]
        if not usable:
            return [], NoAccount("CURRENCY", f"no active {bookmaker_id} account settles in {currency.upper()}")
    able = [r for r in usable if (cap := capacity(r)) is None or cap >= required_stake]
    if not able:
        largest = max((c for r in usable if (c := capacity(r)) is not None), default=None)
        over_cap = all(r.stake_cap is not None and Decimal(r.stake_cap) < required_stake for r in usable)
        reason = "OVER_CAP" if over_cap else "INSUFFICIENT_FUNDS"
        return [], NoAccount(reason, f"no single {bookmaker_id} account can carry {required_stake}: the largest can take {largest}", largest)
    return sorted(able, key=_rank), None


def _choice(row: VaultBookmakerAccount, order_ref: str | None = None) -> AccountChoice:
    return AccountChoice(row.id, row.bookmaker_id, row.label, row.currency.upper(), free_funds(row),
                         None if row.stake_cap is None else Decimal(row.stake_cap), int(row.priority or 100), order_ref)


async def get_optimal_account(session: AsyncSession, bookmaker_id: str, required_stake: Decimal, *, currency: str | None = None) -> AccountChoice | NoAccount:
    """The account that would carry the order (nothing is held: see ``reserve``)."""
    if required_stake <= 0:
        raise ValueError("required_stake must be positive")
    ranked, refusal = await candidates(session, bookmaker_id, Decimal(required_stake), currency=currency)
    return refusal if refusal is not None else _choice(ranked[0])


async def reserve(session: AsyncSession, bookmaker_id: str, required_stake: Decimal, order_ref: str, *, currency: str | None = None, now: datetime | None = None) -> AccountChoice | NoAccount:
    """Hold the stake on the best account that can still carry it (the caller commits). Idempotent per order_ref."""
    stake = Decimal(required_stake)
    if stake <= 0:
        raise ValueError("required_stake must be positive")
    now = now or datetime.now(UTC)
    held = (await session.execute(select(VaultAccountReservation).where(VaultAccountReservation.order_ref == order_ref))).scalars().first()
    if held is not None:
        row = await session.get(VaultBookmakerAccount, held.account_id)
        if row is not None and held.released_at is None:
            return _choice(row, order_ref)
        return NoAccount("RELEASED", f"order {order_ref} already held and released a stake")
    ranked, refusal = await candidates(session, bookmaker_id, stake, currency=currency)
    if refusal is not None:
        return refusal
    for row in ranked:
        funds_ok = or_(VaultBookmakerAccount.balance.is_(None), VaultBookmakerAccount.balance - VaultBookmakerAccount.reserved >= stake)
        cap_ok = or_(VaultBookmakerAccount.stake_cap.is_(None), VaultBookmakerAccount.stake_cap >= stake)
        result = await session.execute(
            update(VaultBookmakerAccount)
            .where(VaultBookmakerAccount.id == row.id, VaultBookmakerAccount.is_active.is_(True), funds_ok, cap_ok)
            .values(reserved=VaultBookmakerAccount.reserved + stake, last_used_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:  # type: ignore[attr-defined]
            session.add(VaultAccountReservation(account_id=row.id, order_ref=order_ref, amount=stake, currency=row.currency.upper(), created_at=now))
            await session.flush()
            await session.refresh(row)
            return _choice(row, order_ref)
    return NoAccount("INSUFFICIENT_FUNDS", f"the {bookmaker_id} accounts' free funds were taken by orders in flight")


async def release(session: AsyncSession, order_ref: str, reason: str, *, now: datetime | None = None) -> bool:
    """Give an order's held stake back (the caller commits). False: nothing was held, or already released."""
    held = (await session.execute(
        select(VaultAccountReservation).where(VaultAccountReservation.order_ref == order_ref, VaultAccountReservation.released_at.is_(None)).with_for_update()
    )).scalars().first()
    if held is None:
        return False
    held.released_at, held.release_reason = now or datetime.now(UTC), reason[:32]
    amount = Decimal(held.amount)
    await session.execute(
        update(VaultBookmakerAccount)
        .where(VaultBookmakerAccount.id == held.account_id)
        .values(reserved=case((VaultBookmakerAccount.reserved >= amount, VaultBookmakerAccount.reserved - amount), else_=ZERO))
        .execution_options(synchronize_session=False)
    )
    return True


async def release_finished(session_factory: async_sessionmaker[AsyncSession], ttl: timedelta, *, now: datetime | None = None, limit: int = 500) -> dict[str, int]:
    """Release holds whose order the ledger has settled, and holds whose order never reached the ledger."""
    from app.models.cfo_vault import PhantomLedger  # noqa: PLC0415 - the ledger is the CFO's

    now = now or datetime.now(UTC)
    out = {"settled": 0, "abandoned": 0}
    async with session_factory() as session:
        held = list((await session.execute(
            select(VaultAccountReservation).where(VaultAccountReservation.released_at.is_(None)).order_by(VaultAccountReservation.created_at).limit(limit)
        )).scalars())
        if not held:
            return out
        refs: dict[uuid.UUID, str] = {}
        for h in held:
            try:
                refs[uuid.UUID(h.order_ref)] = h.order_ref
            except ValueError:
                continue
        status: dict[str, str] = {}
        if refs:
            for key, state in (await session.execute(select(PhantomLedger.idempotency_key, PhantomLedger.status).where(PhantomLedger.idempotency_key.in_(list(refs))))).all():
                status[refs[key]] = str(getattr(state, "value", state))
        for h in held:
            created = h.created_at if h.created_at.tzinfo else h.created_at.replace(tzinfo=UTC)
            state = status.get(h.order_ref)
            if state is not None and state not in _OPEN_LEDGER_STATES:
                await release(session, h.order_ref, "settled", now=now)
                out["settled"] += 1
            elif state is None and now - created > ttl:
                await release(session, h.order_ref, "abandoned", now=now)
                out["abandoned"] += 1
        await session.commit()
    return out
