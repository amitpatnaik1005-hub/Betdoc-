"""Omni-Sniper API under ``/api/v1/omni``: venues (reads: user, writes: admin), the execution
terminal's history, executions with their raw payloads, and the dead-letter queue."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.adapters.execution.factory import VenueUnavailableError
from app.adapters.execution.venue import VenueConfig
from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import AppSettings, SessionFactory
from app.core.security_vault import UnsafeTargetError, assert_public_target
from app.models.cfo_vault import AuditEvent, AuditLog, LedgerStatus, PhantomLedger
from app.models.execution import EntityMapping, ExecutionVenue
from app.schemas.cfo_vault import PositionRead
from app.schemas.sniper import (
    CatalogSyncRead,
    ExecutionRead,
    FeedLine,
    MappingWrite,
    ResolveRequest,
    VenueCredentials,
    VenueRead,
    VenueSession,
    VenueUpsert,
)
from app.services.cfo_ledger import CfoError, apply_streak, resolve_manually
from app.services.order_resolver import OrderResolver
from app.services.sniper import SniperGateway
from app.services.sniper_runtime import SniperRuntime

router = APIRouter(prefix="/omni", tags=["sniper"])

EXECUTION_EVENTS = (AuditEvent.EXECUTED, AuditEvent.BOOKMAKER_REJECTED, AuditEvent.EXECUTION_UNKNOWN, AuditEvent.COMMIT_FAILED)


def _gateway(request: Request) -> SniperGateway:
    runtime: SniperRuntime | None = getattr(request.app.state, "sniper", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "SNIPER_UNAVAILABLE", "message": "The Omni-Sniper is not running"})
    return runtime.gateway


def _http(exc: CfoError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"reason": exc.reason, "message": exc.message, **exc.detail})


def _mask(value: str) -> str:
    return f"…{value[-4:]}" if len(value) > 6 else "…"


# ---------------------------------------------------------------- venues
@router.get("/venues", response_model=list[VenueRead])
async def venues(request: Request, user: CurrentUser, sessions: SessionFactory) -> list[VenueRead]:  # noqa: ARG001 - auth gate
    gateway = _gateway(request)
    async with sessions() as session:
        rows = (await session.execute(select(ExecutionVenue).order_by(ExecutionVenue.is_sandbox, ExecutionVenue.id))).scalars().all()
        counts = dict(
            (await session.execute(select(EntityMapping.venue_id, func.count()).where(EntityMapping.kind == "fixture").group_by(EntityMapping.venue_id))).all()
        )
    out: list[VenueRead] = []
    for row in rows:
        config = VenueConfig.from_row(row)
        out.append(
            VenueRead(
                id=row.id,
                display_name=row.display_name,
                adapter=row.adapter,
                base_url="in-process sandbox" if row.is_sandbox else row.base_url,
                auth_type=row.auth_type,
                bets_per_second=row.bets_per_second,
                burst=row.burst,
                routes=list(row.routes or []),
                is_enabled=row.is_enabled,
                is_sandbox=row.is_sandbox,
                has_credentials=bool(row.encrypted_credentials),
                credentials_hint=row.credentials_hint,
                fixtures_mapped=int(counts.get(row.id, 0)),
                session=VenueSession(**await gateway.sessions.describe(config)),
            )
        )
    return out


@router.post("/venues", response_model=VenueRead, status_code=status.HTTP_201_CREATED)
async def upsert_venue(payload: VenueUpsert, request: Request, admin: CurrentAdmin, sessions: SessionFactory, settings: AppSettings) -> VenueRead:  # noqa: ARG001
    try:  # orders carry money: a venue must be a public https host (DNS checked off the event loop)
        await asyncio.to_thread(assert_public_target, payload.base_url, allowed_schemes=frozenset({"https"}), allow_private=settings.omni_allow_private_networks)
    except UnsafeTargetError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "UNSAFE_VENUE_URL", "message": str(exc)}) from exc
    async with sessions() as session:
        row = await session.get(ExecutionVenue, payload.id)
        if row is None:
            row = ExecutionVenue(id=payload.id, is_sandbox=False)
            session.add(row)
        elif row.is_sandbox:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "SANDBOX_RESERVED", "message": "The sandbox venue is managed automatically"})
        for name, value in payload.model_dump().items():
            if name != "id":
                setattr(row, name, value)
        await session.commit()
    await _gateway(request).venues(fresh=True)
    return next(v for v in await venues(request, admin, sessions) if v.id == payload.id)


@router.put("/venues/{venue_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
async def set_credentials(venue_id: str, payload: VenueCredentials, request: Request, admin: CurrentAdmin, sessions: SessionFactory) -> None:  # noqa: ARG001
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_VAULT", "message": "MASTER_VAULT_KEY is not configured"})
    secret = payload.model_dump(exclude_none=True)
    try:
        async with sessions() as session:
            row = await session.get(ExecutionVenue, venue_id)
            if row is None or row.is_sandbox:
                raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NOT_FOUND", "message": "No such venue"})
            row.encrypted_credentials = vault.encrypt_key(json.dumps(secret))
            row.credentials_hint = f"client {_mask(payload.client_id)}" if payload.client_id else f"key {_mask(payload.api_key or '')}"
            await session.commit()
    finally:
        secret.clear()
    gateway = _gateway(request)
    await gateway.venues(fresh=True)
    if gateway.redis is not None:  # a cached session belongs to the old credentials
        await gateway.redis.delete(gateway.sessions.key(venue_id))


@router.post("/venues/{venue_id}/sync", response_model=CatalogSyncRead)
async def sync_catalog(venue_id: str, request: Request, admin: CurrentAdmin) -> CatalogSyncRead:  # noqa: ARG001
    gateway = _gateway(request)
    venue = next((v for v in await gateway.venues(fresh=True) if v.id == venue_id), None)
    if venue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NOT_FOUND", "message": "No such enabled venue"})
    try:
        events = await gateway.adapter(venue).fetch_events()
    except VenueUnavailableError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"reason": "VENUE_UNAVAILABLE", "message": str(exc)}) from exc
    report = await gateway.mapper.sync_catalog(venue, events)
    return CatalogSyncRead(venue_id=venue.id, events=report.events, mapped=report.mapped, unresolved=report.unresolved[:50])


@router.put("/venues/{venue_id}/mappings", status_code=status.HTTP_204_NO_CONTENT)
async def write_mappings(venue_id: str, rows: list[MappingWrite], request: Request, admin: CurrentAdmin, sessions: SessionFactory) -> None:  # noqa: ARG001
    if len(rows) > 1000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "TOO_MANY", "message": "At most 1000 mappings per call"})
    async with sessions() as session:
        if await session.get(ExecutionVenue, venue_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NOT_FOUND", "message": "No such venue"})
    await _gateway(request).mapper.upsert(venue_id, [(r.kind, r.canonical_key, r.remote_id, "manual") for r in rows])


# ---------------------------------------------------------------- the terminal
@router.get("/sniper/feed", response_model=list[FeedLine])
async def feed(request: Request, user: CurrentUser) -> list[FeedLine]:
    lines = await _gateway(request).feed.history(user.id)
    return [FeedLine.model_validate(line) for line in lines]


@router.get("/executions", response_model=list[ExecutionRead])
async def executions(user: CurrentUser, sessions: SessionFactory, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[ExecutionRead]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.user_id == user.id, AuditLog.event.in_(EXECUTION_EVENTS)).order_by(AuditLog.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    out: list[ExecutionRead] = []
    for row in rows:
        detail = row.detail or {}
        out.append(
            ExecutionRead(
                id=row.id,
                event=row.event,
                reason=row.reason,
                idempotency_key=row.idempotency_key,
                ledger_id=row.ledger_id,
                fixture_id=row.fixture_id,
                selection=row.selection,
                stake_inr=row.stake_inr,
                odds=row.odds,
                created_at=row.created_at,
                venue_id=detail.get("venue_id"),
                remote_bet_id=detail.get("remote_bet_id"),
                http_status=detail.get("http_status"),
                latency_ms=detail.get("latency_ms"),
                matched_odds=detail.get("matched_odds"),
                request_payload=detail.get("request_payload"),
                response_payload=detail.get("response_payload"),
            )
        )
    return out


# ---------------------------------------------------------------- dead-letter queue
@router.get("/dlq", response_model=list[PositionRead])
async def dead_letters(user: CurrentUser, sessions: SessionFactory, everyone: Annotated[bool, Query(alias="all")] = False) -> list[PositionRead]:
    stmt = select(PhantomLedger).where(PhantomLedger.status == LedgerStatus.REQUIRES_MANUAL_INTERVENTION)
    if not (everyone and user.role == "ADMIN"):
        stmt = stmt.where(PhantomLedger.user_id == user.id)
    async with sessions() as session:
        rows = (await session.execute(stmt.order_by(PhantomLedger.created_at.desc()).limit(500))).scalars().all()
    return [PositionRead.model_validate(r) for r in rows]


@router.post("/positions/{ledger_id}/resolve", response_model=PositionRead)
async def resolve_position(
    ledger_id: uuid.UUID, payload: ResolveRequest, request: Request, admin: CurrentAdmin, sessions: SessionFactory, settings: AppSettings
) -> PositionRead:
    async with sessions() as session:
        try:
            entry, won = await resolve_manually(session, settings, ledger_id, outcome=payload.outcome, remote_bet_id=payload.remote_bet_id, actor=admin.id)
            await session.commit()
        except CfoError as exc:
            await session.rollback()
            raise _http(exc) from exc
        except Exception:
            await session.rollback()
            raise
        position = PositionRead.model_validate(entry)
    if won is not None:
        await apply_streak(getattr(request.app.state, "redis", None), settings, entry.user_id, [won])
    return position


@router.post("/sniper/resolve-now")
async def resolve_now(request: Request, admin: CurrentAdmin, sessions: SessionFactory, settings: AppSettings) -> dict[str, object]:  # noqa: ARG001
    """Run one order-resolution sweep here and now (the same one Celery beat runs)."""
    summary = await OrderResolver(_gateway(request), sessions, settings).run()
    return {k: getattr(summary, k) for k in summary.__dataclass_fields__}
