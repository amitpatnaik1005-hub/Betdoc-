from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from betdoc.infrastructure.ledger.sqlite_ledger import SqliteExposureLedger


class Receipt(BaseModel):
    idempotency_key: str
    status: Literal["recorded", "rejected"]
    reason: str


class WalletSnapshot(BaseModel):
    balance_paise: int
    revision: int
    currency: Literal["INR"] = "INR"
    mode: Literal["paper"] = "paper"
    receipt: Receipt | None = None


class PaperLedger(SqliteExposureLedger):
    """Isolated simulation ledger. Never submits an order to a bookmaker.

    A receipt, debit and exposure commit in the SAME SQLite transaction.
    Receipt tombstones are never pruned: a retry cannot resurrect a debit.
    Each worker owns its connection, avoiding interleaved transactions on the
    shared aiosqlite connection used by the existing exposure reader.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path

    async def connect(self) -> None:
        await super().connect()
        await asyncio.to_thread(self._initialize)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connection()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS paper_wallet (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    balance INTEGER NOT NULL CHECK(balance >= 0),
                    revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO paper_wallet VALUES(1, 1042000, 0);
                CREATE TABLE IF NOT EXISTS paper_receipts (
                    key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('recorded', 'rejected')),
                    reason TEXT NOT NULL
                );
            """)
        finally:
            connection.close()

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, key: str | None) -> WalletSnapshot:
        wallet = connection.execute("SELECT balance, revision FROM paper_wallet WHERE id=1").fetchone()
        row = connection.execute(
            "SELECT key, status, reason FROM paper_receipts WHERE key=?", (key,)
        ).fetchone() if key else None
        if wallet is None:
            raise RuntimeError("Paper wallet is not initialized")
        return WalletSnapshot(
            balance_paise=wallet["balance"], revision=wallet["revision"],
            receipt=Receipt(idempotency_key=row["key"], status=row["status"], reason=row["reason"])
            if row else None,
        )

    async def snapshot(self, key: str | None = None) -> WalletSnapshot:
        return await asyncio.to_thread(self._read, key)

    def _read(self, key: str | None) -> WalletSnapshot:
        connection = self._connection()
        try:
            connection.execute("BEGIN")
            result = self._snapshot(connection, key)
            connection.commit()
            return result
        finally:
            connection.close()

    async def place(
        self, *, key: str, market_id: str, model: str, stake_paise: int,
        sport: str, rejection: str | None,
    ) -> WalletSnapshot:
        if not 0 < stake_paise <= 1_000_000_000:
            raise ValueError("Invalid stake")
        fingerprint = json.dumps([market_id, model, stake_paise], separators=(",", ":"))
        return await asyncio.to_thread(
            self._place, key, fingerprint, stake_paise, sport, rejection
        )

    def _place(
        self, key: str, fingerprint: str, stake: int, sport: str, rejection: str | None
    ) -> WalletSnapshot:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT fingerprint FROM paper_receipts WHERE key=?", (key,)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("Idempotency key already belongs to a different payload")
            else:
                balance = self._snapshot(connection, None).balance_paise
                reason = rejection or ("Insufficient paper bankroll" if stake > balance else "")
                status = "rejected" if reason else "recorded"
                if status == "recorded":
                    connection.execute(
                        "INSERT INTO open_exposures VALUES (?, ?, ?, ?, ?)",
                        (key, "paper:primary", sport, stake, datetime.now(UTC).isoformat()),
                    )
                    connection.execute(
                        "UPDATE paper_wallet SET balance=balance-? WHERE id=1", (stake,)
                    )
                    reason = "Recorded in the paper ledger; no sportsbook order was sent."
                connection.execute(
                    "INSERT INTO paper_receipts VALUES (?, ?, ?, ?)",
                    (key, fingerprint, status, reason),
                )
                connection.execute("UPDATE paper_wallet SET revision=revision+1 WHERE id=1")
            result = self._snapshot(connection, key)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
