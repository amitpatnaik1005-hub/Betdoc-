"""Fleet Command: the Universal Ingestion Matrix's control plane.

Reads (config, live health, failover plan) need a signed-in user; anything that changes what the
fleet does, or touches a credential, needs an admin. API keys are write-only: encrypted with the
vault on the way in, shown afterwards only as the hint masked at write time. New providers are
added as JSON specs (``app.adapters.ingestion.factory.ProviderSpec``) with no backend code, and can
be dry-run against a pasted sample response before they ever make a request.
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

from app.adapters.ingestion import INGESTORS
from app.adapters.ingestion.base import IngestionBatch, SourcePayload
from app.adapters.ingestion.factory import ProviderSpec, SpecMapper, default_secret_env
from app.api.deps import CurrentAdmin, DbSession, get_current_user
from app.core.celery_app import celery_app
from app.core.config import Settings
from app.core.encryption import mask_api_key
from app.core.live_odds import live_odds_keys
from app.core.security_vault import VaultCrypto
from app.models.omni_vault import OmniFleetSource
from app.schemas.omni_fleet import (
    RESERVED_IDS,
    FailoverNoteRead,
    FleetApiKeyUpdate,
    FleetDeadLetter,
    FleetGroupRead,
    FleetOverview,
    FleetRunAccepted,
    FleetSourceRead,
    FleetSourceUpdate,
    FleetStatus,
    PreviewTick,
    ProviderCreate,
    ProviderPreview,
    ProviderPreviewRequest,
    ProviderUpdate,
)
from app.services.omni_fleet import (
    FleetDeps,
    SourceState,
    celery_alive,
    dispatch_run,
    fleet_keys,
    fleet_plan,
    get_or_create_source,
    publish_schedule,
    reset_breaker,
)
from app.services.omni_normalizer import OmniNormalizer
from app.services.omni_router import PlanEntry, SourceDescriptor, load_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/omni/fleet", tags=["omni-fleet"], dependencies=[Depends(get_current_user)])

# A starting point for the "Add provider" dialog: an aggregator feed with one entry per bookmaker.
TEMPLATE_SPEC: dict[str, Any] = {
    "display_name": "My odds feed",
    "description": "Licensed partner feed: match result prices per bookmaker.",
    "base_url": "https://api.partner-feed.example",
    "auth": {"type": "header", "param": "X-API-Key"},
    "requests": [{"path": "/v1/sports/{sport}/odds", "params": {"market": "1x2", "format": "decimal"}}],
    "coverage": {"soccer_epl": "football.england.premier-league"},
    "rate_limit": {"requests_per_minute": 30, "burst": 5},
    "quota": {"remaining_header": "x-ratelimit-remaining", "used_header": None, "limit": None},
    "cost": "metered",
    "priority": 20,
    "interval_seconds": 60,
    "devig": "shin",
    "mapping": {
        "events": "$.data[*]",
        "event_id": "id",
        "home": "home_team",
        "away": "away_team",
        "commence_time": "starts_at",
        "commence_format": "iso8601",
        "books": "bookmakers[*]",
        "markets": "markets[*]",
        "market_key": "key",
        "market_values": ["1x2", "match_winner", "h2h"],
        "outcomes": "outcomes[*]",
        "outcome_name": "name",
        "price": {"path": "price", "format": "decimal"},
    },
}


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def _deps(request: Request) -> FleetDeps:
    deps: FleetDeps | None = getattr(request.app.state, "fleet_deps", None)
    if deps is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Fleet is not initialised")
    return deps


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


def _num(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _status(state: SourceState, success_rate: float | None) -> FleetStatus:
    if state.schedule == "disabled":
        return "DISABLED"
    if state.schedule == "paused":
        return "FATAL"
    if state.schedule == "needs_key":
        return "NEEDS_KEY"
    if state.breaker == "open":
        return "TRIPPED"
    if state.availability == "quota_reserve":
        return "QUOTA_RESERVE"
    last = state.metrics.get("last_status")
    if not last:
        return "IDLE"
    if last == "failed" or (success_rate is not None and success_rate < 0.8):
        return "DEGRADED"
    return "HEALTHY"


def _read(descriptor: SourceDescriptor, row: OmniFleetSource | None, state: SourceState, entry: PlanEntry, settings: Settings) -> FleetSourceRead:
    metrics, runs = state.metrics, state.runs
    success_rate = runs.count("1") / len(runs) if runs else None
    return FleetSourceRead(
        source_id=descriptor.source_id,
        display_name=descriptor.display_name,
        description=descriptor.description,
        docs_url=descriptor.docs_url,
        kind=descriptor.kind,
        cost=descriptor.cost,
        priority=descriptor.priority,
        coverage=sorted(descriptor.coverage),
        requires_api_key=descriptor.requires_api_key,
        is_enabled=row.is_enabled if row is not None else True,
        status=_status(state, success_rate),
        role=entry.role,
        availability=entry.availability,
        scope=entry.scope,
        covering=[FailoverNoteRead(group=n.group, replacing=n.replacing, reason=n.reason) for n in entry.covering],
        breaker_state=state.breaker,
        breaker_remaining_seconds=state.breaker_remaining_seconds,
        has_api_key=state.has_key,
        api_key_hint=row.api_key_hint if row is not None else None,
        key_origin=state.key_origin,  # type: ignore[arg-type]
        secret_env=descriptor.spec.secret_env(descriptor.source_id) if descriptor.spec is not None else None,
        interval_seconds=descriptor.interval_seconds,
        default_interval_seconds=descriptor.default_interval_seconds,
        rate_limit_rpm=descriptor.rate_limit.requests_per_minute,
        burst=descriptor.rate_limit.burst,
        consecutive_failures=row.consecutive_failures if row is not None else 0,
        failure_threshold=settings.OMNI_FLEET_FAILURE_THRESHOLD,
        paused_at=row.paused_at if row is not None else None,
        last_error=row.last_error if row is not None else None,
        last_attempt_at=row.last_attempt_at if row is not None else None,
        last_success_at=(row.last_success_at if row is not None else None) or _ts(metrics.get("last_success_at")),
        ping_ms=_int(metrics.get("latency_ms")),
        success_rate=success_rate,
        runs_in_window=len(runs),
        ticks_last_run=_int(metrics.get("ticks")),
        fixtures_last_run=_int(metrics.get("fixtures")),
        malformed_last_run=_int(metrics.get("malformed")),
        throttled_ms=_int(metrics.get("throttled_ms")),
        devig={str(k): int(v) for k, v in _json(metrics.get("devig"), {}).items()},
        unmapped=[str(n) for n in _json(metrics.get("unmapped"), [])],
        unmapped_count=_int(metrics.get("unmapped_count")) or 0,
        quota_remaining=_num(metrics.get("quota_remaining")),
        quota_used=_num(metrics.get("quota_used")),
        quota_limit=_num(metrics.get("quota_limit")),
        quota_fraction=state.quota_fraction,
        runner=metrics.get("runner") or None,
        spec=descriptor.spec.model_dump(mode="json") if descriptor.spec is not None else None,
    )


async def _board_cells(request: Request) -> int | None:
    with contextlib.suppress(RedisError, OSError):
        cutoff = datetime.now(UTC).timestamp() - _settings(request).LIVE_ODDS_SNAPSHOT_TTL_SECONDS
        return int(await _redis(request).zcount(live_odds_keys().board_ts, cutoff, "+inf"))
    return None


async def _overview(request: Request) -> FleetOverview:
    settings = _settings(request)
    registry, rows, states, plan, groups, redis_ok = await fleet_plan(_deps(request))
    alive = await celery_alive(_redis(request), settings)
    mode = "celery" if alive else "inprocess" if settings.OMNI_FLEET_INPROCESS_FALLBACK else "offline"
    ordered = sorted(registry.values(), key=lambda d: (d.kind != "builtin", d.priority, d.source_id))
    return FleetOverview(
        generated_at=datetime.now(UTC),
        mode=mode,
        redis_available=redis_ok,
        vault_configured=getattr(request.app.state, "vault", None) is not None,
        board_cells=await _board_cells(request),
        quota_reserve=settings.OMNI_FLEET_QUOTA_RESERVE,
        sources=[_read(d, rows.get(d.source_id), states[d.source_id], plan[d.source_id], settings) for d in ordered],
        groups=[
            FleetGroupRead(group=g.group, active=g.active, free=g.free, down=dict(g.down), failover=g.failover, uncovered=g.uncovered)
            for g in sorted(groups.values(), key=lambda g: g.group)
        ],
    )


async def _source_read(request: Request, source_id: str) -> FleetSourceRead:
    overview = await _overview(request)
    for source in overview.sources:
        if source.source_id == source_id:
            return source
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown data source '{source_id}'")


async def _descriptor(request: Request, db: DbSession, source_id: str) -> SourceDescriptor:
    registry, _ = await load_registry(db, _settings(request), _deps(request).aliases)
    descriptor = registry.get(source_id)
    if descriptor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown data source '{source_id}'")
    return descriptor


async def _after_change(request: Request, row: OmniFleetSource, descriptor: SourceDescriptor, *, reset: bool = False) -> None:
    """Mirror the new schedule to Redis at once; an operator re-enable or new key also closes the breaker."""
    await publish_schedule(_redis(request), row, descriptor, _settings(request))
    if reset:
        await reset_breaker(_deps(request), row.source_id)


# ---------------------------------------------------------------- reads
@router.get("", response_model=FleetOverview)
async def fleet_overview(request: Request) -> FleetOverview:
    return await _overview(request)


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


@router.get("/providers/template")
async def provider_template() -> dict[str, Any]:
    return TEMPLATE_SPEC


# ---------------------------------------------------------------- config providers (admin)
@router.post("/providers/preview", response_model=ProviderPreview)
async def preview_provider(payload: ProviderPreviewRequest, request: Request, admin: CurrentAdmin) -> ProviderPreview:  # noqa: ARG001 - admin gate
    """Dry-run a spec against a pasted sample response: no network, no storage."""
    spec = payload.spec
    if payload.sport not in spec.coverage:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"'{payload.sport}' is not in spec.coverage")
    aliases = _deps(request).aliases
    batch = IngestionBatch(
        source_id="preview", payloads=[SourcePayload(payload.sport, payload.sample)], fetched_at=datetime.now(UTC), latency_ms=0, requests=0, retries=0
    )
    report = OmniNormalizer(aliases).normalize(batch, mapper=SpecMapper(spec, aliases), devig_method=spec.devig)
    return ProviderPreview(
        events_seen=report.events_seen,
        events_normalized=report.events_normalized,
        malformed=report.malformed,
        unmapped=sorted(report.unmapped),
        devig=dict(report.devig_methods),
        ticks=[
            PreviewTick(
                match_id=t.match_id,
                home_team=t.home_team,
                away_team=t.away_team,
                selection=t.selection,
                odds=float(t.odds),
                true_probability=float(t.true_probability),
                home_canonical=aliases.lookup(payload.sport, t.home_team) is not None,
                away_canonical=aliases.lookup(payload.sport, t.away_team) is not None,
            )
            for t in report.ticks[:60]
        ],
    )


@router.post("/providers", response_model=FleetSourceRead, status_code=status.HTTP_201_CREATED)
async def create_provider(payload: ProviderCreate, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    source_id = payload.source_id
    if source_id in INGESTORS or source_id in RESERVED_IDS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"'{source_id}' is reserved")
    if await db.get(OmniFleetSource, source_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A provider '{source_id}' already exists")
    row = OmniFleetSource(source_id=source_id, spec=payload.spec.model_dump(mode="json"), is_enabled=payload.is_enabled, consecutive_failures=0)
    db.add(row)
    await db.commit()
    descriptor = await _descriptor(request, db, source_id)
    await _after_change(request, row, descriptor)
    logger.info("Fleet provider %s created by %s (enabled=%s)", source_id, admin.username, payload.is_enabled)
    return await _source_read(request, source_id)


@router.put("/providers/{source_id}", response_model=FleetSourceRead)
async def update_provider(source_id: str, payload: ProviderUpdate, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    row = await db.get(OmniFleetSource, source_id)
    if row is None or row.spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No config provider '{source_id}'")
    row.spec = payload.spec.model_dump(mode="json")
    row.consecutive_failures, row.paused_at, row.last_error = 0, None, None  # a new mapping deserves a fresh trial
    await db.commit()
    descriptor = await _descriptor(request, db, source_id)
    await _after_change(request, row, descriptor, reset=True)
    logger.info("Fleet provider %s spec replaced by %s", source_id, admin.username)
    return await _source_read(request, source_id)


@router.delete("/providers/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(source_id: str, request: Request, db: DbSession, admin: CurrentAdmin) -> None:
    row = await db.get(OmniFleetSource, source_id)
    if row is None or row.spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No config provider '{source_id}' (built-ins can only be disabled)")
    await db.delete(row)
    await db.commit()
    keys = fleet_keys(_settings(request))
    with contextlib.suppress(RedisError, OSError):
        await _redis(request).delete(keys.fleet_metrics(source_id), keys.fleet_runs(source_id), keys.breaker_open(source_id), keys.breaker_half_open(source_id))
    logger.info("Fleet provider %s deleted by %s", source_id, admin.username)


# ---------------------------------------------------------------- per-source writes (admin)
@router.put("/{source_id}", response_model=FleetSourceRead)
async def update_source(source_id: str, payload: FleetSourceUpdate, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    descriptor = await _descriptor(request, db, source_id)
    row = await get_or_create_source(db, source_id)
    reset = False
    if payload.is_enabled is not None:
        row.is_enabled = payload.is_enabled
        if payload.is_enabled:
            # Re-enabling is the operator's acknowledgement of a dead-lettered or tripped source
            row.paused_at, row.consecutive_failures, row.last_error = None, 0, None
            reset = True
    if "interval_seconds" in payload.model_fields_set:
        row.interval_seconds = payload.interval_seconds
    await db.commit()
    descriptor = await _descriptor(request, db, source_id)
    await _after_change(request, row, descriptor, reset=reset)
    logger.info("Fleet %s updated by %s: enabled=%s interval=%s", source_id, admin.username, row.is_enabled, row.interval_seconds)
    return await _source_read(request, source_id)


@router.put("/{source_id}/api-key", response_model=FleetSourceRead)
async def set_api_key(source_id: str, payload: FleetApiKeyUpdate, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    descriptor = await _descriptor(request, db, source_id)
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
    await _after_change(request, row, descriptor, reset=True)
    logger.info("Fleet %s API key replaced by %s", source_id, admin.username)
    return await _source_read(request, source_id)


@router.delete("/{source_id}/api-key", response_model=FleetSourceRead)
async def clear_api_key(source_id: str, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetSourceRead:
    descriptor = await _descriptor(request, db, source_id)
    row = await get_or_create_source(db, source_id)
    row.encrypted_api_key, row.api_key_hint = None, None
    await db.commit()
    await _after_change(request, row, descriptor)
    logger.info("Fleet %s API key removed by %s", source_id, admin.username)
    return await _source_read(request, source_id)


@router.post("/{source_id}/run", response_model=FleetRunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def run_now(source_id: str, request: Request, db: DbSession, admin: CurrentAdmin) -> FleetRunAccepted:  # noqa: ARG001 - admin gate
    await _descriptor(request, db, source_id)
    dispatched = await dispatch_run(source_id, _deps(request), celery_app.send_task)
    return FleetRunAccepted(source_id=source_id, dispatched_to=dispatched)  # type: ignore[arg-type]


__all__ = ["TEMPLATE_SPEC", "default_secret_env", "router"]

# Every literal the template uses must stay a valid spec
ProviderSpec.model_validate(TEMPLATE_SPEC)
