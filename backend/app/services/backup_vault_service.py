"""Group 87: the encrypted backup & integrity restore vault (SHIVAJI & TODAR MAL). Developer Credit: Amit Ashok Kumar Patnaik.

A snapshot is every live table's rows (up to ``BACKUP_MAX_ROWS_PER_TABLE`` each, ordered by primary key), sealed by
``backup_vault_math`` into ``<BACKUP_VAULT_DIR>/betdoc_vault_<UTC stamp>_<id>.betdoc.enc.gz``, with a manifest chained
to the previous one. It complements ``scripts/backup.sh`` (``pg_dump`` to ``*.sql.gz``): The Archive's "last backup"
is the newer of the two, and nothing pretends to be a pg_dump file.

* ``INTEGRITY_VERIFY``: the file's digest, the Fernet HMAC, the plaintext digest and the envelope.
* ``DRY_RUN_RESTORE``: all of that, then every snapshot table and column against the live schema: what a restore here
  could not put back is named (``missing_tables``, ``missing_columns``). Nothing is written to the data tables.
* Pruning deletes the files of unpinned snapshots past the newest ``retention_count``; their manifests stay (status
  PRUNED) so the chain can still be verified.
* The protected bankroll is measured (the real bankrolls' cash and locked exposure, or the CFO main accounts' balance
  and exposure, the larger); USD only at TODAR MAL's fresh rate.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, Settings
from app.domain.archive import backup_vault_math as bvm
from app.domain.archive.errors import ArchiveDomainError
from app.domain.archive.manager import ENCRYPTION_ALGORITHM, _serialize_row
from app.models import Base
from app.models.archive import ArchiveAccessLogModel, ArchiveAction
from app.models.backup_vault import (
    BackupAuditOperation,
    BackupAuditVerdict,
    BackupRestoreAuditModel,
    BackupSnapshotManifestModel,
    BackupSnapshotStatus,
    BackupTriggerMode,
)
from app.models.cfo_vault import BankrollAccount
from app.models.real_money import RealBankroll
from app.schemas.archive import (
    BackupCreateRequest,
    BackupRestoreAuditRead,
    BackupSnapshotRead,
    BackupVaultDashboardResponse,
    BackupVerifyResponse,
)

logger = logging.getLogger("betdoc.backup_vault")

VAULT_TABLES: frozenset[str] = frozenset({"backup_snapshot_manifests", "backup_restore_audits"})  # the vault's own record is not in its snapshots
SUFFIX = ".betdoc.enc.gz"
SCHEDULER = "SHIVAJI_SCHEDULER"
CHAIN_RETRIES = 3


def _stamp(moment: datetime) -> str:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _manifest_hash(prev: str, m: BackupSnapshotManifestModel) -> str:
    return bvm.compute_backup_manifest_hash(prev_hash=prev, filename=m.filename, plaintext_sha256=m.plaintext_sha256, ciphertext_sha256=m.ciphertext_sha256,
                                            total_tables=m.total_tables, total_rows=m.total_rows, protected_bankroll_inr=m.protected_bankroll_inr,
                                            timestamp_iso=_stamp(m.created_at))


def resolve_backup_dir(settings: Settings) -> Path:
    path = Path(settings.BACKUP_VAULT_DIR)
    return path if path.is_absolute() else PROJECT_ROOT / path


class BackupVaultService:
    def __init__(self, *, backup_dir: Path | None = None) -> None:
        self._override_dir = backup_dir

    def _dir(self, settings: Settings, *, create: bool = False) -> Path:
        target = self._override_dir if self._override_dir is not None else resolve_backup_dir(settings)
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    # ---------------------------------------------------------------- measurements
    async def _live_schema(self, db: AsyncSession) -> dict[str, set[str]]:
        """Every registered table that exists in the connected database, with its live columns."""
        def _collect(sync_session: Session) -> dict[str, set[str]]:
            inspector = inspect(sync_session.connection())
            present = set(inspector.get_table_names())
            return {key: {c["name"] for c in inspector.get_columns(t.name)} for key, t in Base.metadata.tables.items() if t.name in present}
        return await db.run_sync(_collect)

    async def _usd_rate(self, db: AsyncSession, redis: Redis | None, settings: Settings, now: datetime, live: dict[str, set[str]]) -> Decimal | None:
        from app.services.currency_service import CurrencyService, CurrencyUnavailableError  # noqa: PLC0415

        if "currency_exchange_rates" not in live and redis is None:
            return None  # nowhere a rate could be read from
        try:
            return (await CurrencyService(settings, redis).inr_per_unit(db, "USD", now, allow_refresh=False)).inr_per_unit
        except CurrencyUnavailableError:
            return None

    async def _protected_bankroll(self, db: AsyncSession, live: dict[str, set[str]]) -> Decimal | None:
        real = cfo = Decimal("0")
        if "real_bankrolls" in live:
            real = Decimal(str(await db.scalar(select(func.coalesce(func.sum(RealBankroll.cash_balance + RealBankroll.locked_exposure), 0))) or 0))
        if "cfo_bankroll_accounts" in live:
            cfo = Decimal(str(await db.scalar(select(func.coalesce(func.sum(BankrollAccount.available_balance + BankrollAccount.exposure_balance), 0))
                                              .where(BankrollAccount.bot_id.is_(None))) or 0))
        best = max(real, cfo)
        return best.quantize(bvm.CENT, rounding=ROUND_HALF_UP) if best > 0 else None

    async def _export(self, db: AsyncSession, live: dict[str, set[str]], cap: int) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """Each live table's rows by primary key, at most ``cap``; the tables that held more."""
        payload: dict[str, list[dict[str, Any]]] = {}
        truncated: list[str] = []
        for name, table in sorted(Base.metadata.tables.items()):
            if name not in live or name in VAULT_TABLES:
                continue
            stmt = select(table).limit(cap + 1)
            if table.primary_key.columns:
                stmt = stmt.order_by(*table.primary_key.columns)
            rows = (await db.execute(stmt)).mappings().all()
            if len(rows) > cap:
                truncated.append(name)
                rows = rows[:cap]
            payload[name] = [_serialize_row(row) for row in rows]
        return payload, truncated

    async def _architect(self, db: AsyncSession, live: dict[str, set[str]]) -> str:
        from app.models.control_panel import SystemSettingsModel  # noqa: PLC0415

        if "system_settings" in live:
            from app.services.twin.vetting import developer_credit  # noqa: PLC0415 - pulls in the oracle engine

            return await developer_credit(db)
        return str(SystemSettingsModel.__table__.c.developer_name.default.arg)

    # ---------------------------------------------------------------- reads
    async def latest_snapshot_at(self, db: AsyncSession) -> datetime | None:
        """The newest live snapshot (for The Archive's "last backup")."""
        return await db.scalar(select(func.max(BackupSnapshotManifestModel.created_at)).where(BackupSnapshotManifestModel.status != BackupSnapshotStatus.PRUNED.value))

    async def dashboard(self, db: AsyncSession, redis: Redis | None, settings: Settings, limit: int = 25) -> BackupVaultDashboardResponse:  # noqa: ARG002
        chain = list((await db.execute(select(BackupSnapshotManifestModel).order_by(BackupSnapshotManifestModel.created_at))).scalars())
        intact, prev = True, bvm.GENESIS_BACKUP_HASH
        for m in chain:
            if m.prev_hash != prev or _manifest_hash(prev, m) != m.entry_hash:
                intact = False
                break
            prev = m.entry_hash
        live_snaps = [m for m in chain if m.status != BackupSnapshotStatus.PRUNED.value]
        audits = list((await db.execute(select(BackupRestoreAuditModel).order_by(BackupRestoreAuditModel.created_at.desc()).limit(limit))).scalars())
        return BackupVaultDashboardResponse(
            encryption_algorithm=ENCRYPTION_ALGORITHM, backup_dir=str(self._dir(settings)), schedule_enabled=settings.BACKUP_SCHEDULE_ENABLED,
            schedule_hours=settings.BACKUP_SCHEDULE_HOURS, retention_count=settings.BACKUP_RETENTION_COUNT, max_rows_per_table=settings.BACKUP_MAX_ROWS_PER_TABLE,
            current_key_fingerprint=bvm.key_fingerprint(settings.ENCRYPTION_KEY.get_secret_value()), total_snapshots=len(live_snaps),
            total_storage_bytes=sum(m.file_bytes for m in live_snaps), latest_backup_at=live_snaps[-1].created_at if live_snaps else None, chain_intact=intact,
            snapshots=[BackupSnapshotRead.model_validate(m) for m in reversed(chain[-limit:])], audits=[BackupRestoreAuditRead.model_validate(a) for a in audits],
            architect=await self._architect(db, await self._live_schema(db)),
        )

    # ---------------------------------------------------------------- writes
    async def create_snapshot(self, db: AsyncSession, redis: Redis | None, settings: Settings, payload: BackupCreateRequest, *, by: str,
                              trigger: BackupTriggerMode | None = None) -> BackupSnapshotManifestModel:
        now = datetime.now(UTC)
        live = await self._live_schema(db)
        rate = await self._usd_rate(db, redis, settings, now, live)
        bankroll = await self._protected_bankroll(db, live)
        tables, truncated = await self._export(db, live, settings.BACKUP_MAX_ROWS_PER_TABLE)
        key = settings.ENCRYPTION_KEY.get_secret_value()
        mode = (trigger or BackupTriggerMode(payload.trigger_mode)).value
        blob = bvm.create_encrypted_snapshot_blob(tables_payload=tables, fernet_key=key, metadata={
            "snapshot_label": payload.snapshot_label, "trigger_mode": mode, "triggered_by": by, "created_at": _stamp(now), "truncated_tables": truncated,
            "max_rows_per_table": settings.BACKUP_MAX_ROWS_PER_TABLE})
        filename = f"betdoc_vault_{now:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}{SUFFIX}"
        path = self._dir(settings, create=True) / filename
        path.write_bytes(blob.ciphertext_bytes)
        manifest = BackupSnapshotManifestModel(
            id=uuid4(), snapshot_label=payload.snapshot_label, trigger_mode=mode, status=BackupSnapshotStatus.COMPLETED.value, filename=filename,
            storage_path=str(path), encryption_algorithm=ENCRYPTION_ALGORITHM, key_fingerprint=bvm.key_fingerprint(key), file_bytes=blob.file_bytes,
            uncompressed_bytes=blob.uncompressed_bytes, total_tables=blob.total_tables, total_rows=blob.total_rows, is_complete=not truncated,
            truncated_tables=truncated, protected_bankroll_inr=bankroll, protected_bankroll_usd=bvm.convert_inr_to_usd(bankroll, rate),
            plaintext_sha256=blob.plaintext_sha256, ciphertext_sha256=blob.ciphertext_sha256, prev_hash="", entry_hash="", is_pinned=payload.is_pinned,
            triggered_by=by[:128], created_at=now,
        )
        for attempt in range(CHAIN_RETRIES):
            last = (await db.execute(select(BackupSnapshotManifestModel).order_by(BackupSnapshotManifestModel.created_at.desc()).limit(1))).scalars().first()
            manifest.prev_hash = bvm.GENESIS_BACKUP_HASH if last is None else last.entry_hash
            manifest.entry_hash = _manifest_hash(manifest.prev_hash, manifest)
            db.add(manifest)
            db.add(ArchiveAccessLogModel(user_id=None, action=ArchiveAction.INTEGRITY_CHECK.value, target_resource=f"BACKUP:{filename}"[:120]))
            try:
                await db.commit()
                break
            except IntegrityError:  # another snapshot took this predecessor first: chain onto it
                await db.rollback()
                if attempt == CHAIN_RETRIES - 1:
                    path.unlink(missing_ok=True)
                    raise
        await db.refresh(manifest)
        logger.info("BACKUP VAULT: %s sealed (%d tables, %d rows%s) by %s", filename, blob.total_tables, blob.total_rows,
                    f", truncated: {', '.join(truncated)}" if truncated else "", by)
        return manifest

    async def verify(self, db: AsyncSession, settings: Settings, snapshot_id: UUID, operation: str, *, by: str) -> BackupVerifyResponse:
        started = time.perf_counter()
        manifest = await db.get(BackupSnapshotManifestModel, snapshot_id, populate_existing=True)
        if manifest is None:
            raise ArchiveDomainError(f"Snapshot {snapshot_id} not found.")
        key = settings.ENCRYPTION_KEY.get_secret_value()
        path = Path(manifest.storage_path)
        missing_tables: list[str] = []
        missing_columns: dict[str, list[str]] = {}
        if manifest.status == BackupSnapshotStatus.PRUNED.value or not path.is_file():
            result = bvm._fail(f"The archive file is gone ({'pruned' if manifest.status == BackupSnapshotStatus.PRUNED.value else 'missing on disk'}): {manifest.filename}", "")
        else:
            result = bvm.verify_and_decrypt_snapshot_blob(ciphertext_bytes=path.read_bytes(), fernet_key=key, expected_plaintext_sha256=manifest.plaintext_sha256,
                                                          expected_ciphertext_sha256=manifest.ciphertext_sha256)
            if not result.valid and manifest.key_fingerprint != bvm.key_fingerprint(key) and "Fernet" in result.detail:
                result = bvm._fail(f"Sealed with key {manifest.key_fingerprint}, the current key is {bvm.key_fingerprint(key)}: restore needs the old ENCRYPTION_KEY.",
                                   result.ciphertext_sha256)
            if result.valid and operation == BackupAuditOperation.DRY_RUN_RESTORE.value and result.payload is not None:
                live = await self._live_schema(db)
                for table, rows in sorted(result.payload["tables"].items()):
                    if table not in live:
                        missing_tables.append(table)
                        continue
                    gaps = sorted({col for row in rows for col in row} - live[table])
                    if gaps:
                        missing_columns[table] = gaps
                if missing_tables or missing_columns:
                    result = bvm.SnapshotVerificationResult(False, "FAIL", result.tables_checked, result.rows_verified, result.plaintext_sha256, result.ciphertext_sha256,
                                                            f"Intact, but not restorable here as is: {len(missing_tables)} table(s) and {len(missing_columns)} table(s) "
                                                            f"with columns the live schema lacks.", None)
                else:
                    result = bvm.SnapshotVerificationResult(True, "PASS", result.tables_checked, result.rows_verified, result.plaintext_sha256, result.ciphertext_sha256,
                                                            f"Dry-run restore: intact, and every one of {result.tables_checked} tables and its columns exists in the "
                                                            f"live schema ({result.rows_verified} rows restorable).", None)
        duration = round((time.perf_counter() - started) * 1000.0, 2)
        now = datetime.now(UTC)
        file_ok = result.valid or bool(missing_tables or missing_columns)  # a schema gap is not damage to the file
        if manifest.status != BackupSnapshotStatus.PRUNED.value:
            manifest.status = BackupSnapshotStatus.VERIFIED.value if file_ok else BackupSnapshotStatus.CORRUPTED.value
            if file_ok:
                manifest.last_verified_at = now
        audit = BackupRestoreAuditModel(id=uuid4(), snapshot_id=manifest.id, operation=operation, verdict=result.verdict, tables_checked=result.tables_checked,
                                        rows_verified=result.rows_verified, duration_ms=Decimal(f"{duration:.2f}"), detail=result.detail[:1024], triggered_by=by[:128],
                                        created_at=now)
        db.add(audit)
        db.add(ArchiveAccessLogModel(user_id=None, action=ArchiveAction.INTEGRITY_CHECK.value, target_resource=f"{operation}:{manifest.filename}"[:120]))
        await db.commit()
        await db.refresh(manifest)
        await db.refresh(audit)
        return BackupVerifyResponse(valid=result.valid, verdict=result.verdict, operation=operation, tables_checked=result.tables_checked,  # type: ignore[arg-type]
                                    rows_verified=result.rows_verified, duration_ms=duration, plaintext_sha256=result.plaintext_sha256,
                                    ciphertext_sha256=result.ciphertext_sha256, detail=result.detail, missing_tables=missing_tables, missing_columns=missing_columns,
                                    snapshot=BackupSnapshotRead.model_validate(manifest), audit=BackupRestoreAuditRead.model_validate(audit))

    async def set_pin(self, db: AsyncSession, snapshot_id: UUID, pinned: bool) -> None:
        manifest = await db.get(BackupSnapshotManifestModel, snapshot_id, populate_existing=True)
        if manifest is None:
            raise ArchiveDomainError(f"Snapshot {snapshot_id} not found.")
        manifest.is_pinned = pinned
        await db.commit()

    async def prune(self, db: AsyncSession, retention_count: int, *, by: str) -> int:
        started = time.perf_counter()
        rows = list((await db.execute(select(BackupSnapshotManifestModel).order_by(BackupSnapshotManifestModel.created_at.desc()))).scalars())
        doomed = set(bvm.select_snapshots_for_retention_pruning([(str(m.id), m.is_pinned, m.status) for m in rows], retention_count))
        now = datetime.now(UTC)
        names = []
        for m in rows:
            if str(m.id) in doomed:
                Path(m.storage_path).unlink(missing_ok=True)
                m.status, m.pruned_at = BackupSnapshotStatus.PRUNED.value, now
                names.append(m.filename)
        detail = f"Kept the newest {retention_count} live snapshots and every pinned one; pruned {len(names)}" + (f": {', '.join(names)}" if names else ".")
        db.add(BackupRestoreAuditModel(id=uuid4(), snapshot_id=None, operation=BackupAuditOperation.PRUNE_RETENTION.value, verdict=BackupAuditVerdict.PASS.value,
                                       tables_checked=0, rows_verified=0, duration_ms=Decimal(f"{(time.perf_counter() - started) * 1000.0:.2f}"),
                                       detail=detail[:1024], triggered_by=by[:128], created_at=now))
        await db.commit()
        return len(names)
