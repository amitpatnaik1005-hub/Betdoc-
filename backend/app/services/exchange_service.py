from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_api_key
from app.models import ExchangeAccount
from app.schemas.exchange import ExchangeAccountCreate


async def create_exchange_account(
    db: AsyncSession, user_id: UUID, account_in: ExchangeAccountCreate
) -> ExchangeAccount:
    account = ExchangeAccount(
        user_id=user_id,
        exchange_name=account_in.exchange_name,
        api_key_encrypted=encrypt_api_key(account_in.api_key),
        api_secret_encrypted=encrypt_api_key(account_in.api_secret),
        is_active=True,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def get_user_exchanges(db: AsyncSession, user_id: UUID) -> Sequence[ExchangeAccount]:
    result = await db.execute(
        select(ExchangeAccount)
        .where(ExchangeAccount.user_id == user_id)
        .order_by(ExchangeAccount.exchange_name, ExchangeAccount.id)
    )
    return result.scalars().all()


async def deactivate_exchange_account(
    db: AsyncSession, account_id: UUID, user_id: UUID
) -> ExchangeAccount | None:
    # Filtering on user_id in the query itself: another user's account ID returns
    # None, exactly like an ID that doesn't exist, so account IDs can't be probed
    result = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.id == account_id,
            ExchangeAccount.user_id == user_id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        return None

    if account.is_active:
        account.is_active = False
        await db.commit()
        await db.refresh(account)

    return account
