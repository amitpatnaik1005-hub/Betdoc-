import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Query, WebSocket, WebSocketException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, settings
from app.core.database import AsyncSessionLocal
from app.core.encryption import EncryptionService
from app.domain.integration.adapters import (
    KellyRiskEngine,
    PredictionModelPort,
    RiskEnginePort,
    SpreadCoverPredictionAdapter,
)
from app.domain.integration.orchestrator import OrchestratorService
from app.domain.integration.repositories import (
    ArenaRepository,
    ConstraintRepository,
    LabRepository,
    OracleRepository,
    VaultRepository,
)
from app.domain.math.models_v2.normal_distribution import NormalDistributionModel
from app.models import User

# Point this at the login route once it exists
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


class TokenPayload(BaseModel):
    sub: str


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# Name used by the Omni admin routes.
get_session = get_db


def _credentials_exception(detail: str = "Could not validate credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_user_id(token: str) -> uuid.UUID:
    """Verified subject of a bearer token; raises the standard 401 otherwise."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY.get_secret_value(),
            algorithms=[settings.ALGORITHM],  # Pin the algorithm; never trust the token header
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        # Caught first: ExpiredSignatureError is a subclass of InvalidTokenError
        raise _credentials_exception("Token has expired")
    except jwt.InvalidTokenError:
        raise _credentials_exception()

    try:
        token_data = TokenPayload.model_validate(payload)
        return uuid.UUID(token_data.sub)
    except (ValidationError, ValueError):
        raise _credentials_exception()


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user_id = _decode_user_id(token)

    user = await db.get(User, user_id)
    if user is None:
        # Same response as a bad token, so the API doesn't reveal which accounts exist
        raise _credentials_exception()

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user account")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_ws_user(websocket: WebSocket, token: Annotated[str | None, Query()] = None) -> User:
    """WebSocket twin of get_current_user. Browsers can't set headers on a WS handshake, so the
    JWT rides in ?token=, and since WebSockets bypass CORS the Origin is checked explicitly.
    Failing before accept() rejects the handshake (the client sees HTTP 403)."""
    origin = websocket.headers.get("origin")
    if origin and origin not in settings.BACKEND_CORS_ORIGINS:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Origin not allowed")
    if not token:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
    try:
        user_id = _decode_user_id(token)
    except HTTPException:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token") from None
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
    return user


WsUser = Annotated[User, Depends(get_ws_user)]


ADMIN_ROLE = "ADMIN"

def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != ADMIN_ROLE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]


# --------------------------------------------------------------------------- integration
SPREAD_MODEL_NAME = "v2.normal_distribution.spread_cover"


def build_orchestrator(
    *,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    encryption: EncryptionService,
    settings: Settings,
    vault: VaultRepository | None = None,
    oracle: OracleRepository | None = None,
    arena: ArenaRepository | None = None,
    lab: LabRepository | None = None,
    constraints: ConstraintRepository | None = None,
    prediction_model: PredictionModelPort | None = None,
    risk_engine: RiskEnginePort | None = None,
) -> OrchestratorService:
    """Compose the cross-section orchestrator. Every collaborator can be swapped (tests, fault injection)."""
    return OrchestratorService(
        session,
        encryption,
        session_factory=session_factory,
        settings=settings,
        vault=vault or VaultRepository(),
        oracle=oracle or OracleRepository(),
        arena=arena or ArenaRepository(),
        lab=lab or LabRepository(),
        constraints=constraints or ConstraintRepository(),
        prediction_model=prediction_model or SpreadCoverPredictionAdapter(NormalDistributionModel(), SPREAD_MODEL_NAME),
        risk_engine=risk_engine
        or KellyRiskEngine(
            kelly_fraction=settings.kelly_fraction,
            max_stake_fraction=settings.max_stake_fraction,
            max_open_exposure_fraction=settings.max_open_exposure_fraction,
            min_edge=settings.min_edge,
        ),
    )


def get_encryption_service() -> EncryptionService:
    return EncryptionService(settings.ENCRYPTION_KEY.get_secret_value())


def get_orchestrator(
    db: Annotated[AsyncSession, Depends(get_db)],
    encryption: Annotated[EncryptionService, Depends(get_encryption_service)],
) -> OrchestratorService:
    return build_orchestrator(session=db, session_factory=AsyncSessionLocal, encryption=encryption, settings=settings)
