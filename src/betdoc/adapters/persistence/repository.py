"""Repository boundary. The domain never sees SQLAlchemy, only these methods."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from betdoc.adapters.persistence.models import BetRecord, BetStatus, OddsTick, utc_now

__all__ = ["BetRepository", "IllegalBetTransitionError", "OddsRepository"]


class IllegalBetTransitionError(ValueError):
    """An attempt to re-settle an already terminal bet."""

    def __init__(self, bet_id: uuid.UUID, current: BetStatus, attempted: BetStatus) -> None:
        super().__init__(
            f"bet {bet_id} is already terminal in {current.value!r}; "
            f"refusing to overwrite with {attempted.value!r}"
        )
        self.bet_id = bet_id
        self.current = current
        self.attempted = attempted


class BetRepository:
    """Persistence for placed bets. Flushes but never commits.

    Commit authority belongs to the unit of work that owns the session, so a
    bet insert and its ledger transaction land in the same atomic commit.
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_bet(
        self,
        *,
        event_id: str,
        market_key: str,
        outcome_key: str,
        bookmaker: str,
        odds: float,
        stake_paise: int,
        status: BetStatus = BetStatus.PENDING,
        placed_at: datetime | None = None,
        bet_id: uuid.UUID | None = None,
    ) -> BetRecord:
        """Insert a bet, validating money and price before touching the DB."""
        if isinstance(stake_paise, bool) or not isinstance(stake_paise, int):
            msg = f"stake_paise must be an int in paise, got {type(stake_paise).__name__}"
            raise ValueError(msg)
        if stake_paise <= 0:
            msg = f"stake_paise must be positive, got {stake_paise}"
            raise ValueError(msg)
        if not odds > 1.0:
            msg = f"odds must exceed 1.0, got {odds!r}"
            raise ValueError(msg)
        if status.is_terminal:
            msg = "a bet cannot be created in a terminal state; save it PENDING then settle"
            raise ValueError(msg)

        bet = BetRecord(
            id=bet_id or uuid.uuid4(),
            event_id=event_id,
            market_key=market_key,
            outcome_key=outcome_key,
            bookmaker=bookmaker,
            odds=odds,
            stake_paise=stake_paise,
            status=status,
            placed_at=placed_at or utc_now(),
            settled_at=None,
        )
        self._session.add(bet)
        await self._session.flush()
        return bet

    async def get_bet(self, bet_id: uuid.UUID) -> BetRecord | None:
        """Fetch by primary key. ``None`` when absent, never an exception."""
        return await self._session.get(BetRecord, bet_id)

    async def settle_bet(
        self,
        bet_id: uuid.UUID,
        status: BetStatus,
        *,
        settled_at: datetime | None = None,
    ) -> BetRecord:
        """Transition a bet to a terminal state, exactly once.

        The row is locked with ``FOR UPDATE`` so two concurrent settlement
        messages serialise instead of racing. The second one then observes the
        terminal state and raises, which is the correct outcome: a duplicate
        settlement must be investigated, not silently applied twice.
        """
        if not status.is_terminal:
            msg = f"settle_bet requires a terminal status, got {status.value!r}"
            raise ValueError(msg)

        bet = await self._session.scalar(
            select(BetRecord).where(BetRecord.id == bet_id).with_for_update()
        )
        if bet is None:
            msg = f"bet {bet_id} does not exist"
            raise LookupError(msg)
        if bet.status.is_terminal:
            if bet.status is status:
                return bet
            raise IllegalBetTransitionError(bet_id, bet.status, status)

        bet.status = status
        bet.settled_at = settled_at or utc_now()
        await self._session.flush()
        return bet

    async def list_open_bets(self, *, limit: int = 500) -> tuple[BetRecord, ...]:
        """Pending bets, oldest first. Drives the settlement worker."""
        rows = await self._session.scalars(
            select(BetRecord)
            .where(BetRecord.status == BetStatus.PENDING)
            .order_by(BetRecord.placed_at)
            .limit(limit)
        )
        return tuple(rows.all())

    async def total_open_exposure_paise(self) -> int:
        """Capital currently at risk, summed in SQL for exactness."""
        from sqlalchemy import func

        total = await self._session.scalar(
            select(func.coalesce(func.sum(BetRecord.stake_paise), 0)).where(
                BetRecord.status == BetStatus.PENDING
            )
        )
        return int(total or 0)


