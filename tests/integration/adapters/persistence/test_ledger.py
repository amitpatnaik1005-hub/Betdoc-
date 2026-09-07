"""Double-entry invariants. An unbalanced transaction must never persist."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.persistence.ledger import (
    DuplicateTransactionError,
    LedgerError,
    TransactionLeg,
    UnbalancedTransactionError,
    UnknownAccountError,
    assert_ledger_balanced,
    ensure_account,
    get_account_balance,
    get_balances,
    get_transaction_legs,
    record_transaction,
)
from adapters.persistence.models import AccountType, LedgerAccount, LedgerEntry

pytestmark = pytest.mark.asyncio


async def test_balanced_bet_placement_updates_both_balances(
    session: AsyncSession,
    cash_account: LedgerAccount,
    open_bets_account: LedgerAccount,
) -> None:
    """The canonical transaction: INR 50 leaves cash and becomes an open bet.

    Credit CASH by -5,000 paise, debit OPEN_BETS by +5,000 paise. Balances must
    land on exactly -5,000 and +5,000, and the pair must net to zero.
    """
    transaction_id = await record_transaction(
        session,
        [
            TransactionLeg(account_id=cash_account.id, amount_paise=-5_000),
            TransactionLeg(account_id=open_bets_account.id, amount_paise=5_000),
        ],
    )
    await session.commit()

    assert await get_account_balance(session, cash_account.id) == -5_000
    assert await get_account_balance(session, open_bets_account.id) == 5_000

    legs = await get_transaction_legs(session, transaction_id)
    assert len(legs) == 2
    assert sum(leg.amount_paise for leg in legs) == 0
    await assert_ledger_balanced(session)


async def test_unbalanced_transaction_raises_value_error(
    session: AsyncSession,
    cash_account: LedgerAccount,
    open_bets_account: LedgerAccount,
) -> None:
    """The core guarantee of the whole module."""
    with pytest.raises(UnbalancedTransactionError) as excinfo:
        await record_transaction(
            session,
            [
                TransactionLeg(account_id=cash_account.id, amount_paise=-5_000),
                TransactionLeg(account_id=open_bets_account.id, amount_paise=4_999),
            ],
        )

    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.total_paise == -1
    assert excinfo.value.leg_count == 2


async def test_unbalanced_transaction_writes_nothing(
    session: AsyncSession,
    cash_account: LedgerAccount,
    open_bets_account: LedgerAccount,
) -> None:
    """Rejection happens before any ORM object exists.

    Critical: if an unbalanced entry were added to the session and merely not
    flushed, an unrelated later ``commit()`` would silently persist it. This
    asserts the session is genuinely clean.
    """
    with pytest.raises(UnbalancedTransactionError):
        await record_transaction(
            session,
            [
                TransactionLeg(account_id=cash_account.id, amount_paise=-1_000),
                TransactionLeg(account_id=open_bets_account.id, amount_paise=1),
            ],
        )

    assert not session.new
    await session.commit()
    count = await session.scalar(select(func.count()).select_from(LedgerEntry))
    assert int(count or 0) == 0


@pytest.mark.parametrize("amounts", [(-5_000,), ()])
async def test_fewer_than_two_legs_is_refused(
    session: AsyncSession, cash_account: LedgerAccount, amounts: tuple[int, ...]
) -> None:
    legs = [
        TransactionLeg(account_id=cash_account.id, amount_paise=amount)
        for amount in amounts
    ]
    with pytest.raises(ValueError, match="at least two legs"):
        await record_transaction(session, legs)


async def test_zero_amount_leg_is_refused_at_construction(
    cash_account: LedgerAccount,
) -> None:
    with pytest.raises(ValueError, match="zero-amount leg"):
        TransactionLeg(account_id=cash_account.id, amount_paise=0)


@pytest.mark.parametrize("bad_amount", [True, 1.5, "100", None])
async def test_non_integer_amount_is_refused(
    cash_account: LedgerAccount, bad_amount: object
) -> None:
    """Money is integer paise. A float amount is a bug, caught immediately."""
    with pytest.raises(TypeError, match="amount_paise must be an int"):
        TransactionLeg(account_id=cash_account.id, amount_paise=bad_amount)  # type: ignore[arg-type]


async def test_unknown_account_raises_a_domain_error(
    session: AsyncSession, cash_account: LedgerAccount, unknown_account_id: uuid.UUID
) -> None:
    """A clear domain error, not a dialect-specific IntegrityError."""
    with pytest.raises(UnknownAccountError) as excinfo:
        await record_transaction(
            session,
            [
                TransactionLeg(account_id=cash_account.id, amount_paise=-1_000),
                TransactionLeg(account_id=unknown_account_id, amount_paise=1_000),
            ],
        )
    assert unknown_account_id in excinfo.value.account_ids


async def test_replayed_transaction_id_is_rejected(
    session: AsyncSession,
    cash_account: LedgerAccount,
    open_bets_account: LedgerAccount,
) -> None:
    """At-least-once delivery must not double-post a transaction."""
    idempotency_key = uuid.uuid4()
    legs = [
        TransactionLeg(account_id=cash_account.id, amount_paise=-2_500),
        TransactionLeg(account_id=open_bets_account.id, amount_paise=2_500),
    ]

    await record_transaction(session, legs, idempotency_key)
    await session.commit()

    with pytest.raises(DuplicateTransactionError):
        await record_transaction(session, legs, idempotency_key)

    assert await get_account_balance(session, cash_account.id) == -2_500


async def test_multi_leg_transaction_with_commission_balances(
    session: AsyncSession,
) -> None:
    """Three legs: stake out, position in, commission expensed. Still nets zero."""
    cash = await ensure_account(session, "CASH_EXCHANGE", AccountType.ASSET)
    position = await ensure_account(session, "OPEN_POSITIONS", AccountType.ASSET)
    commission = await ensure_account(session, "COMMISSION_EXPENSE", AccountType.EXPENSE)
    await session.flush()

    await record_transaction(
        session,
        [
            TransactionLeg(account_id=cash.id, amount_paise=-100_000),
            TransactionLeg(account_id=position.id, amount_paise=98_000),
            TransactionLeg(account_id=commission.id, amount_paise=2_000),
        ],
    )
    await session.commit()

    balances = await get_balances(session, [cash.id, position.id, commission.id])
    assert balances[cash.id] == -100_000
    assert balances[position.id] == 98_000
    assert balances[commission.id] == 2_000
    assert sum(balances.values()) == 0
    await assert_ledger_balanced(session)


async def test_balances_accumulate_across_transactions(
    session: AsyncSession,
    cash_account: LedgerAccount,
    open_bets_account: LedgerAccount,
) -> None:
    for amount in (1_000, 2_500, 7_500):
        await record_transaction(
            session,
            [
                TransactionLeg(account_id=cash_account.id, amount_paise=-amount),
                TransactionLeg(account_id=open_bets_account.id, amount_paise=amount),
            ],
        )
    await session.commit()

    assert await get_account_balance(session, cash_account.id) == -11_000
    assert await get_account_balance(session, open_bets_account.id) == 11_000
    await assert_ledger_balanced(session)


async def test_balance_of_untouched_account_is_zero(
    session: AsyncSession, unknown_account_id: uuid.UUID
) -> None:
    """``COALESCE`` must return 0, never ``None``."""
    balance = await get_account_balance(session, unknown_account_id)
    assert balance == 0
    assert isinstance(balance, int)


async def test_large_amounts_remain_exact(session: AsyncSession) -> None:
    """Beyond float64 integer precision, so any float path would corrupt this."""
    left = await ensure_account(session, "BIG_LEFT", AccountType.ASSET)
    right = await ensure_account(session, "BIG_RIGHT", AccountType.LIABILITY)
    await session.flush()

    huge = 9_007_199_254_740_993
    await record_transaction(
        session,
        [
            TransactionLeg(account_id=left.id, amount_paise=huge),
            TransactionLeg(account_id=right.id, amount_paise=-huge),
        ],
    )
    await session.commit()

    assert await get_account_balance(session, left.id) == huge
    assert await get_account_balance(session, right.id) == -huge


async def test_ensure_account_is_idempotent(session: AsyncSession) -> None:
    first = await ensure_account(session, "BANKROLL", AccountType.EQUITY)
    second = await ensure_account(session, "BANKROLL", AccountType.EQUITY)
    assert first.id == second.id


async def test_ensure_account_refuses_to_reinterpret_a_type(
    session: AsyncSession,
) -> None:
    """Silently changing an account's type would corrupt every report built on it."""
    await ensure_account(session, "PAYABLE", AccountType.LIABILITY)
    with pytest.raises(LedgerError, match="refusing to reinterpret"):
        await ensure_account(session, "PAYABLE", AccountType.ASSET)


async def test_assert_ledger_balanced_detects_an_out_of_band_write(
    session: AsyncSession,
    cash_account: LedgerAccount,
) -> None:
    """The global tripwire.

    Simulates a write that bypassed ``record_transaction``. The identity check
    must catch it, which is exactly the signal that should halt placement.
    """
    session.add(
        LedgerEntry(
            id=uuid.uuid4(),
            transaction_id=uuid.uuid4(),
            account_id=cash_account.id,
            amount_paise=1_234,
        )
    )
    await session.commit()

    with pytest.raises(UnbalancedTransactionError) as excinfo:
        await assert_ledger_balanced(session)
    assert excinfo.value.total_paise == 1_234
