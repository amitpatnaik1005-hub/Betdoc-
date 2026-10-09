"""The Sentinel under ``/api/v1/sentinel`` (Group 68): alerting, liveness and remote command.

    GET  /sentinel/status                         liveness, dependency health, debouncer, kill switch, channels
    GET  /sentinel/alerts                         the alert trail, each with its deliveries
    POST /sentinel/check                          run the dependency and liveness checks now (admin)
    POST /sentinel/test-alert                     a TEST alert through the whole pipeline (admin)
    GET  /sentinel/channels                       the dispatchers: configuration, hints, last success/error (admin)
    PUT  /sentinel/channels/{channel}             enable / disable, non-secret settings (admin)
    PUT  /sentinel/channels/{channel}/credentials secrets, encrypted with the master vault key (admin)
    DELETE /sentinel/channels/{channel}/credentials
    POST /sentinel/channels/{channel}/test        send a test message on that channel alone (admin)
    GET  /sentinel/routing                        the routing matrix
    PUT  /sentinel/routing                        (admin)
    GET  /sentinel/hype                           the last 08:00 forecast (admin)
    POST /sentinel/hype/preview                   today's forecast and the line it would send, sending nothing (admin)
    GET  /sentinel/commands                       the Telegram command log (admin)
    POST /sentinel/webhook/telegram               Telegram's webhook: /halt, /resume, /status (no JWT; secret header)

The live feed is the ``/ws/sentinel`` WebSocket (``app.api.v1.ws``). Secrets never leave the server:
the API shows a hint (``bot 1234567``, ``key ab…wxyz``) and whether each one is set.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.notifications.base import DispatcherConfigurationError, OutboundMessage
from app.adapters.notifications.discord import validate_webhook_url
from app.adapters.notifications.registry import CONFIG_FIELDS, CREDENTIAL_FIELDS, build, credentials_hint, decrypt
from app.adapters.notifications.twilio import validate_number
from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.core.security_vault import VaultCrypto
from app.models.sentinel import ChannelName, SentinelAlertLog, SentinelChannel, SentinelCommandLog, SentinelDelivery, SentinelRouting, Severity
from app.schemas.sentinel import ChannelCredentials, ChannelUpdate, RoutingUpdate, TestAlert
from app.services import sentinel_hype
from app.services.risk_guard import kill_switch_engaged
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, emit_alert
from app.services.sentinel_commands import CommandCenter, TelegramUpdate, reply_payload
from app.services.sentinel_health import GARUDA, HealthMonitor, LivenessMonitor, read_health
from app.services.sentinel_routing import BROWSER, COLUMNS, ROWS, load_matrix, normalise, validate
from app.workers.sentinel_dispatcher import deliver_direct

logger = logging.getLogger("betdoc.sentinel.api")

router = APIRouter(prefix="/sentinel", tags=["Sentinel · alerting"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_NO_HTTP: httpx.AsyncClient = None  # type: ignore[assignment] - a dispatcher built only to validate never sends


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _vault(request: Request) -> VaultCrypto | None:
    return getattr(request.app.state, "vault", None)


async def _config_changed(request: Request, settings: Settings) -> None:
    """Tell the dispatchers to reload the routing and channels now (they would within 10s anyway)."""
    redis = _redis(request)
    if redis is not None:
        try:
            await redis.incr(SentinelKeys(settings).config_version)
        except (RedisError, OSError):
            pass


def _channel(name: str) -> ChannelName:
    try:
        return ChannelName(name.upper())
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "UNKNOWN_CHANNEL", "message": f"No channel {name!r}"}) from None


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).isoformat()


def _channel_view(channel: ChannelName, row: SentinelChannel | None, vault: VaultCrypto | None) -> dict[str, Any]:
    secrets = decrypt(row, vault) if row is not None else None
    problem = None
    if row is None or not row.encrypted_credentials:
        problem = "no credentials stored"
    elif secrets is None:
        problem = "credentials cannot be decrypted with this vault key" if vault is not None else "MASTER_VAULT_KEY is not configured"
    else:
        try:
            build(row, vault, _NO_HTTP)  # constructs and validates only: nothing is sent
        except DispatcherConfigurationError as exc:
            problem = str(exc)
    return {
        "channel": channel.value,
        "enabled": bool(row.enabled) if row else False,
        "ready": bool(row and row.enabled and problem is None),
        "problem": problem,
        "credentials_hint": row.credentials_hint if row else None,
        "credentials_set": {name: bool(secrets and secrets.get(name)) for name in CREDENTIAL_FIELDS[channel]},
        "credential_fields": list(CREDENTIAL_FIELDS[channel]),
        "config": (row.config or {}) if row else {},
        "config_fields": {name: kind.__name__ for name, kind in CONFIG_FIELDS[channel].items()},
        "last_success_at": _iso(row.last_success_at) if row else None,
        "last_error": row.last_error if row else None,
        "last_error_at": _iso(row.last_error_at) if row else None,
    }


async def _liveness_view(redis: Redis, settings: Settings) -> dict[str, Any]:
    keys = SentinelKeys(settings)
    raw = await redis.get(keys.heartbeat_last(GARUDA))
    state = await redis.hgetall(keys.liveness(GARUDA))
    beat = None
    try:
        beat = json.loads(raw) if raw else None
    except ValueError:
        beat = None
    now = datetime.now(UTC).timestamp()
    age = now - float(beat["at"]) if beat else None
    return {
        "name": GARUDA,
        "status": state.get("status") or ("ALIVE" if age is not None and age < settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS else "UNKNOWN"),
        "last_beat_at": datetime.fromtimestamp(float(beat["at"]), UTC).isoformat() if beat else None,
        "age_seconds": None if age is None else round(age, 1),
        "runner": beat.get("runner") if beat else None,
        "timeout_seconds": settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS,
        "heartbeat_seconds": settings.SENTINEL_HEARTBEAT_SECONDS,
    }


# ---------------------------------------------------------------- status & alerts
@router.get("/status")
async def sentinel_status(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    keys = SentinelKeys(settings)
    out: dict[str, Any] = {"enabled": settings.SENTINEL_ENABLED, "generated_at": datetime.now(UTC).isoformat(), "redis": redis is not None}
    if redis is not None:
        try:
            out["liveness"] = await _liveness_view(redis, settings)
            out["health"] = await read_health(redis, settings)
            out["debounce"] = {k: json.loads(v) for k, v in (await redis.hgetall(keys.debounce)).items()}
            out["stats"] = await redis.hgetall(keys.stats)
            out["stream_length"] = await redis.xlen(keys.stream)
            out["kill_switch"] = await kill_switch_engaged(redis, settings)
            raw = await redis.get(keys.hype_last)
            out["hype"] = json.loads(raw) if raw else None
        except (RedisError, OSError, ValueError):
            out["redis"] = False
    async with sessions() as session:
        rows = {r.channel: r for r in (await session.execute(select(SentinelChannel))).scalars()}
        out["routing"] = await load_matrix(session)
    out["channels"] = [
        {k: v for k, v in _channel_view(c, rows.get(c.value), _vault(request)).items() if k in ("channel", "enabled", "ready", "problem", "last_success_at", "last_error")}
        for c in ChannelName
    ]
    out["settings"] = {
        "debounce_seconds": settings.SENTINEL_DEBOUNCE_SECONDS,
        "health_interval_seconds": settings.SENTINEL_HEALTH_INTERVAL_SECONDS,
        "liveness_timeout_seconds": settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS,
        "heartbeat_seconds": settings.SENTINEL_HEARTBEAT_SECONDS,
        "whale_stake_inr": str(settings.SENTINEL_WHALE_STAKE_INR),
        "margin_utilisation": str(settings.SENTINEL_MARGIN_UTILISATION),
        "hype_at": f"{settings.SENTINEL_HYPE_HOUR:02d}:{settings.SENTINEL_HYPE_MINUTE:02d} {settings.SENTINEL_TIMEZONE}",
        "flash_min_probability": settings.HIVE_FLASH_MIN_PROBABILITY,
    }
    return out


@router.get("/alerts")
async def alerts(
    user: CurrentUser,  # noqa: ARG001
    sessions: Sessions,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    severity: Severity | None = None,
    kind: AlertKind | None = None,
) -> list[dict[str, Any]]:
    async with sessions() as session:
        query = select(SentinelAlertLog).order_by(SentinelAlertLog.occurred_at.desc()).limit(limit)
        if severity is not None:
            query = query.where(SentinelAlertLog.severity == severity.value)
        if kind is not None:
            query = query.where(SentinelAlertLog.kind == kind.value)
        rows = list((await session.execute(query)).scalars())
        deliveries: dict[Any, list[dict[str, Any]]] = {}
        if rows:
            for d in (await session.execute(select(SentinelDelivery).where(SentinelDelivery.alert_id.in_([r.id for r in rows])).order_by(SentinelDelivery.attempted_at))).scalars():
                deliveries.setdefault(d.alert_id, []).append(
                    {"channel": d.channel, "status": d.status, "error": d.error, "latency_ms": d.latency_ms, "digest_id": str(d.digest_id) if d.digest_id else None, "attempted_at": _iso(d.attempted_at)}
                )
    return [
        {
            "id": str(r.id), "kind": r.kind, "severity": r.severity, "title": r.title, "body": r.body, "source": r.source, "dedupe_key": r.dedupe_key,
            "detail": r.detail, "occurred_at": _iso(r.occurred_at), "deliveries": deliveries.get(r.id, []),
        }
        for r in rows
    ]


@router.post("/check")
async def run_checks(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    vault = _vault(request)

    async def direct(alert: SentinelAlert) -> None:
        await deliver_direct(alert, sessions, settings, vault)

    health = await HealthMonitor(redis, sessions, settings, deliver_without_redis=direct).check()
    liveness = (await LivenessMonitor(redis, settings).check()).as_dict() if redis is not None else None
    return {"health": health.as_dict(), "liveness": liveness}


@router.post("/test-alert", status_code=status.HTTP_202_ACCEPTED)
async def test_alert(payload: TestAlert, request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    alert = SentinelAlert(
        kind=AlertKind.TEST,
        severity=payload.severity,
        title=payload.title or f"Test {payload.severity.lower()} alert from the Sentinel tab",
        body=f"Sent by {admin.username} to check routing, the debouncer and the sirens.",
        source="sentinel.test",
        dedupe_key=f"test:{payload.severity}",
    )
    if not await emit_alert(_redis(request), settings, alert):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "BUS_UNAVAILABLE", "message": "The alert bus (Redis) is unreachable"})
    return {"id": str(alert.id), "severity": alert.severity}


# ---------------------------------------------------------------- channels (the vault)
@router.get("/channels")
async def channels(request: Request, admin: CurrentAdmin, sessions: Sessions) -> list[dict[str, Any]]:  # noqa: ARG001
    async with sessions() as session:
        rows = {r.channel: r for r in (await session.execute(select(SentinelChannel))).scalars()}
    return [_channel_view(c, rows.get(c.value), _vault(request)) for c in ChannelName]


def _clean_config(channel: ChannelName, config: dict[str, Any]) -> dict[str, Any]:
    fields = CONFIG_FIELDS[channel]
    unknown = sorted(set(config) - set(fields))
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "UNKNOWN_FIELDS", "message": f"Unknown settings: {', '.join(unknown)}"})
    clean: dict[str, Any] = {}
    try:
        for name, value in config.items():
            kind = fields[name]
            if kind is bool:
                if not isinstance(value, bool):
                    raise ValueError(f"{name} must be true or false")
                clean[name] = value
            elif kind is list:
                if not isinstance(value, list):
                    raise ValueError(f"{name} must be a list")
                if name in ("chat_ids", "admin_user_ids"):
                    clean[name] = [int(v) for v in value]
                else:
                    clean[name] = [validate_number(str(v)) for v in value]
            elif name == "from_number":
                clean[name] = validate_number(str(value)) if value else ""
            else:
                clean[name] = str(value)[:64]
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "BAD_SETTING", "message": str(exc)}) from exc
    return clean


async def _row(session: AsyncSession, channel: ChannelName) -> SentinelChannel:
    row = await session.get(SentinelChannel, channel.value)
    if row is None:
        row = SentinelChannel(channel=channel.value, enabled=False, config={})
        session.add(row)
    return row


@router.put("/channels/{name}")
async def update_channel(name: str, payload: ChannelUpdate, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    channel = _channel(name)
    async with sessions() as session:
        row = await _row(session, channel)
        if payload.config is not None:
            row.config = {**(row.config or {}), **_clean_config(channel, payload.config)}
        if payload.enabled is not None:
            row.enabled = payload.enabled
        await session.commit()
        await session.refresh(row)
    await _config_changed(request, settings)
    return _channel_view(channel, row, _vault(request))


@router.put("/channels/{name}/credentials")
async def set_credentials(name: str, payload: ChannelCredentials, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    channel = _channel(name)
    vault = _vault(request)
    if vault is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_VAULT", "message": "MASTER_VAULT_KEY is not configured"})
    unknown = sorted(set(payload.values) - set(CREDENTIAL_FIELDS[channel]))
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "UNKNOWN_FIELDS", "message": f"Unknown credentials: {', '.join(unknown)}"})
    incoming = {k: v.strip() for k, v in payload.values.items()}
    if channel is ChannelName.DISCORD and incoming.get("webhook_url"):
        try:
            validate_webhook_url(incoming["webhook_url"])
        except DispatcherConfigurationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "BAD_WEBHOOK", "message": str(exc)}) from exc
    async with sessions() as session:
        row = await _row(session, channel)
        current = decrypt(row, vault)
        merged = {**(current.values if current else {}), **incoming}
        merged = {k: v for k, v in merged.items() if v}
        try:
            row.encrypted_credentials = vault.encrypt_key(json.dumps(merged)) if merged else None
            row.credentials_hint = credentials_hint(channel, merged) if merged else None
        finally:
            merged.clear()
            incoming.clear()
        await session.commit()
        await session.refresh(row)
    await _config_changed(request, settings)
    return _channel_view(channel, row, vault)


@router.delete("/channels/{name}/credentials", status_code=status.HTTP_204_NO_CONTENT)
async def clear_credentials(name: str, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> Response:  # noqa: ARG001
    channel = _channel(name)
    async with sessions() as session:
        row = await session.get(SentinelChannel, channel.value)
        if row is not None:
            row.encrypted_credentials, row.credentials_hint, row.enabled = None, None, False
            await session.commit()
    await _config_changed(request, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/channels/{name}/test")
async def test_channel(name: str, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    channel = _channel(name)
    async with sessions() as session:
        row = await session.get(SentinelChannel, channel.value)
        if row is None:
            raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NOT_CONFIGURED", "message": "Store this channel's credentials first"})
        async with httpx.AsyncClient(timeout=settings.SENTINEL_HTTP_TIMEOUT_SECONDS) as http:
            try:
                dispatcher = build(row, _vault(request), http)
            except DispatcherConfigurationError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "NOT_READY", "message": str(exc)}) from exc
            message = OutboundMessage(Severity.INFO, AlertKind.TEST, f"Sentinel test on {channel.value.title()}", f"Sent by {admin.username} from the Control Panel.", datetime.now(UTC), "sentinel.test")
            result = await dispatcher.send(message)
        now = datetime.now(UTC)
        if result.ok:
            row.last_success_at = now
        else:
            row.last_error, row.last_error_at = (result.error or "failed")[:300], now
        session.add(SentinelDelivery(alert_id=None, channel=channel.value, status="SENT" if result.ok else "FAILED", error=result.error, latency_ms=result.latency_ms, attempted_at=now))
        await session.commit()
    return {"channel": channel.value, "ok": result.ok, "status_code": result.status_code, "error": result.error, "latency_ms": result.latency_ms, "deliveries": result.deliveries}


# ---------------------------------------------------------------- routing
@router.get("/routing")
async def routing(user: CurrentUser, sessions: Sessions) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        return {"matrix": await load_matrix(session), "rows": [str(r) for r in ROWS], "columns": list(COLUMNS), "browser": BROWSER}


@router.put("/routing")
async def update_routing(payload: RoutingUpdate, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    problems = validate(payload.matrix)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"reason": "BAD_MATRIX", "message": "; ".join(problems)})
    async with sessions() as session:
        row = await session.get(SentinelRouting, 1)
        if row is None:
            row = SentinelRouting(id=1)
            session.add(row)
        row.matrix = normalise(payload.matrix)
        row.updated_by = admin.id
        await session.commit()
        matrix = row.matrix
    await _config_changed(request, settings)
    return {"matrix": matrix, "rows": [str(r) for r in ROWS], "columns": list(COLUMNS), "browser": BROWSER}


# ---------------------------------------------------------------- hype
@router.get("/hype")
async def hype_last(request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any] | None:  # noqa: ARG001
    redis = _redis(request)
    if redis is None:
        return None
    raw = await redis.get(SentinelKeys(settings).hype_last)
    return json.loads(raw) if raw else None


@router.post("/hype/preview")
async def hype_preview(request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "The math engine's live state (Redis) is unreachable"})
    try:
        return await sentinel_hype.market_forecast_hype(redis, settings, dry_run=True)
    except (RedisError, OSError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "The math engine's live state (Redis) is unreachable"}) from exc


# ---------------------------------------------------------------- Telegram
@router.get("/commands")
async def commands(admin: CurrentAdmin, sessions: Sessions, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict[str, Any]]:  # noqa: ARG001
    async with sessions() as session:
        rows = (await session.execute(select(SentinelCommandLog).order_by(SentinelCommandLog.received_at.desc()).limit(limit))).scalars()
        return [
            {"id": r.id, "command": r.command, "sender": r.sender, "chat_id": r.chat_id, "authorised": r.authorised, "outcome": r.outcome, "received_at": _iso(r.received_at)}
            for r in rows
        ]


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    """Telegram calls this with every update. It must carry the secret token set with ``setWebhook``
    (``secret_token``); the reply, if any, is the ``sendMessage`` call in this response's body."""
    async with sessions() as session:
        row = await session.get(SentinelChannel, ChannelName.TELEGRAM.value)
    secrets = decrypt(row, _vault(request)) if row is not None and row.enabled else None
    expected = secrets.get("webhook_secret") if secrets else ""
    if not expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NOT_CONFIGURED", "message": "Not found"})
    supplied = request.headers.get(TELEGRAM_SECRET_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        logger.warning("Sentinel: Telegram webhook call with a wrong secret token")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"reason": "BAD_SECRET", "message": "Unauthorized"})
    try:
        body = await request.json()
    except ValueError:
        return {}
    update = TelegramUpdate.parse(body) if isinstance(body, dict) else None
    if update is None or not update.text or update.chat_id is None:
        return {}
    redis = _redis(request)
    if redis is not None:
        try:
            if not await redis.set(SentinelKeys(settings).telegram_update(update.update_id), "1", nx=True, ex=86_400):
                return {}  # Telegram retried an update this webhook already answered
        except (RedisError, OSError):
            pass
    reply = await CommandCenter(sessions, redis, settings).handle(update, (row.config or {}) if row is not None else {})
    if reply.text is None:
        return {}
    return reply_payload(update.chat_id, reply.text)
