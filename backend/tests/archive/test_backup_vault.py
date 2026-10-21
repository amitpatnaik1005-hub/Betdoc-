"""Group 87: the encrypted backup & integrity restore vault (SHIVAJI & TODAR MAL). Developer Credit: Amit Ashok Kumar Patnaik.

Sealing and verifying (tampered file, wrong key, the envelope's counts), the manifest chain (a fork is refused),
retention (pinned snapshots survive, pruned files are deleted, their manifests stay verifiable), the API lifecycle
(admin only, the signed-in admin recorded, The Archive's "last backup" following the vault), a dry-run restore that
names a table the live schema lost, truncation at the row cap, key rotation, and the measured protected bankroll.
"""

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, String, Table, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_current_admin
from app.api.v1.archive import get_backup_vault_service
from app.core.config import get_settings
from app.domain.archive import backup_vault_math as bvm
from app.models import Base, User
from app.models.backup_vault import BackupSnapshotManifestModel
from app.models.real_money import RealBankroll
from app.schemas.archive import BackupCreateRequest
from app.services.backup_vault_service import BackupVaultService

from .conftest import app as archive_app

BASE = "/api/v1/archive"
ADMIN = User(id=uuid.uuid4(), username="vault_admin", hashed_password="x", role="ADMIN")


def _as_admin(tmp_path: Path) -> None:
    archive_app.dependency_overrides[get_backup_vault_service] = lambda: BackupVaultService(backup_dir=tmp_path)
    archive_app.dependency_overrides[get_current_admin] = lambda: ADMIN


# ================================================================ the mathematics
def test_sealing_verification_and_its_failures() -> None:
    key = Fernet.generate_key().decode()
    tables = {"archive_access_logs": [{"id": "1", "action": "VIEW_TABLE"}, {"id": "2", "action": "INTEGRITY_CHECK"}]}
    blob = bvm.create_encrypted_snapshot_blob(tables_payload=tables, fernet_key=key, metadata={"trigger_mode": "MANUAL"})
    again = bvm.create_encrypted_snapshot_blob(tables_payload=tables, fernet_key=key, metadata={"trigger_mode": "MANUAL"})
    assert (blob.total_tables, blob.total_rows) == (1, 2) and blob.plaintext_sha256 == again.plaintext_sha256  # canonical JSON: same rows, same digest
    ok = bvm.verify_and_decrypt_snapshot_blob(ciphertext_bytes=blob.ciphertext_bytes, fernet_key=key, expected_plaintext_sha256=blob.plaintext_sha256,
                                              expected_ciphertext_sha256=blob.ciphertext_sha256)
    assert ok.valid and ok.verdict == "PASS" and (ok.tables_checked, ok.rows_verified) == (1, 2) and ok.payload["tables"] == tables  # type: ignore[index]
    tampered = bytearray(blob.ciphertext_bytes)
    tampered[-1] ^= 0xFF
    bad_file = bvm.verify_and_decrypt_snapshot_blob(ciphertext_bytes=bytes(tampered), fernet_key=key, expected_plaintext_sha256=blob.plaintext_sha256,
                                                    expected_ciphertext_sha256=blob.ciphertext_sha256)
    assert not bad_file.valid and "modified on disk" in bad_file.detail
    other_key = bvm.verify_and_decrypt_snapshot_blob(ciphertext_bytes=blob.ciphertext_bytes, fernet_key=Fernet.generate_key(), expected_plaintext_sha256=blob.plaintext_sha256,
                                                     expected_ciphertext_sha256=blob.ciphertext_sha256)
    assert not other_key.valid and "HMAC" in other_key.detail
    assert len(bvm.key_fingerprint(key)) == 16 and bvm.key_fingerprint(key) != bvm.key_fingerprint(Fernet.generate_key())
    assert bvm.convert_inr_to_usd(Decimal("8410"), Decimal("84.10")) == Decimal("100.00") and bvm.convert_inr_to_usd(Decimal("1"), None) is None


