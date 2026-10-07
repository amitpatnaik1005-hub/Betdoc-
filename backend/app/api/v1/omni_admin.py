"""Admin control plane for the Omni-Ingestion engine (token-protected)."""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.core.config import Settings
from app.core.encryption import mask_api_key
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultCrypto
from app.models.omni_vault import WS_STRATEGIES, OmniAuthStrategy, OmniHealth, OmniProviderConfig, OmniProviderEndpoint

logger = logging.getLogger(__name__)


async def require_omni_admin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    supplied = request.headers.get(settings.omni_admin_header, "")
    expected = settings.omni_admin_token.get_secret_value()
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin credentials")


router = APIRouter(prefix="/admin/omni", tags=["omni-admin"], dependencies=[Depends(require_omni_admin)])


# ---------------------------------------------------------------- schemas
class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=2048)
    http_method: Literal["GET", "POST"] = "GET"
    query_params: dict[str, str] | None = None
    request_body: dict[str, Any] | None = None
    topic: str | None = Field(default=None, max_length=128)
    min_interval_seconds: float | None = Field(default=None, gt=0.0)
    is_active: bool = True


class ProviderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_name: str = Field(min_length=1, max_length=128)
    category_code: str = Field(pattern=r"^[A-H]$")
    base_url: HttpUrl
    ws_url: AnyUrl | None = None
    auth_strategy: OmniAuthStrategy
    auth_param_name: str | None = Field(default=None, max_length=128)
    api_key: SecretStr | None = None
    ws_auth_payload: dict[str, Any] | None = None
    ws_subscribe_payloads: list[Any] | None = None
    default_headers: dict[str, str] | None = None
    requests_per_minute: int = Field(ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0.0)
    queue_name: str | None = None
    adapter_key: str | None = None
    normalization_spec: dict[str, Any] | None = None
    is_active: bool = False
    endpoints: list[EndpointCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_auth(self) -> Self:
        strategy = self.auth_strategy
        if strategy is not OmniAuthStrategy.NONE and self.api_key is None:
            raise ValueError(f"{strategy} requires api_key.")
        if strategy in (OmniAuthStrategy.HEADER, OmniAuthStrategy.QUERY) and not self.auth_param_name:
            raise ValueError(f"{strategy} requires auth_param_name.")
        if strategy in WS_STRATEGIES:
            if self.ws_url is None or self.ws_url.scheme not in ("ws", "wss"):
                raise ValueError(f"{strategy} requires a ws:// or wss:// ws_url.")
            if strategy is OmniAuthStrategy.MESSAGE and self.ws_auth_payload is None:
                raise ValueError("MESSAGE requires ws_auth_payload (use the configured key placeholder).")
        if self.default_headers and self.auth_param_name and self.auth_param_name.lower() in {
            h.lower() for h in self.default_headers
        }:
            raise ValueError("default_headers must not contain the auth header; supply it via api_key.")
        return self


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: bool


class ProviderRead(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    provider_name: str
    category_code: str
    base_url: str
    ws_url: str | None
    auth_strategy: OmniAuthStrategy
    auth_param_name: str | None
    masked_api_key: str | None
    requests_per_minute: int
    queue_name: str | None
    adapter_key: str | None
    is_active: bool
    health_status: str
    endpoint_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, p: OmniProviderConfig, endpoint_count: int) -> ProviderRead:
        return cls(
            id=p.id,
            provider_name=p.provider_name,
            category_code=p.category_code,
            base_url=p.base_url,
            ws_url=p.ws_url,
            auth_strategy=p.auth_strategy,
            auth_param_name=p.auth_param_name,
            masked_api_key=p.api_key_hint,
            requests_per_minute=p.requests_per_minute,
            queue_name=p.queue_name,
            adapter_key=p.adapter_key,
            is_active=p.is_active,
            health_status=p.health_status,
            endpoint_count=endpoint_count,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class EndpointRead(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)
    id: UUID
    provider_id: UUID
    path: str
    http_method: str
    topic: str | None
    min_interval_seconds: float | None
    is_active: bool


BreakerState = Literal["closed", "open", "half_open", "unknown"]


class ProviderHealth(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_id: UUID
    provider_name: str
    category_code: str
    is_active: bool
    health_status: str
    breaker_state: BreakerState
    recent_failures: int | None
    cooldown_remaining_seconds: float | None


class OmniHealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    generated_at: datetime
    total: int
    active: int
    degraded: int
    open_circuits: int
    redis_available: bool
    providers: list[ProviderHealth]


# ---------------------------------------------------------------- helpers
def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _vault(request: Request) -> VaultCrypto:
    vault: VaultCrypto = request.app.state.vault
    return vault


def _redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


async def _endpoint_count(session: AsyncSession, provider_id: UUID) -> int:
    stmt = select(func.count(OmniProviderEndpoint.id)).where(OmniProviderEndpoint.provider_id == provider_id)
    return int((await session.execute(stmt)).scalar_one())


def _assert_scheme(url: str, settings: Settings) -> None:
    allowed = {"https", "http"} if settings.omni_allow_insecure_http else {"https"}
    scheme = url.split(":", 1)[0].lower()
    if scheme not in allowed:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"base_url scheme must be one of {sorted(allowed)}")


# ---------------------------------------------------------------- routes
@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderRead:
    settings = _settings(request)
    base_url = str(payload.base_url).rstrip("/")
    _assert_scheme(base_url, settings)
    if payload.queue_name is not None and payload.queue_name not in settings.omni_queues:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="queue_name is not a configured queue")

    plain = payload.api_key.get_secret_value() if payload.api_key is not None else None
    provider = OmniProviderConfig(
        provider_name=payload.provider_name,
        category_code=payload.category_code,
        base_url=base_url,
        ws_url=str(payload.ws_url) if payload.ws_url is not None else None,
        auth_strategy=payload.auth_strategy,
        auth_param_name=payload.auth_param_name,
        encrypted_api_key=_vault(request).encrypt_key(plain) if plain else None,
        api_key_hint=mask_api_key(plain, settings.mask_visible_chars) if plain else None,
        ws_auth_payload=payload.ws_auth_payload,
        ws_subscribe_payloads=payload.ws_subscribe_payloads,
        default_headers=payload.default_headers,
        requests_per_minute=payload.requests_per_minute,
        timeout_seconds=payload.timeout_seconds,
        queue_name=payload.queue_name,
        adapter_key=payload.adapter_key,
        normalization_spec=payload.normalization_spec,
        is_active=payload.is_active,
        health_status=OmniHealth.UNKNOWN.value,
    )
    provider.endpoints = [OmniProviderEndpoint(**ep.model_dump()) for ep in payload.endpoints]
    plain = None
    session.add(provider)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Provider name already exists") from exc
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Failed to create provider %s", payload.provider_name)
        raise
    logger.info("Omni provider created: %s (%s)", provider.provider_name, provider.id)
    return ProviderRead.from_model(provider, len(payload.endpoints))


@router.post("/providers/{provider_id}/endpoints", response_model=EndpointRead, status_code=status.HTTP_201_CREATED)
async def add_endpoint(
    provider_id: UUID,
    payload: EndpointCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EndpointRead:
    if await session.get(OmniProviderConfig, provider_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found")
    endpoint = OmniProviderEndpoint(provider_id=provider_id, **payload.model_dump())
    session.add(endpoint)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Endpoint already exists") from exc
    return EndpointRead.model_validate(endpoint)


@router.put("/providers/{provider_id}/status", response_model=ProviderRead)
async def update_provider_status(
    provider_id: UUID,
    payload: StatusUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ProviderRead:
    provider = await session.get(OmniProviderConfig, provider_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found")
    provider.is_active = payload.is_active
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Failed to update status for provider %s", provider_id)
        raise
    logger.info("Omni provider %s set is_active=%s", provider.provider_name, payload.is_active)
    return ProviderRead.from_model(provider, await _endpoint_count(session, provider_id))


@router.get("/health", response_model=OmniHealthResponse)
async def omni_health(request: Request, session: Annotated[AsyncSession, Depends(get_session)]) -> OmniHealthResponse:
    settings = _settings(request)
    keys = OmniRedisKeys(settings.omni_redis_prefix)
    providers = (await session.execute(select(OmniProviderConfig).order_by(OmniProviderConfig.provider_name))).scalars().all()

    redis_available = True
    breaker_data: list[Any] = []
    if providers:
        try:
            pipe = _redis(request).pipeline(transaction=False)
            for p in providers:
                pipe.pttl(keys.breaker_open(p.id))
                pipe.get(keys.breaker_failures(p.id))
                pipe.exists(keys.breaker_half_open(p.id))
            breaker_data = await pipe.execute()
        except RedisError:
            logger.exception("Redis unavailable while reading circuit-breaker state.")
            redis_available = False

    entries: list[ProviderHealth] = []
    for index, p in enumerate(providers):
        state: BreakerState = "unknown"
        failures: int | None = None
        cooldown: float | None = None
        if redis_available:
            ttl_ms, raw_failures, half_open = breaker_data[index * 3 : index * 3 + 3]
            failures = int(raw_failures) if raw_failures is not None else 0
            if isinstance(ttl_ms, int) and ttl_ms > 0:
                state, cooldown = "open", ttl_ms / 1000.0
            elif half_open:
                state = "half_open"
            else:
                state = "closed"
        entries.append(
            ProviderHealth(
                provider_id=p.id,
                provider_name=p.provider_name,
                category_code=p.category_code,
                is_active=p.is_active,
                health_status=p.health_status,
                breaker_state=state,
                recent_failures=failures,
                cooldown_remaining_seconds=cooldown,
            )
        )

    return OmniHealthResponse(
        generated_at=datetime.now(UTC),
        total=len(entries),
        active=sum(e.is_active for e in entries),
        degraded=sum(e.health_status == OmniHealth.DEGRADED.value for e in entries),
        open_circuits=sum(e.breaker_state == "open" for e in entries),
        redis_available=redis_available,
        providers=entries,
    )
