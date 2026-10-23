"""TODAR MAL's reconciliation of the real cash vault (Group 114, point a.7). Developed for Amit Ashok Kumar Patnaik.

* **Exposure**: the vault's locked exposure must equal the stakes of its PLACED tickets. A drift is corrected to the
  open tickets' sum (that sum is the truth: every open stake is on a ticket) and reported with its size.
* **Cash**: liquid cash is re-derived from the cash ledger, every row of which moved it: deposits, payouts and refunds
  in; withdrawals, locked stakes and TDS out (a forfeited stake had already left liquid cash when it was locked). A
  difference is reported and never written: a person decides what happened to money.

The caller commits, then announces.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.real_money import RealBetTicket, RealCashTransaction, RealTicketStatus, RealTransactionType
from app.services import real_money_service as rms

logger = logging.getLogger("betdoc.vault_reconciliation")

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
CASH_IN = frozenset({RealTransactionType.DEPOSIT.value, RealTransactionType.BET_PAYOUT_CREDIT.value, RealTransactionType.BET_REFUND.value})
CASH_OUT = frozenset({RealTransactionType.WITHDRAWAL.value, RealTransactionType.BET_STAKE_LOCKED.value, RealTransactionType.TDS_WITHHOLDING_DEBIT.value})


async def reconcile(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    bankroll = await rms.get_or_create_bankroll(db, user_id, now)
    stakes = [s for (s,) in (await db.execute(select(RealBetTicket.placed_stake).where(
        RealBetTicket.user_id == user_id, RealBetTicket.status == RealTicketStatus.PLACED.value))).all()]
    expected = sum(stakes, rms.ZERO)
    recorded = bankroll.locked_exposure
    drift = recorded - expected
    if drift != 0:
        logger.warning("vault exposure drift for %s: recorded ₹%s, open tickets ₹%s (%s); corrected", user_id, recorded, expected, drift)
        bankroll.locked_exposure = expected
        bankroll.updated_at = now
        await db.flush()

    sums = {kind: total for kind, total in (await db.execute(
        select(RealCashTransaction.transaction_type, func.coalesce(func.sum(RealCashTransaction.amount), 0))
        .where(RealCashTransaction.user_id == user_id).group_by(RealCashTransaction.transaction_type))).all()}
    ledger_cash = sum((Decimal(str(sums.get(k, 0))) for k in CASH_IN), rms.ZERO) - sum((Decimal(str(sums.get(k, 0))) for k in CASH_OUT), rms.ZERO)
    cash_drift = bankroll.cash_balance - ledger_cash
    if cash_drift != 0:
        logger.warning("vault cash for %s differs from its ledger by ₹%s (reported, not corrected)", user_id, cash_drift)
    return {
        "reconciled": cash_drift == 0, "open_tickets_count": len(stakes), "previous_exposure": float(recorded), "locked_exposure": float(expected),
        "exposure_drift": float(drift), "adjusted": drift != 0, "liquid_cash": float(bankroll.cash_balance), "ledger_cash": float(ledger_cash),
        "cash_drift": float(cash_drift), "total_bankroll": float(bankroll.cash_balance + bankroll.locked_exposure), "checked_at": now.isoformat(),
        "developer_name": DEVELOPER_CREDIT,
    }


__all__ = ["CASH_IN", "CASH_OUT", "reconcile"]