def test_the_chain_hash_and_retention() -> None:
    fields = {"filename": "a.betdoc.enc.gz", "plaintext_sha256": "a" * 64, "ciphertext_sha256": "b" * 64, "total_tables": 5, "total_rows": 100,
              "protected_bankroll_inr": Decimal("50000.00"), "timestamp_iso": "2026-10-21T12:00:00.000000Z"}
    h1 = bvm.compute_backup_manifest_hash(prev_hash=bvm.GENESIS_BACKUP_HASH, **fields)
    assert len(h1) == 64 and bvm.compute_backup_manifest_hash(prev_hash=h1, **fields) != h1
    assert bvm.compute_backup_manifest_hash(prev_hash=bvm.GENESIS_BACKUP_HASH, **{**fields, "total_rows": 99}) != h1
    snaps = [("s1", False, "COMPLETED"), ("s2", False, "VERIFIED"), ("s3", True, "VERIFIED"), ("s4", False, "COMPLETED"), ("s5", False, "PRUNED")]
    assert bvm.select_snapshots_for_retention_pruning(snaps, 2) == ["s4"]  # s3 is pinned, s5 already pruned


# ================================================================ the API
@pytest.mark.asyncio
async def test_create_verify_prune_lifecycle(client, seed_logs, tmp_path: Path) -> None:  # noqa: ANN001
    _as_admin(tmp_path)
    await seed_logs(3)
    assert (await client.get(f"{BASE}/overview")).json()["last_backup_at"] is None
    empty = (await client.get(f"{BASE}/backups")).json()
    assert empty["total_snapshots"] == 0 and empty["chain_intact"] and empty["current_key_fingerprint"] == bvm.key_fingerprint(get_settings().ENCRYPTION_KEY.get_secret_value())

    first = (await client.post(f"{BASE}/backups/create", json={"snapshot_label": "Pinned milestone", "is_pinned": True})).json()
    snap = first["snapshots"][0]
    assert first["total_snapshots"] == 1 and snap["prev_hash"] == bvm.GENESIS_BACKUP_HASH and snap["triggered_by"] == "vault_admin" and snap["is_complete"]
    assert snap["total_rows"] >= 3 and "storage_path" not in snap and (tmp_path / snap["filename"]).is_file()
    assert snap["protected_bankroll_inr"] is None and snap["protected_bankroll_usd"] is None  # no bankroll tables here: unmeasured
    assert (await client.get(f"{BASE}/overview")).json()["last_backup_at"] is not None  # The Archive sees the vault's snapshot

    checked = (await client.post(f"{BASE}/backups/{snap['id']}/verify", json={"operation": "DRY_RUN_RESTORE"})).json()
    assert checked["valid"] and checked["verdict"] == "PASS" and checked["snapshot"]["status"] == "VERIFIED" and checked["missing_tables"] == []
    assert checked["audit"]["triggered_by"] == "vault_admin" and "Dry-run restore" in checked["detail"]

    await client.post(f"{BASE}/backups/create", json={"snapshot_label": "Second"})
    third = (await client.post(f"{BASE}/backups/create", json={"snapshot_label": "Third"})).json()
    assert third["total_snapshots"] == 3 and third["chain_intact"]
    second = next(s for s in third["snapshots"] if s["snapshot_label"] == "Second")
    pruned = (await client.post(f"{BASE}/backups/prune", json={"retention_count": 1})).json()
    status = {s["snapshot_label"]: s["status"] for s in pruned["snapshots"]}
    assert status == {"Third": "COMPLETED", "Second": "PRUNED", "Pinned milestone": "VERIFIED"} and pruned["total_snapshots"] == 2
    assert not (tmp_path / second["filename"]).exists() and pruned["chain_intact"]  # the file is gone, the chain still verifies
    assert pruned["audits"][0]["operation"] == "PRUNE_RETENTION" and second["filename"] in pruned["audits"][0]["detail"]
    gone = (await client.post(f"{BASE}/backups/{second['id']}/verify", json={"operation": "INTEGRITY_VERIFY"})).json()
    assert not gone["valid"] and "pruned" in gone["detail"] and gone["snapshot"]["status"] == "PRUNED"
    assert (await client.post(f"{BASE}/backups/{uuid.uuid4()}/verify", json={})).status_code == 404


