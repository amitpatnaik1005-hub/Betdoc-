from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.exchange import ExchangeAccountCreate, ExchangeAccountRead
from app.services import exchange_service

router = APIRouter(tags=["exchanges"])


# Path "" (not "/") serves /api/v1/exchanges directly. A trailing-slash redirect
# can drop the Authorization header in some clients.
# response_model on every route is a second leak guard: only ExchangeAccountRead
# fields are ever serialized, whatever object is returned.
@router.post("", response_model=ExchangeAccountRead, status_code=status.HTTP_201_CREATED)
async def create_exchange_account(
    account_in: ExchangeAccountCreate,
    current_user: CurrentUser,
    db: DbSession,
) -> ExchangeAccountRead:
    account = await exchange_service.create_exchange_account(db, current_user.id, account_in)
    return ExchangeAccountRead.model_validate(account)


@router.get("", response_model=list[ExchangeAccountRead])
async def list_exchange_accounts(
    current_user: CurrentUser,
    db: DbSession,
) -> list[ExchangeAccountRead]:
    accounts = await exchange_service.get_user_exchanges(db, current_user.id)
    return [ExchangeAccountRead.model_validate(a) for a in accounts]


@router.patch("/{account_id}/deactivate", response_model=ExchangeAccountRead)
async def deactivate_exchange_account(
    account_id: UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> ExchangeAccountRead:
    account = await exchange_service.deactivate_exchange_account(db, account_id, current_user.id)
    if account is None:
        # 404 for both "doesn't exist" and "belongs to someone else"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exchange account not found")
    return ExchangeAccountRead.model_validate(account)
