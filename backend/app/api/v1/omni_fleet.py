"""Fleet Command: the ingestion fleet's control plane.

Reads (config + live health) need a signed-in user; anything that changes what the fleet does,
or touches a credential, needs an admin. API keys are write-only: encrypted with the vault on the
way in, shown afterwards only as the hint masked at write time.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from app.adapters.ingestion import INGESTORS, BaseDataIngestor
from app.api.deps import CurrentAdmin, DbSession, get_current_user
from app.core.celery_app import celery_app
from app.core.config import Settings
from app.core.encryption import mask_api_key
from app.core.live_odds import live_odds_keys
from app.core.security_vault import VaultCrypto
from app.models.omni_vault import OmniFleetSource
from app.schemas.omni_fleet import (
    FleetApiKeyUpdate,
    FleetDeadLetter,
    FleetOverview,
    FleetRunAccepted,
    FleetSourceRead,
    FleetSourceUpdate,
    FleetStatus,
)
from app.services.omni_fleet import (
    FleetDeps,
    default_interval,
    dispatch_run,
    effective_interval,
    fleet_keys,
    get_or_create_source,
    key_origin,
    publish_schedule,
    schedule_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/omni/fleet", tags=["omni-fleet"], dependencies=[Depends(get_current_user)])


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def _ingestor(source_id: str) -> type[BaseDataIngestor]:
    ingestor = INGESTORS.get(source_id)
    if ingestor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown data source '{source_id}'")
    return ingestor


def _ts(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None


def _int(value: object) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _status(state: str, metrics: dict[str, str], success_rate: float | None) -> FleetStatus:
    if state == "disabled":
        return "DISABLED"
    if state == "paused":
        return "FATAL"
    if state == "needs_key":
        return "NEEDS_KEY"
    last = metrics.get("last_status")
    if not last:
        return "IDLE"
    if last == "failed" or (success_rate is not None and success_rate < 0.8):
        return "DEGRADED"
    return "HEALTHY"


def _read(row: OmniFleetSource, settings: Settings, metrics: dict[str, str], runs: list[str]) -> FleetSourceRead:
    ingestor = INGESTORS[row.source_id]
    state = schedule_state(row, settings)
    success_rate = runs.count("1") / len(runs) if runs else None
    try:
        unmapped = [str(n) for n in json.loads(metrics.get("unmapped") or "[]")]
    except (TypeError, ValueError):
        unmapped = []
    quota = metrics.get("quota_remaining")
    return FleetSourceRead(
        source_id=row.source_id,
        display_name=ingestor.display_name,
        description=ingestor.description,
        docs_url=ingestor.docs_url,
        requires_api_key=ingestor.requires_api_key,
        is_enabled=row.is_enabled,
        status=_status(state, metrics, success_rate),
        has_api_key=key_origin(row, settings) is not None,
        api_key_hint=row.api_key_hint,
        key_origin=key_origin(row, settings),  # type: ignore[arg-type]
        interval_seconds=effective_interval(row, settings),
        default_interval_seconds=default_interval(row.source_id, settings),
        consecutive_failures=row.consecutive_failures or 0,
        failure_threshold=settings.OMNI_FLEET_FAILURE_THRESHOLD,
        paused_at=row.paused_at,
        last_error=row.last_error,
        last_attempt_at=row.last_attempt_at,
        last_success_at=row.last_success_at or _ts(metrics.get("last_success_at")),
        ping_ms=_int(metrics.get("latency_ms")),
        success_rate=success_rate,
        runs_in_window=len(runs),
        ticks_last_run=_int(metrics.get("ticks")),
        fixtures_last_run=_int(metrics.get("fixtures")),
        unmapped=unmapped,
        unmapped_count=_int(metrics.get("unmapped_count")) or 0,
        quota_remaining=float(quota) if quota not in (None, "") else None,
        runner=metrics.get("runner") or None,
    )


async def _live_state(redis: Redis, settings: Settings, source_ids: list[str]) -> tuple[bool, bool, int | None, dict[str, tuple[dict[str, str], list[str]]]]:
    """(redis_available, celery_alive, board_cells, per-source (metrics, runs)) in one round trip."""
    keys = fleet_keys(settings)
    board = live_odds_keys()
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.exists(keys.fleet_heartbeat())
        pipe.zcount(board.board_ts, datetime.now(UTC).timestamp() - settings.LIVE_ODDS_SNAPSHOT_TTL_SECONDS, "+inf")
        for source_id in source_ids:
            pipe.hgetall(keys.fleet_metrics(source_id))
            pipe.lrange(keys.fleet_runs(source_id), 0, -1)
        results: list[Any] = await pipe.execute()
    except (RedisError, OSError):
        return False, False, None, {sid: ({}, []) for sid in source_ids}
    per_source = {sid: (results[2 + 2 * i] or {}, results[3 + 2 * i] or []) for i, sid in enumerate(source_ids)}
    return True, bool(results[0]), int(results[1]), per_source


async def _source_read(request: Request, row: OmniFleetSource) -> FleetSourceRead:
    _, _, _, live = await _live_state(_redis(request), _settings(request), [row.source_id])
    metrics, runs = live[row.source_id]
    return _read(row, _settings(request), metrics, runs)


async def _after_change(request: Request, row: OmniFleetSource) -> None:
    """Push the new schedule state to Redis so the beat tick and the fallback see it at once."""
    with contextlib.suppress(RedisError, OSError):
        await publish_schedule(_redis(request), row, _settings(request))
        if row.is_enabled and row.paused_at is None:
            await _redis(request).hset(fleet_keys(_settings(request)).fleet_metrics(row.source_id), "consecutive_failures", 0)


# ---------------------------------------------------------------- reads
@router.get("", response_model=FleetOverview)
async def fleet_overview(request: Request, db: DbSession) -> FleetOverview:
    settings = _settings(request)
    stored = {r.source_id: r for r in (await db.execute(select(OmniFleetSource))).scalars()}
    # Sources never run yet are shown with their defaults (not persisted by a read)
    rows = [stored.get(sid) or OmniFleetSource(source_id=sid, is_enabled=True, consecutive_failures=0) for sid in INGESTORS]
    redis_ok, alive, board_cells, live = await _live_state(_redis(request), settings, list(INGESTORS))
    mode = "celery" if alive else "inprocess" if settings.OMNI_FLEET_INPROCESS_FALLBACK and redis_ok else "offline"
    return FleetOverview(
        generated_at=datetime.now(UTC),
        mode=mode,
        redis_available=redis_ok,
        vault_configured=getattr(request.app.state, "vault", None) is not None,
        board_cells=board_cells,
        sources=[_read(row, settings, *live[row.source_id]) for row in rows],
    )


@router.get("/deadletter", response_model=list[FleetDeadLetter])
async def dead_letters(request: Request, limit: int = 50) -> list[FleetDeadLetter]:
    try:
        raw: list[str] = await _redis(request).lrange(fleet_keys(_settings(request)).fleet_deadletter(), 0, max(0, min(limit, 200) - 1))
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis unavailable") from exc
    entries: list[FleetDeadLetter] = []
    for item in raw:
        with contextlib.suppress(ValidationError, ValueError):
            entries.append(FleetDeadLetter.model_validate_json(item))
    return entries


# ---------------------------------------------------------------- writes (admin)
@router.put("/{source_id}", response_model=FleetSourceRead)
async def update_source(source_id: str, payload: FleetSourceUpdate, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    _ingestor(source_id)
    row = await get_or_create_source(db, source_id)
    if payload.is_enabled is not None:
        row.is_enabled = payload.is_enabled
        if payload.is_enabled:
            # Re-enabling is the operator's acknowledgement of a dead-lettered source
            row.paused_at, row.consecutive_failures, row.last_error = None, 0, None
    if "interval_seconds" in payload.model_fields_set:
        row.interval_seconds = payload.interval_seconds
    await db.commit()
    await _after_change(request, row)
    logger.info("Fleet %s updated by %s: enabled=%s interval=%s", source_id, admin.username, row.is_enabled, row.interval_seconds)
    return await _source_read(request, row)


@router.put("/{source_id}/api-key", response_model=FleetSourceRead)
async def set_api_key(source_id: str, payload: FleetApiKeyUpdate, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    _ingestor(source_id)
    vault: VaultCrypto | None = getattr(request.app.state, "vault", None)
    if vault is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MASTER_VAULT_KEY is not configured")
    plain = payload.api_key.get_secret_value().strip()
    row = await get_or_create_source(db, source_id)
    row.encrypted_api_key = vault.encrypt_key(plain)
    row.api_key_hint = mask_api_key(plain, _settings(request).mask_visible_chars)[-24:]
    # A new key is a fix for an auth failure: give the source a clean slate
    row.paused_at, row.consecutive_failures, row.last_error = None, 0, None
    plain = ""
    await db.commit()
    await _after_change(request, row)
    logger.info("Fleet %s API key replaced by %s", source_id, admin.username)
    return await _source_read(request, row)


@router.delete("/{source_id}/api-key", response_model=FleetSourceRead)
async def clear_api_key(source_id: str, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    _ingestor(source_id)
    row = await get_or_create_source(db, source_id)
    row.encrypted_api_key, row.api_key_hint = None, None
    await db.commit()
    await _after_change(request, row)
    logger.info("Fleet %s API key removed by %s", source_id, admin.username)
    return await _source_read(request, row)


@router.post("/{source_id}/run", response_model=FleetRunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def run_now(source_id: str, request: Request, admin: CurrentAdmin) -> FleetRunAccepted:  # noqa: ARG001 - admin gate
    _ingestor(source_id)
    deps: FleetDeps | None = getattr(request.app.state, "fleet_deps", None)
    if deps is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Fleet is not initialised")
    dispatched = await dispatch_run(source_id, deps, celery_app.send_task)
    return FleetRunAccepted(source_id=source_id, dispatched_to=dispatched)  # type: ignore[arg-type]