@pytest.mark.asyncio
async def test_writes_need_an_administrator(client, tmp_path: Path) -> None:  # noqa: ANN001
    archive_app.dependency_overrides[get_backup_vault_service] = lambda: BackupVaultService(backup_dir=tmp_path)
    assert (await client.post(f"{BASE}/backups/create", json={})).status_code == 401
    assert (await client.post(f"{BASE}/backups/prune", json={})).status_code == 401
    assert (await client.get(f"{BASE}/backups")).status_code == 200  # reading needs only the mount's sign-in
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_tampered_file_and_a_rotated_key_fail(client, seed_logs, tmp_path: Path) -> None:  # noqa: ANN001
    _as_admin(tmp_path)
    await seed_logs(2)
    snap = (await client.post(f"{BASE}/backups/create", json={})).json()["snapshots"][0]
    path = tmp_path / snap["filename"]
    data = bytearray(path.read_bytes())
    data[10] ^= 0x01
    path.write_bytes(bytes(data))
    broken = (await client.post(f"{BASE}/backups/{snap['id']}/verify", json={"operation": "INTEGRITY_VERIFY"})).json()
    assert not broken["valid"] and broken["snapshot"]["status"] == "CORRUPTED" and "modified on disk" in broken["detail"]


@pytest.mark.asyncio
async def test_rotated_key_truncation_and_a_refused_fork(session_factory: async_sessionmaker[AsyncSession], seed_logs, tmp_path: Path) -> None:  # noqa: ANN001
    settings, vault = get_settings(), BackupVaultService(backup_dir=tmp_path)
    await seed_logs(3)
    async with session_factory() as db:
        capped = await vault.create_snapshot(db, None, settings.model_copy(update={"BACKUP_MAX_ROWS_PER_TABLE": 2}), BackupCreateRequest(), by="tester")
        assert not capped.is_complete and capped.truncated_tables == ["archive_access_logs"]
        rotated = settings.model_copy(update={"ENCRYPTION_KEY": type(settings.ENCRYPTION_KEY)(Fernet.generate_key().decode())})
        result = await vault.verify(db, rotated, capped.id, "INTEGRITY_VERIFY", by="tester")
        assert not result.valid and f"Sealed with key {capped.key_fingerprint}" in result.detail
        db.add(BackupSnapshotManifestModel(
            snapshot_label="fork", filename="fork.betdoc.enc.gz", storage_path="x", encryption_algorithm="x", key_fingerprint="x" * 16, plaintext_sha256="0" * 64,
            ciphertext_sha256="0" * 64, prev_hash=capped.prev_hash, entry_hash="f" * 64, triggered_by="tester"))
        with pytest.raises(IntegrityError):  # a second child of the same predecessor
            await db.commit()


@pytest.mark.asyncio
async def test_a_dry_run_names_a_table_the_live_schema_lost(engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    gap = Table("archive_vault_gap_probe", Base.metadata, Column("id", Integer, primary_key=True), Column("note", String(16)))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(gap.create)
            await conn.execute(insert(gap), [{"id": 1, "note": "kept"}])
        vault = BackupVaultService(backup_dir=tmp_path)
        async with session_factory() as db:
            snap = await vault.create_snapshot(db, None, get_settings(), BackupCreateRequest(), by="tester")
        async with engine.begin() as conn:
            await conn.run_sync(gap.drop)
        async with session_factory() as db:
            result = await vault.verify(db, get_settings(), snap.id, "DRY_RUN_RESTORE", by="tester")
        assert not result.valid and result.missing_tables == ["archive_vault_gap_probe"] and result.snapshot.status == "VERIFIED"  # the file is fine
        assert "not restorable here" in result.detail
    finally:
        Base.metadata.remove(gap)


@pytest.mark.asyncio
async def test_the_protected_bankroll_is_measured(engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    tables = [User.__table__, RealBankroll.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    try:
        async with session_factory() as db:
            user = User(id=uuid.uuid4(), username="bankroll_owner", hashed_password="x")
            db.add(user)
            await db.flush()
            db.add(RealBankroll(user_id=user.id, cash_balance=Decimal("40000.00"), locked_exposure=Decimal("10000.00")))
            await db.commit()
            snap = await BackupVaultService(backup_dir=tmp_path).create_snapshot(db, None, get_settings(), BackupCreateRequest(), by="tester")
        assert snap.protected_bankroll_inr == Decimal("50000.00") and snap.protected_bankroll_usd is None  # no fresh USD rate: no USD figure
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=list(reversed(tables)))