class OddsRepository:
    """Persistence for the odds time series."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_tick(
        self,
        *,
        bookmaker: str,
        event_id: str,
        market_key: str,
        outcome_key: str,
        decimal_odds: float,
        timestamp: datetime | None = None,
        tick_id: uuid.UUID | None = None,
    ) -> OddsTick:
        if not decimal_odds > 1.0:
            msg = f"decimal_odds must exceed 1.0, got {decimal_odds!r}"
            raise ValueError(msg)

        tick = OddsTick(
            id=tick_id or uuid.uuid4(),
            bookmaker=bookmaker,
            event_id=event_id,
            market_key=market_key,
            outcome_key=outcome_key,
            decimal_odds=decimal_odds,
            timestamp=timestamp or utc_now(),
        )
        self._session.add(tick)
        await self._session.flush()
        return tick

    async def save_ticks_ignoring_duplicates(self, ticks: Sequence[dict[str, object]]) -> int:
        """Bulk insert, skipping natural-key duplicates.

        Concurrent pollers frequently observe the same tick. On PostgreSQL this
        becomes a single ``ON CONFLICT DO NOTHING`` statement, so racing writers
        cost nothing and need no coordination. Other dialects fall back to
        per-row inserts inside savepoints.
        """
        if not ticks:
            return 0

        dialect = self._session.bind.dialect.name if self._session.bind else ""
        if dialect == "postgresql":
            statement = (
                pg_insert(OddsTick)
                .values(list(ticks))
                .on_conflict_do_nothing(constraint="uq_odds_ticks_natural_key")
            )
            result = await self._session.execute(statement)
            return int(result.rowcount or 0)

        from sqlalchemy.exc import IntegrityError

        inserted = 0
        for payload in ticks:
            savepoint = await self._session.begin_nested()
            try:
                self._session.add(OddsTick(**payload))  # type: ignore[arg-type]
                await self._session.flush()
            except IntegrityError:
                await savepoint.rollback()
            else:
                await savepoint.commit()
                inserted += 1
        return inserted

    async def get_latest_odds(
        self,
        *,
        event_id: str,
        market_key: str,
        outcome_key: str,
        bookmaker: str | None = None,
    ) -> OddsTick | None:
        """Most recent tick for one selection, optionally at one bookmaker.

        Served entirely by ``ix_odds_ticks_lookup`` as a backwards index scan
        with an early stop, so it stays constant-time as the hypertable grows.
        """
        statement = (
            select(OddsTick)
            .where(
                OddsTick.event_id == event_id,
                OddsTick.market_key == market_key,
                OddsTick.outcome_key == outcome_key,
            )
            .order_by(OddsTick.timestamp.desc())
            .limit(1)
        )
        if bookmaker is not None:
            statement = statement.where(OddsTick.bookmaker == bookmaker)
        return await self._session.scalar(statement)

    async def get_best_odds_across_books(
        self, *, event_id: str, market_key: str, outcome_key: str
    ) -> OddsTick | None:
        """Highest current price across bookmakers. The line-shopping primitive."""
        from sqlalchemy import func

        latest_per_book = (
            select(
                OddsTick.bookmaker,
                func.max(OddsTick.timestamp).label("latest"),
            )
            .where(
                OddsTick.event_id == event_id,
                OddsTick.market_key == market_key,
                OddsTick.outcome_key == outcome_key,
            )
            .group_by(OddsTick.bookmaker)
            .subquery()
        )
        statement = (
            select(OddsTick)
            .join(
                latest_per_book,
                (OddsTick.bookmaker == latest_per_book.c.bookmaker)
                & (OddsTick.timestamp == latest_per_book.c.latest),
            )
            .where(
                OddsTick.event_id == event_id,
                OddsTick.market_key == market_key,
                OddsTick.outcome_key == outcome_key,
            )
            .order_by(OddsTick.decimal_odds.desc())
            .limit(1)
        )
        return await self._session.scalar(statement)

    async def get_history(
        self,
        *,
        event_id: str,
        market_key: str,
        outcome_key: str,
        since: datetime | None = None,
        limit: int = 1_000,
    ) -> tuple[OddsTick, ...]:
        statement = (
            select(OddsTick)
            .where(
                OddsTick.event_id == event_id,
                OddsTick.market_key == market_key,
                OddsTick.outcome_key == outcome_key,
            )
            .order_by(OddsTick.timestamp.desc())
            .limit(limit)
        )
        if since is not None:
            statement = statement.where(OddsTick.timestamp >= since)
        rows = await self._session.scalars(statement)
        return tuple(rows.all())
