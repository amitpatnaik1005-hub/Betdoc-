from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.security import get_password_hash, verify_password
from app.models import RiskMandate, User
from app.schemas.user import UserCreate

# FIX: Aligned backend mandate defaults to the frontend UI
DEFAULT_MAX_STAKE_PER_BET = Decimal("50000")
DEFAULT_MAX_DAILY_EXPOSURE = Decimal("250000")
DEFAULT_KILL_THRESHOLD_PCT = Decimal("15")

# Hashed once at import. Checked against when the username doesn't exist.
_DUMMY_HASH = get_password_hash("betdoc-timing-equalizer-not-a-real-password")


class UsernameTakenError(ValueError):
    """ValueError subclass for duplicate usernames."""


def _normalize(username: str) -> str:
    return username.strip().lower()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == _normalize(username)))
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, user_in: UserCreate) -> User:
    if await get_user_by_username(db, user_in.username) is not None:
        raise UsernameTakenError("Username already exists")

    # FIX: Use run_in_threadpool so bcrypt hashing doesn't block the async event loop
    hashed_pwd = await run_in_threadpool(get_password_hash, user_in.password)

    user = User(
        username=_normalize(user_in.username),
        hashed_password=hashed_pwd,
    )

    # ATOMIC MANDATE
    mandate = RiskMandate(
        user=user,
        max_stake_per_bet=DEFAULT_MAX_STAKE_PER_BET,
        max_daily_exposure=DEFAULT_MAX_DAILY_EXPOSURE,
        kill_threshold_pct=DEFAULT_KILL_THRESHOLD_PCT,
    )

    db.add_all([user, mandate])

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise UsernameTakenError("Username already exists") from None

    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, username: str, password: str) -> User | None:
    user = await get_user_by_username(db, username)

    if user is None:
        # Equalize response time with threadpool
        await run_in_threadpool(verify_password, password, _DUMMY_HASH)
        return None

    # FIX: Async verify to prevent event loop blocking
    is_valid = await run_in_threadpool(verify_password, password, user.hashed_password)
    
    if not is_valid:
        return None

    if not user.is_active:
        return None

    return user
