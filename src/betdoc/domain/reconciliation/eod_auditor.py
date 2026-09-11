# src/betdoc/domain/reconciliation/eod_auditor.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, UTC
from decimal import Decimal
from typing import Dict, List
from enum import Enum

from betdoc.infrastructure.ledger.paper_ledger import PaperLedger
from betdoc.infrastructure.execution.client import ExecutionClient
from betdoc.domain.math.money import from_paise, to_paise

class ReconciliationStatus(Enum):
    RECONCILED = "RECONCILED"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"

@dataclass(frozen=True)
class BookmakerAuditEntry:
    bookmaker: str
    reported_balance: Decimal
    is_reachable: bool
    error_message: str = ""

@dataclass(frozen=True)
class AuditReport:
    timestamp: datetime
    ledger_balance: Decimal
    total_real_balance: Decimal
    drift_amount: Decimal
    drift_percentage: Decimal
    status: ReconciliationStatus
    entries: List[BookmakerAuditEntry]
    is_reconciled: bool

class EndOfDayAuditor:
    """
    Cross-references the immutable paper ledger against actual bookmaker funds.
    Runs all balance queries concurrently. Tolerates partial failures gracefully.
    """
    def __init__(self, ledger: PaperLedger, clients: Dict[str, ExecutionClient], tolerance_paise: int = 100):
        self.ledger = ledger
        self.clients = clients
        self.tolerance_paise = tolerance_paise

    async def run_audit(self) -> AuditReport:
        snapshot = await self.ledger.snapshot()
        ledger_balance = from_paise(snapshot.balance_paise)
        
        entries = []
        total_real_balance = Decimal("0.00")
        has_failure = False

        async def fetch_balance(name: str, client: ExecutionClient) -> tuple[str, Decimal | Exception]:
            try:
                balance = await client.get_balance()
                return name, balance
            except Exception as e:
                return name, e

        tasks = [fetch_balance(name, client) for name, client in self.clients.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for name, result in results:
            if isinstance(result, Exception):
                has_failure = True
                entries.append(BookmakerAuditEntry(
                    bookmaker=name,
                    reported_balance=Decimal("0.00"),
                    is_reachable=False,
                    error_message=str(result)
                ))
            else:
                total_real_balance += result
                entries.append(BookmakerAuditEntry(
                    bookmaker=name,
                    reported_balance=result,
                    is_reachable=True,
                    error_message=""
                ))

        drift_amount = abs(total_real_balance - ledger_balance)
        
        if ledger_balance > 0:
            drift_percentage = (drift_amount / ledger_balance) * 100
        else:
            drift_percentage = Decimal("0.00")

        # Use rounding standard from domain
        drift_amount_paise = to_paise(drift_amount)
        is_reconciled = drift_amount_paise <= self.tolerance_paise

        if has_failure:
            status = ReconciliationStatus.PARTIAL_FAILURE
        elif is_reconciled:
            status = ReconciliationStatus.RECONCILED
        else:
            status = ReconciliationStatus.DRIFT_DETECTED

        return AuditReport(
            timestamp=datetime.now(UTC),
            ledger_balance=ledger_balance,
            total_real_balance=total_real_balance,
            drift_amount=drift_amount,
            drift_percentage=drift_percentage,
            status=status,
            entries=entries,
            is_reconciled=is_reconciled
        )
