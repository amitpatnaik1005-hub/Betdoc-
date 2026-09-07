"""Schema-level guarantees: composite keys, timezones, money, constraints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from betdoc.adapters.persistence.models import (
    AccountType,
    BetRecord,
    BetStatus,
    LedgerAccount,
    LedgerEntry,
    OddsTick,
    utc_now,
)

pytestmark = pytest.mark.asyncio


async def test_odds_tick_primary_key_is_composite() -> None:
    """TimescaleDB refuses a hypertable whose unique index omits the time column."""
    key_columns = {column.name for column in OddsTick.__table__.primary_key.columns}
    assert key_columns == {"id", "timestamp"}
    assert OddsTick.__table__.c.id.primary_key is True
    assert OddsTick.__table__.c.timestamp.primary_key is True


async def test_same_tick_id_persists_at_two_timestamps(session: AsyncSession) -> None:
    """Direct consequence of the composite key: id alone is not unique."""
    shared_id = uuid.uuid4()
    base = utc_now()

    session.add_all(
        [
            OddsTick(
                id=shared_id,
                timestamp=base,
                bookmaker="pinnacle",
                event_id="evt-1",
                market_key="h2h",
                outcome_key="home",
                decimal_odds=2.10,
            ),
            OddsTick(
                id=shared_id,
                timestamp=base + timedelta(seconds=1),
                bookmaker="pinnacle",
                event_id="evt-1",
                market_key="h2h",
                outcome_key="home",
                decimal_odds=2.12,
            ),
        ]
    )
    await session.commit()

    rows = (
        await session.scalars(select(OddsTick).where(OddsTick.id == shared_id))
    ).all()
    assert len(rows) == 2


async def test_natural_key_duplicate_is_rejected(session: AsyncSession) -> None:
    """Makes concurrent ingestion idempotent instead of duplicating history."""
    stamp = utc_now()
    payload = {
        "bookmaker": "pinnacle",
        "event_id": "evt-2",
        "market_key": "h2h",
        "outcome_key": "home",
        "decimal_odds": 2.05,
        "timestamp": stamp,
    }
    session.add(OddsTick(id=uuid.uuid4(), **payload))
    await session.commit()

    session.add(OddsTick(id=uuid.uuid4(), **payload))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_timestamps_are_timezone_aware_after_round_trip(
    session: AsyncSession,
) -> None:
    """The guarantee ``DateTime(timezone=True)`` alone does not give on SQLite.

    ``UtcDateTime`` re-attaches UTC on read, so no naive datetime can escape
    the ORM boundary on any backend.
    """
    exact = datetime(2026, 3, 1, 18, 30, tzinfo=UTC)
    session.add(
        OddsTick(
            id=uuid.uuid4(),
            timestamp=exact,
            bookmaker="stake",
            event_id="evt-3",
            market_key="h2h",
            outcome_key="away",
            decimal_odds=3.40,
        )
    )
    await session.commit()
    session.expunge_all()

    loaded = await session.scalar(select(OddsTick).where(OddsTick.event_id == "evt-3"))
    assert loaded is not None
    assert loaded.timestamp.tzinfo is not None
    assert loaded.timestamp.utcoffset() == timedelta(0)
    assert loaded.timestamp == exact


async def test_naive_datetime_is_refused(session: AsyncSession) -> None:
    """Fail at the boundary, not silently at 3am six months later."""
    session.add(
        OddsTick(
            id=uuid.uuid4(),
            timestamp=datetime(2026, 3, 1, 18, 30),  # noqa: DTZ001 - deliberate
            bookmaker="stake",
            event_id="evt-naive",
            market_key="h2h",
            outcome_key="home",
            decimal_odds=2.0,
        )
    )
    with pytest.raises(StatementError, match="naive datetime rejected"):
        await session.flush()
    await session.rollback()


async def test_uuid_primary_keys_round_trip_as_uuid_objects(
    session: AsyncSession,
) -> None:
    """The generic ``Uuid`` type must yield real UUIDs, not hex strings."""
    bet_id = uuid.uuid4()
    session.add(
        BetRecord(
            id=bet_id,
            event_id="evt-4",
            market_key="h2h",
            outcome_key="home",
            bookmaker="pinnacle",
            odds=1.95,
            stake_paise=500_000,
            status=BetStatus.PENDING,
            placed_at=utc_now(),
        )
    )
    await session.commit()
    session.expunge_all()

    loaded = await session.get(BetRecord, bet_id)
    assert loaded is not None
    assert isinstance(loaded.id, uuid.UUID)
    assert loaded.id == bet_id


async def test_money_is_stored_as_exact_integer_paise(session: AsyncSession) -> None:
    """A stake beyond float64's integer-exact range must survive unchanged."""
    huge = 9_007_199_254_740_993  # 2**53 + 1
    bet = BetRecord(
        id=uuid.uuid4(),
        event_id="evt-5",
        market_key="h2h",
        outcome_key="home",
        bookmaker="pinnacle",
        odds=2.0,
        stake_paise=huge,
        status=BetStatus.PENDING,
        placed_at=utc_now(),
    )
    session.add(bet)
    await session.commit()
    session.expunge_all()

    loaded = await session.get(BetRecord, bet.id)
    assert loaded is not None
    assert loaded.stake_paise == huge
    assert isinstance(loaded.stake_paise, int)


