"""The Vault & Fleet admin API under ``/api/v1/vault`` (Group 70). Every route is admin-only.

    POST   /vault/import-preview           parse a .md upload, pasted text or an allowed server path; write nothing
    POST   /vault/import-markdown          the same, then encrypt (AES-256-GCM) and upsert everything found
    GET    /vault/imports                  the import trail (counts and warnings, no values)
    GET    /vault/accounts                 every bookmaker account, secrets masked ("pa***42", has_password...)
    POST   /vault/accounts                 add one by hand
    PATCH  /vault/accounts/{id}            label, currency, priority, balance, stake cap, active; replace secrets
    PATCH  /vault/accounts/{id}/toggle     enable / disable
    DELETE /vault/accounts/{id}            (refused while an order holds stake on it)
    GET    /vault/accounts/route           which account would carry an order (bookmaker, stake, currency)
    GET    /vault/providers                data-provider keys, masked
    PATCH  /vault/providers/{id}           enable / disable
    POST   /vault/providers/{id}/link      make it the key its Fleet Command source runs on
    DELETE /vault/providers/{id}
    GET    /vault/fleet-config             activated sports, markets per sport, quiet hours, account routing
    PUT    /vault/fleet-config
    GET    /vault/status                   sports, providers, quota and its burn rate, currencies and their FX rates
    POST   /vault/probe                    run the credential health prober now (sanctioned APIs only)
    POST   /vault/rotate-keys              re-seal every secret under the current MASTER_VAULT_KEY
    POST   /vault/export-backup            the whole vault, sealed under a passphrase (re-enter your password)
    POST   /vault/restore-backup           upsert a backup (idempotent)

The responses never carry a secret; the import preview names fields and line numbers, never values.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.ingestion.odds_api_adapter import credits_per_call, odds_api_markets
from app.api.deps import CurrentAdmin, DbSession
from app.core import fleet_overlay
from app.core.config import Settings, get_settings
from app.core.omni_keys import OmniRedisKeys
from app.core.security import verify_password
from app.core.security_vault import VaultCrypto, VaultDecryptionError
from app.models.omni_vault import OmniFleetSource, VaultBookmakerAccount, VaultImportRun, VaultProviderCredential
from app.schemas.vault_admin import (
    AccountCreate,
    AccountUpdate,
    BackupExportRequest,
    BackupRestoreRequest,
    FleetConfigUpdate,
    ProviderLinkRequest,
    ProviderToggle,
)
from app.services.vault import catalog, fleet_config, registry
from app.services.vault.account_rotator import NoAccount, get_optimal_account
from app.services.vault.markdown_importer import (
    ImportSourceError,
    ParsedAccount,
    content_digest,
    decode_upload,
    import_parsed,
    link_to_fleet,
    parse_markdown,
    preview_payload,
    read_path,
)

logger = logging.getLogger("betdoc.vault.api")

router = APIRouter(prefix="/vault", tags=["Vault · fleet credentials"])

AppSettings = Annotated[Settings, Depends(get_settings)]
_probe_task: asyncio.Task[Any] | None = None


def _vault(request: Request) -> VaultCrypto:
    vault: VaultCrypto | None = getattr(request.app.state, "vault", None)
    if vault is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MASTER_VAULT_KEY is not configured: the Vault cannot encrypt")
    return vault


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


async def _publish(request: Request, db: AsyncSession, settings: Settings) -> None:
    await fleet_config.publish(db, _redis(request), settings)


async def _read_source(file: UploadFile | None, text: str | None, path: str | None, settings: Settings) -> tuple[str, str]:
    given = [x for x in (file, text, path) if x not in (None, "")]
    if len(given) != 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="send exactly one of: file, text, path")
    try:
        if file is not None:
            data = await file.read(settings.VAULT_IMPORT_MAX_BYTES + 1)
            return decode_upload(data, settings), "upload"
        if text:
            if len(text.encode("utf-8")) > settings.VAULT_IMPORT_MAX_BYTES:
                raise ImportSourceError(f"the text is over {settings.VAULT_IMPORT_MAX_BYTES:,} bytes")
            return text, "text"
        return await asyncio.to_thread(read_path, path or "", settings), "path"
    except ImportSourceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


Upload = Annotated[UploadFile | None, File(description="a .md / .txt credentials file")]
TextField = Annotated[str | None, Form(description="pasted markdown")]
PathField = Annotated[str | None, Form(description="a file under VAULT_IMPORT_ALLOWED_DIRS")]


# ------------------------------------------------------------------------------------------ import
@router.post("/import-preview")
async def import_preview(request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings, file: Upload = None, text: TextField = None, path: PathField = None) -> dict[str, Any]:
    """Dry run: what the file holds and what an import would create / update, without writing."""
    content, origin = await _read_source(file, text, path, settings)
    parsed = parse_markdown(content)
    vault = getattr(request.app.state, "vault", None)
    report = None
    if vault is not None:
        report = await import_parsed(db, vault, parsed, settings, origin=origin, content_sha256=content_digest(content), actor_id=admin.id, dry_run=True)
        await db.rollback()
    content = ""
    return preview_payload(parsed, report) | {"vault_configured": vault is not None}


@router.post("/import-markdown")
async def import_markdown(request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings, file: Upload = None, text: TextField = None, path: PathField = None) -> dict[str, Any]:
    vault = _vault(request)
    content, origin = await _read_source(file, text, path, settings)
    parsed = parse_markdown(content)
    try:
        report = await import_parsed(db, vault, parsed, settings, origin=origin, content_sha256=content_digest(content), actor_id=admin.id, redis=_redis(request))
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the import clashed with a concurrent change: nothing was written, try again") from exc
    finally:
        content = ""
    await _publish(request, db, settings)
    logger.info("Vault import (%s) by %s: accounts +%d ~%d =%d, providers +%d ~%d =%d, sports +%d", origin, admin.username,
                report.accounts_created, report.accounts_updated, report.accounts_unchanged, report.providers_created, report.providers_updated,
                report.providers_unchanged, len(report.sports_added))
    return parsed.summary() | {"report": report.as_dict()}


@router.get("/imports")
async def import_runs(db: DbSession, admin: CurrentAdmin, limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:  # noqa: ARG001
    rows = (await db.execute(select(VaultImportRun).order_by(VaultImportRun.created_at.desc()).limit(limit))).scalars()
    return [
        {"id": str(r.id), "origin": r.origin, "created_at": r.created_at.isoformat(), "content_sha256": r.content_sha256[:12],
         "accounts": {"created": r.accounts_created, "updated": r.accounts_updated, "unchanged": r.accounts_unchanged},
         "providers": {"created": r.providers_created, "updated": r.providers_updated, "unchanged": r.providers_unchanged},
         "sports_added": r.sports_added or [], "warnings": r.warnings or []}
        for r in rows
    ]


# ------------------------------------------------------------------------------------------ accounts
async def _account(db: AsyncSession, account_id: uuid.UUID) -> VaultBookmakerAccount:
    row = await db.get(VaultBookmakerAccount, account_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such account")
    return row


@router.get("/accounts")
async def accounts(db: DbSession, admin: CurrentAdmin) -> list[dict[str, Any]]:  # noqa: ARG001
    return await registry.list_accounts(db)


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
async def create_account(payload: AccountCreate, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    vault = _vault(request)
    entity = catalog.match_entity(payload.bookmaker)
    book = entity.key if entity is not None and entity.kind == "bookmaker" else catalog.slug(payload.bookmaker)
    secrets = payload.plain()
    data = ParsedAccount(book, 0, "manual", label=payload.label, currency=payload.currency, balance=payload.balance, stake_cap=payload.stake_cap,
                         **{k: secrets.get(k) for k in ("username", "password", "api_key", "token", "totp_seed", "notes", "url")})
    try:
        row = await registry.create_account(db, vault, settings, data, admin.id)
        await db.commit()
    except registry.VaultConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    finally:
        secrets.clear()
    await _publish(request, db, settings)
    logger.info("Vault account %s (%s) added by %s", row.id, book, admin.username)
    return registry.account_view(row)


@router.patch("/accounts/{account_id}")
async def update_account(account_id: uuid.UUID, payload: AccountUpdate, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    vault = _vault(request)
    row = await _account(db, account_id)
    secrets = payload.secrets.plain() if payload.secrets is not None else {}
    if secrets.get("username", "") is None and "username" in secrets and not (row.encrypted_api_key or row.encrypted_token or secrets.get("api_key") or secrets.get("token")):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="the login cannot be cleared: the account would have nothing to identify it")
    try:
        await registry.update_account(db, vault, row, payload.changes(), secrets)
        await fleet_config.bump(db, admin.id)
        await db.commit()
    except registry.VaultConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (ValueError, VaultDecryptionError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    finally:
        secrets.clear()
    await db.refresh(row)
    await _publish(request, db, settings)
    logger.info("Vault account %s edited by %s (%s)", row.id, admin.username, ", ".join(sorted(payload.model_fields_set)))
    return registry.account_view(row)


@router.patch("/accounts/{account_id}/toggle")
async def toggle_account(account_id: uuid.UUID, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings, payload: ProviderToggle | None = None) -> dict[str, Any]:
    row = await _account(db, account_id)
    row.is_active = payload.is_active if payload is not None else not row.is_active
    await fleet_config.bump(db, admin.id)
    await db.commit()
    await db.refresh(row)
    await _publish(request, db, settings)
    logger.info("Vault account %s %s by %s", row.id, "enabled" if row.is_active else "disabled", admin.username)
    return registry.account_view(row)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(account_id: uuid.UUID, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> None:
    row = await _account(db, account_id)
    if (await registry.open_holds(db)).get(row.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="an order in flight holds stake on this account: disable it instead, then delete once it settles")
    await db.delete(row)
    await fleet_config.bump(db, admin.id)
    await db.commit()
    await _publish(request, db, settings)
    logger.info("Vault account %s deleted by %s", account_id, admin.username)


@router.get("/accounts/route")
async def route_preview(db: DbSession, admin: CurrentAdmin, bookmaker: str, stake: Decimal = Query(gt=0), currency: str | None = None) -> dict[str, Any]:  # noqa: ARG001
    entity = catalog.match_entity(bookmaker)
    book = entity.key if entity is not None and entity.kind == "bookmaker" else catalog.slug(bookmaker)
    choice = await get_optimal_account(db, book, stake, currency=currency.upper() if currency else None)
    if isinstance(choice, NoAccount):
        return {"bookmaker": book, "stake": str(stake), "account": None, "reason": choice.reason, "message": choice.message,
                "largest_capacity": None if choice.largest_capacity is None else str(choice.largest_capacity)}
    return {"bookmaker": book, "stake": str(stake), "account": {"id": str(choice.account_id), "label": choice.label, "currency": choice.currency,
            "free_funds": None if choice.free_funds is None else str(choice.free_funds), "priority": choice.priority}, "reason": None,
            "routing_enabled": fleet_overlay.current().account_routing}


# ------------------------------------------------------------------------------------------ providers
async def _provider(db: AsyncSession, provider_id: uuid.UUID) -> VaultProviderCredential:
    row = await db.get(VaultProviderCredential, provider_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such provider key")
    return row


@router.get("/providers")
async def providers(db: DbSession, admin: CurrentAdmin) -> list[dict[str, Any]]:  # noqa: ARG001
    return await registry.list_providers(db)


@router.patch("/providers/{provider_id}")
async def toggle_provider(provider_id: uuid.UUID, payload: ProviderToggle, db: DbSession, admin: CurrentAdmin) -> dict[str, Any]:
    row = await _provider(db, provider_id)
    row.is_active = payload.is_active
    await db.commit()
    await db.refresh(row)
    logger.info("Vault provider key %s %s by %s", row.id, "enabled" if row.is_active else "disabled", admin.username)
    return registry.provider_view(row)


@router.post("/providers/{provider_id}/link")
async def link_provider(provider_id: uuid.UUID, payload: ProviderLinkRequest, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    vault = _vault(request)
    row = await _provider(db, provider_id)
    if not row.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="enable the key before linking it")
    try:
        outcome = await link_to_fleet(db, vault, row, settings, _redis(request), force=payload.force)
    except VaultDecryptionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if outcome == "no_source":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"no Fleet Command source runs on {catalog.provider_display(row.provider_id)} keys yet: add its provider spec in Fleet Command first")
    await db.commit()
    await db.refresh(row)
    logger.info("Vault provider key %s linked to %s by %s (%s)", row.id, row.linked_source_id, admin.username, outcome)
    return registry.provider_view(row) | {"outcome": outcome}


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: uuid.UUID, db: DbSession, admin: CurrentAdmin) -> None:
    row = await _provider(db, provider_id)
    if row.linked_source_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Fleet Command's {row.linked_source_id} runs on this key: link another key (or clear it in Fleet Command) first")
    await db.delete(row)
    await db.commit()
    logger.info("Vault provider key %s deleted by %s", provider_id, admin.username)


# ------------------------------------------------------------------------------------------ fleet configuration
def _config_view(row: Any) -> dict[str, Any]:
    return {"sports": list(row.sports or []), "markets_by_sport": dict(row.markets_by_sport or {}), "quiet_start": row.quiet_start,
            "quiet_end": row.quiet_end, "timezone": row.timezone, "account_routing": bool(row.account_routing), "version": row.version,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@router.get("/fleet-config")
async def get_fleet_config(db: DbSession, admin: CurrentAdmin) -> dict[str, Any]:  # noqa: ARG001
    row = await fleet_config.get_config(db)
    await db.commit()
    return _config_view(row)


@router.put("/fleet-config")
async def put_fleet_config(payload: FleetConfigUpdate, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    row = await fleet_config.get_config(db)
    for name in payload.model_fields_set:
        setattr(row, name, getattr(payload, name) if name not in ("sports", "markets_by_sport") else (getattr(payload, name) or ([] if name == "sports" else {})))
    await fleet_config.bump(db, admin.id)
    await db.commit()
    await db.refresh(row)
    await _publish(request, db, settings)
    logger.info("Vault fleet configuration v%s by %s (%s)", row.version, admin.username, ", ".join(sorted(payload.model_fields_set)))
    return _config_view(row)


# ------------------------------------------------------------------------------------------ status
async def _hash(redis: Redis | None, key: str) -> dict[str, str]:
    if redis is None:
        return {}
    try:
        return await redis.hgetall(key)
    except (RedisError, OSError):
        return {}


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("/status")
async def vault_status(request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    await fleet_config.refresh(redis, settings, None)
    overlay = fleet_overlay.current()
    config = await fleet_config.get_config(db)
    now = datetime.now(UTC)
    env_sports = [s.strip() for s in settings.ODDS_SPORT_KEYS.split(",") if s.strip()]
    offered: set[str] = set()
    if redis is not None:
        try:
            offered = {m.decode() if isinstance(m, bytes) else str(m) for m in await redis.smembers(fleet_config.offered_key(settings))}
        except (RedisError, OSError):
            offered = set()
    keys = OmniRedisKeys(settings.omni_redis_prefix)
    odds_metrics = await _hash(redis, keys.fleet_metrics("odds_api"))
    interval = max(10.0, float(settings.ODDS_POLLING_INTERVAL_SEC))
    sports = []
    for sport in list(dict.fromkeys([*env_sports, *(config.sports or [])])):
        cost = credits_per_call(settings, sport, now)
        sports.append({
            "key": sport, "source": "environment" if sport in env_sports else "vault",
            "polling": sport in settings.odds_sport_keys, "offered": None if not offered else sport in offered,
            "markets": odds_api_markets(settings, sport, now), "credits_per_call": cost,
        })
    burn = sum(s["credits_per_call"] for s in sports if s["polling"]) * 3600.0 / interval
    remaining = _num(odds_metrics.get("quota_remaining"))
    sources = []
    for row in (await db.execute(select(OmniFleetSource).order_by(OmniFleetSource.source_id))).scalars():
        metrics = await _hash(redis, keys.fleet_metrics(row.source_id))
        sources.append({"source_id": row.source_id, "enabled": bool(row.is_enabled), "has_key": bool(row.encrypted_api_key), "key_hint": row.api_key_hint,
                        "paused": row.paused_at is not None, "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                        "quota_remaining": _num(metrics.get("quota_remaining")), "quota_fraction": _num(metrics.get("quota_fraction"))})
    providers = await registry.list_providers(db)
    accounts = await registry.list_accounts(db)
    from app.services.fx_rates import FxRates  # noqa: PLC0415

    rates = await FxRates(redis, settings).snapshot() if redis is not None else {}
    currencies = {}
    for book, currency in sorted(overlay.currencies.items()):
        currencies[book] = {"currency": currency, "fx_ok": currency == "INR" or currency in rates,
                            "note": None if currency == "INR" or currency in rates else f"no live {currency}/INR rate: {catalog.bookmaker_display(book)} prices are refused until one is published"}
    per_book: dict[str, dict[str, int]] = {}
    for a in accounts:
        counts = per_book.setdefault(a["bookmaker_id"], {"accounts": 0, "active": 0})
        counts["accounts"] += 1
        counts["active"] += int(a["is_active"])
    return {
        "generated_at": now.isoformat(), "overlay_version": overlay.version, "account_routing": overlay.account_routing,
        "quiet_hours": {"start": config.quiet_start, "end": config.quiet_end, "timezone": config.timezone, "active": overlay.in_quiet_hours(now)},
        "sports": sports, "odds_api_index_known": bool(offered),
        "quota": {"remaining": remaining, "used": _num(odds_metrics.get("quota_used")), "limit": _num(odds_metrics.get("quota_limit")),
                  "fraction": _num(odds_metrics.get("quota_fraction")), "floor": settings.ODDS_QUOTA_FLOOR, "regions": settings.ODDS_API_REGIONS,
                  "poll_interval_seconds": interval, "credits_per_hour": round(burn, 1),
                  "hours_left": round(remaining / burn, 1) if remaining is not None and burn > 0 else None},
        "sources": sources, "providers": providers, "bookmakers": per_book, "currencies": currencies,
        "prober": await _hash(redis, f"{settings.omni_redis_prefix}:vault:prober"),
        "vault_configured": getattr(request.app.state, "vault", None) is not None,
        "path_imports": bool(settings.VAULT_IMPORT_ALLOWED_DIRS),
    }


# ------------------------------------------------------------------------------------------ prober, rotation, backup
@router.post("/probe", status_code=status.HTTP_202_ACCEPTED)
async def probe_now(request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    global _probe_task
    from app.core.database import AsyncSessionLocal  # noqa: PLC0415
    from app.workers.vault_prober import probe_all  # noqa: PLC0415

    vault = _vault(request)
    if _probe_task is not None and not _probe_task.done():
        return {"started": False, "detail": "a probe run is already going"}
    _probe_task = asyncio.create_task(probe_all(AsyncSessionLocal, _redis(request), settings, vault, force=True), name="vault-probe")
    logger.info("Vault probe started by %s", admin.username)
    return {"started": True, "detail": "probing, with a 3-7 s pause between checks; results appear on each account"}


@router.post("/rotate-keys")
async def rotate_keys(request: Request, db: DbSession, admin: CurrentAdmin) -> dict[str, int]:
    vault = _vault(request)
    try:
        counts = await registry.rotate_all(db, vault)
        await db.commit()
    except VaultDecryptionError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"a secret would not open under the configured keys: nothing was re-sealed ({exc})") from exc
    logger.info("Vault re-sealed by %s: %s", admin.username, counts)
    return counts


@router.post("/export-backup")
async def export_backup(payload: BackupExportRequest, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    if not verify_password(payload.current_password.get_secret_value(), admin.hashed_password):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="that is not your password")
    vault = _vault(request)
    try:
        backup = await registry.export_backup(db, vault, payload.passphrase.get_secret_value(), settings)
    except registry.BackupError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except VaultDecryptionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"a secret would not open: {exc}") from exc
    logger.warning("Vault backup exported by %s (%s)", admin.username, backup["counts"])
    return backup


@router.post("/restore-backup")
async def restore_backup(payload: BackupRestoreRequest, request: Request, db: DbSession, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    vault = _vault(request)
    try:
        report = await registry.restore_backup(db, vault, payload.backup, payload.passphrase.get_secret_value(), settings, admin.id)
        await fleet_config.bump(db, admin.id)
        await db.commit()
    except registry.BackupError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    await _publish(request, db, settings)
    logger.warning("Vault backup restored by %s", admin.username)
    return report
