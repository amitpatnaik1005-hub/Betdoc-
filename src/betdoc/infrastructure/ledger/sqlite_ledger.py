from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite
import structlog

from betdoc.domain.shared.clock import Clock, SystemClock


class SqliteExposureLedger:
    """Durable SQLite-backed ledger satisfying ExposureLedgerPort.
    
    Provides idempotency and crash resilience for real-money exposure tracking.
    """

    __slots__ = ("_clock", "_db_path", "_log", "_pool", "_retain_days")

    def __init__(
        self,
        db_path: str,
        *,
        clock: Clock | None = None,
        retain_days: int = 7,
    ) -> None:
        if retain_days < 1:
            raise ValueError("retain_days must be >= 1")
        self._db_path = db_path
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._retain_days = retain_days
        self._log: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger("betdoc.ledger.sqlite")
        self._pool: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._pool = await aiosqlite.connect(self._db_path)
        await self._init_schema()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _init_schema(self) -> None:
        if self._pool is None:
            return

        await self._pool.executescript("""
            CREATE TABLE IF NOT EXISTS open_exposures (
                idempotency_key TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                sport_type TEXT NOT NULL,
                stake_paise INTEGER NOT NULL,
                placed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_open_exposures_profile ON open_exposures(profile_id, sport_type);

            CREATE TABLE IF NOT EXISTS settlements (
                idempotency_key TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                sport_type TEXT NOT NULL,
                stake_paise INTEGER NOT NULL,
                payout_paise INTEGER NOT NULL,
                net_pnl INTEGER NOT NULL,
                settled_at TEXT NOT NULL,
                settled_date TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_settlements_profile_date ON settlements(profile_id, settled_date);
        """)
        await self._pool.commit()

    async def realized_loss_today_paise(self, *, profile_id: str, as_of: datetime) -> int:
        if self._pool is None:
            raise RuntimeError("Database not connected")

        utc_date = as_of.astimezone(UTC).date().isoformat()

        async with self._pool.execute(
            "SELECT SUM(net_pnl) FROM settlements WHERE profile_id = ? AND settled_date = ?",
            (profile_id, utc_date)
        ) as cursor:
            row = await cursor.fetchone()
            net_pnl = row[0] if row and row[0] is not None else 0

        return -net_pnl if net_pnl < 0 else 0

    async def open_exposure_paise(self, *, profile_id: str, sport_type: str) -> int:
        if self._pool is None:
            raise RuntimeError("Database not connected")

        async with self._pool.execute(
            "SELECT SUM(stake_paise) FROM open_exposures WHERE profile_id = ? AND sport_type = ?",
            (profile_id, sport_type)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else 0

    async def record_placement(self, *, profile_id: str, sport_type: str, stake_paise: int, idempotency_key: str) -> None:
        if self._pool is None:
            raise RuntimeError("Database not connected")
        if stake_paise <= 0:
            raise ValueError("stake_paise must be positive")

        placed_at = self._clock.now().astimezone(UTC).isoformat()

        try:
            await self._pool.execute(
                """
                INSERT INTO open_exposures (idempotency_key, profile_id, sport_type, stake_paise, placed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (idempotency_key, profile_id, sport_type, stake_paise, placed_at)
            )
            await self._pool.commit()
        except Exception as e:
            self._log.error("ledger.placement_failed", error=str(e), idempotency_key=idempotency_key)
            raise

    async def record_settlement(
        self,
        *,
        profile_id: str,
        sport_type: str,
        stake_paise: int,
        payout_paise: int,
        settled_at: datetime,
        idempotency_key: str,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("Database not connected")
        if stake_paise <= 0:
            raise ValueError("stake_paise must be positive")
        if payout_paise < 0:
            raise ValueError("payout_paise must be non-negative")

        net_pnl = payout_paise - stake_paise
        settled_at_utc = settled_at.astimezone(UTC)
        settled_iso = settled_at_utc.isoformat()
        settled_date = settled_at_utc.date().isoformat()

        try:
            await self._pool.execute("BEGIN TRANSACTION")

            # Move from open to settled
            await self._pool.execute(
                "DELETE FROM open_exposures WHERE idempotency_key = ?",
                (idempotency_key,)
            )

            await self._pool.execute(
                """
                INSERT INTO settlements (idempotency_key, profile_id, sport_type, stake_paise, payout_paise, net_pnl, settled_at, settled_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (idempotency_key, profile_id, sport_type, stake_paise, payout_paise, net_pnl, settled_iso, settled_date)
            )

            await self._pool.commit()

            # Asynchronously trigger prune (fire and forget)
            self._prune_task = asyncio.create_task(self._prune_old_days())

        except Exception as e:
            await self._pool.rollback()
            self._log.error("ledger.settlement_failed", error=str(e), idempotency_key=idempotency_key)
            raise

    async def _prune_old_days(self) -> None:
        if self._pool is None:
            return

        cutoff = (self._clock.now() - timedelta(days=self._retain_days)).date().isoformat()
        try:
            await self._pool.execute(
                "DELETE FROM settlements WHERE settled_date < ?",
                (cutoff,)
            )
            await self._pool.commit()
        except Exception as e:
            self._log.warning("ledger.prune_failed", error=str(e))