@pytest.mark.parametrize("status", list(BetStatus))
async def test_every_bet_status_persists_by_value(
    session: AsyncSession, status: BetStatus
) -> None:
    """Enum stored as its lowercase value, not the Python member name."""
    settled = None if status is BetStatus.PENDING else utc_now()
    bet = BetRecord(
        id=uuid.uuid4(),
        event_id=f"evt-{status.value}",
        market_key="h2h",
        outcome_key="home",
        bookmaker="pinnacle",
        odds=2.0,
        stake_paise=100_000,
        status=status,
        placed_at=utc_now(),
        settled_at=settled,
    )
    session.add(bet)
    await session.commit()
    session.expunge_all()

    loaded = await session.get(BetRecord, bet.id)
    assert loaded is not None
    assert loaded.status is status


@pytest.mark.parametrize(
    ("stake_paise", "odds"),
    [(0, 2.0), (-100, 2.0), (100, 1.0), (100, 0.5)],
)
async def test_bet_check_constraints_reject_impossible_values(
    session: AsyncSession, stake_paise: int, odds: float
) -> None:
    session.add(
        BetRecord(
            id=uuid.uuid4(),
            event_id="evt-bad",
            market_key="h2h",
            outcome_key="home",
            bookmaker="pinnacle",
            odds=odds,
            stake_paise=stake_paise,
            status=BetStatus.PENDING,
            placed_at=utc_now(),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_settlement_coherence_constraint(session: AsyncSession) -> None:
    """A terminal status without a settled_at is a data bug, blocked in DDL."""
    session.add(
        BetRecord(
            id=uuid.uuid4(),
            event_id="evt-incoherent",
            market_key="h2h",
            outcome_key="home",
            bookmaker="pinnacle",
            odds=2.0,
            stake_paise=100_000,
            status=BetStatus.WON,
            placed_at=utc_now(),
            settled_at=None,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_account_name_is_unique(session: AsyncSession) -> None:
    session.add(LedgerAccount(name="CASH", account_type=AccountType.ASSET))
    await session.commit()

    session.add(LedgerAccount(name="CASH", account_type=AccountType.ASSET))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_zero_amount_ledger_entry_is_rejected_by_the_database(
    session: AsyncSession, cash_account: LedgerAccount
) -> None:
    """Belt and braces: the ledger module refuses it, and so does the schema."""
    session.add(
        LedgerEntry(
            id=uuid.uuid4(),
            transaction_id=uuid.uuid4(),
            account_id=cash_account.id,
            amount_paise=0,
            timestamp=utc_now(),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_ledger_entry_requires_an_existing_account(
    session: AsyncSession, unknown_account_id: uuid.UUID, foreign_keys_enabled: bool
) -> None:
    assert foreign_keys_enabled, "the FK pragma fixture did not apply"
    session.add(
        LedgerEntry(
            id=uuid.uuid4(),
            transaction_id=uuid.uuid4(),
            account_id=unknown_account_id,
            amount_paise=1_000,
            timestamp=utc_now(),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.parametrize(
    ("account_type", "expected_sign"),
    [
        (AccountType.ASSET, 1),
        (AccountType.EXPENSE, 1),
        (AccountType.LIABILITY, -1),
        (AccountType.EQUITY, -1),
        (AccountType.REVENUE, -1),
    ],
)
def test_normal_balance_signs(account_type: AccountType, expected_sign: int) -> None:
    assert account_type.normal_balance_sign == expected_sign


@pytest.mark.parametrize(
    ("status", "win", "refund"),
    [
        (BetStatus.WON, 1.0, 0.0),
        (BetStatus.HALF_WON, 0.5, 0.5),
        (BetStatus.VOID, 0.0, 1.0),
        (BetStatus.HALF_LOST, 0.0, 0.5),
        (BetStatus.LOST, 0.0, 0.0),
    ],
)
def test_settlement_fractions(status: BetStatus, win: float, refund: float) -> None:
    assert status.win_fraction == win
    assert status.stake_refund_fraction == refund
