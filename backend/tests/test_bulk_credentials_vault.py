"""Group 70: the Fleet Credentials & Multi-Account Vault.

The brief's proofs first:

* the importer parses the markdown layouts a hand-written credentials file uses: the master file's
  layout (one section per bookmaker or provider, ``**Label:** value`` lines, accounts under
  sub-headings, an API-keys list, a sports list), tables, env blocks, JSON, inline pairs, fuzzy names;
* every secret is AES-256-GCM ciphertext in the database (no plaintext in any column) and opens cleanly
  for the execution engine (the session manager's own ``revealed``);
* importing the same file twice creates nothing: zero duplicates, every row "unchanged";
* the load balancer moves an order to the next account as funds are held, by priority then free funds,
  and never splits one order across accounts.

Then: the cipher itself (per-record nonces, field binding, key rotation, old Fernet tokens), the
fleet overlay (imported sports join the Odds API's coverage, the per-sport market switch and quiet
hours, currency routing), Parimatch direct injection, multi-sport score polling, the paced prober
(sanctioned APIs only, nothing logs in to a soft book), the admin API end to end with the encrypted
backup, and the Omni-Sniper logging in as the chosen account. All values here are fakes.
SQLite (and PostgreSQL when ``TEST_POSTGRES_URL`` is set); real Redis on the isolated test index.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.adapters.execution.venue import VenueConfig
from app.adapters.ingestion.odds_api_adapter import OddsApiIngestor, credits_per_call, odds_api_markets
from app.api.deps import get_current_admin, get_current_user, get_db
from app.api.v1 import parimatch_feed, vault_admin
from app.core import fleet_overlay
from app.core.config import Settings, get_settings
from app.core.security import get_password_hash
from app.core.security_vault import GCM_PREFIX, VaultCrypto, VaultDecryptionError
from app.models import User
from app.models.cfo_vault import LedgerStatus, PhantomLedger
from app.models.hive_bots import TradingBot
from app.models.omni_vault import (
    OmniFleetSource,
    VaultAccountReservation,
    VaultBookmakerAccount,
    VaultFleetConfig,
    VaultImportRun,
    VaultProviderCredential,
)
from app.models.user_bets_ledger import FixtureScore, UserPlacedBet, UserPlacedLeg
from app.services.bookmaker_gateway import BookmakerOrder
from app.services.oracle_scores import finished_after, parse_scores, score_value, sports_awaiting_scores
from app.services.session_manager import revealed
from app.services.user_pnl_tracker import leg_result_from_score
from app.services.vault import account_rotator, fleet_config, registry
from app.services.vault.account_rotator import AccountChoice, NoAccount
from app.services.vault.catalog import match_entity, split_entity_label
from app.services.vault.markdown_importer import (
    ImportSourceError,
    content_digest,
    import_parsed,
    mask_identity,
    parse_markdown,
    preview_payload,
    read_path,
)
from app.services.venue_costs import bookmaker_terms
from app.workers import vault_prober

D = Decimal
TABLES = [
    User.__table__, TradingBot.__table__, OmniFleetSource.__table__, VaultBookmakerAccount.__table__, VaultAccountReservation.__table__,
    VaultProviderCredential.__table__, VaultFleetConfig.__table__, VaultImportRun.__table__, PhantomLedger.__table__,
    UserPlacedBet.__table__, UserPlacedLeg.__table__, FixtureScore.__table__,
]
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
_MARK = "betdoc:test-vault"

# The master file's layout ("API Keys & TARGET URL (Read me).md"): a title, an API-keys list, one section
# per bookmaker with its target URL and bold labels, a second account under a sub-heading, a sports list.
# Every value is a fake.
MASTER_FILE = """# API Keys & TARGET URL (Read me)

> Keep this file private. Imported into the BetDoc Vault.

## Data Provider API Keys
- **The Odds API Key:** `oddsapi-fake-0001-aaaa`
- **The Odds API Key 2:** `oddsapi-fake-0002-bbbb`
- **Pinn API Key:** `pinnapi-fake-0003`
- **SharpAPI Key:** `sharp-fake-0004`

## Parimatch
- **Target URL:** https://parimatch.in/en/
- **Username:** `pm_fake_alpha`
- **Password:** `Fake#Pari1`
- **2FA Seed:** `JBSW Y3DP EHPK 3PXP`
- **Currency:** INR

### Parimatch Account 2
- **Username:** pm_fake_beta
- **Password:** Fake#Pari2

## 1xBet
- **Target URL:** https://1xbet.com
- **Email:** onex_fake@example.com
- **Password:** Fake#Onex1
- **Notes:** main wallet

## Stake
- **Target URL:** https://stake.com
- **Username:** stake_fake
- **Password:** Fake#Stake1
- **API Token:** stake-token-fake-01

## Pinnacle
- **Username:** pinn_fake
- **Password:** Fake#Pinn1

## Betfair
- **Username:** bf_fake
- **Password:** Fake#Bf1
- **App Key:** BFAPPKEY-FAKE-01

## Active Sports
- soccer_epl
- cricket_ipl
- basketball_nba
- La Liga, Serie A
- tennis_atp
"""
FAKE_SECRETS = [
    "oddsapi-fake-0001-aaaa", "oddsapi-fake-0002-bbbb", "pinnapi-fake-0003", "sharp-fake-0004", "pm_fake_alpha", "Fake#Pari1", "JBSWY3DPEHPK3PXP",
    "pm_fake_beta", "Fake#Pari2", "onex_fake@example.com", "Fake#Onex1", "main wallet", "stake_fake", "Fake#Stake1", "stake-token-fake-01", "pinn_fake",
    "Fake#Pinn1", "bf_fake", "Fake#Bf1", "BFAPPKEY-FAKE-01", "parimatch.in/en",
]


# ================================================================ fixtures
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(_sqlite_metadata().create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=TABLES))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(TABLES))))
        await engine.dispose()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_MARK) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_MARK, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture(autouse=True)
def _clean_overlay() -> Any:
    fleet_overlay.reset()
    yield
    fleet_overlay.reset()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={
        "ODDS_SPORT_KEYS": "soccer_epl", "ODDS_API_MARKETS": "h2h", "ODDS_MARKETS_BY_SPORT": {}, "ODDS_API_REGIONS": "uk,eu",
        "BOOKMAKER_CURRENCIES": {}, "VAULT_IMPORT_ALLOWED_DIRS": [], "omni_redis_prefix": "test_vault_omni",
    })


@pytest.fixture
def vault() -> VaultCrypto:
    return VaultCrypto(Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def admin(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions() as session:
        row = User(username=f"vault_{uuid.uuid4().hex[:6]}", hashed_password=get_password_hash("Correct-Horse-9"))
        session.add(row)
        await session.commit()
        return row


async def _import(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, settings: Settings, text_: str, redis: Redis | None = None, **kw: Any) -> Any:
    parsed = parse_markdown(text_)
    async with sessions() as session:
        report = await import_parsed(session, vault, parsed, settings, origin="text", content_sha256=content_digest(text_), redis=redis, **kw)
        await session.commit()
        await fleet_config.publish(session, redis, settings)
    return parsed, report


# ================================================================ 1. parsing
def test_the_master_file_layout_parses_completely() -> None:
    parsed = parse_markdown(MASTER_FILE)
    summary = parsed.summary()
    assert set(summary) == {"accounts_found", "providers_found", "sports_found", "syntax_warnings"}
    assert (summary["accounts_found"], summary["providers_found"]) == (6, 4)
    by = {(a.bookmaker_id, a.username): a for a in parsed.accounts}
    pm = by["parimatch", "pm_fake_alpha"]
    assert (pm.password, pm.totp_seed, pm.currency, pm.url) == ("Fake#Pari1", "JBSWY3DPEHPK3PXP", "INR", "https://parimatch.in/en/")
    assert by["parimatch", "pm_fake_beta"].password == "Fake#Pari2" and by["parimatch", "pm_fake_beta"].label == "Parimatch Account 2"
    onex = by["1xbet", "onex_fake@example.com"]
    assert (onex.password, onex.notes, onex.url) == ("Fake#Onex1", "main wallet", "https://1xbet.com")
    assert by["stake", "stake_fake"].token == "stake-token-fake-01"
    assert by["betfair", "bf_fake"].api_key == "BFAPPKEY-FAKE-01"
    assert by["pinnacle", "pinn_fake"].password == "Fake#Pinn1"
    providers = {(p.provider_id, p.api_key) for p in parsed.providers}
    assert providers == {("odds_api", "oddsapi-fake-0001-aaaa"), ("odds_api", "oddsapi-fake-0002-bbbb"), ("pinnacle_api", "pinnapi-fake-0003"), ("sharpapi", "sharp-fake-0004")}
    assert parsed.sports == ["soccer_epl", "cricket_ipl", "basketball_nba", "soccer_spain_la_liga", "soccer_italy_serie_a"]
    assert any("tennis_atp" in w and "per tournament" in w for w in summary["syntax_warnings"])  # a bare tour matches no feed
    blob = json.dumps(summary)
    assert not any(secret in blob for secret in FAKE_SECRETS)  # warnings name lines and fields, never a value


def test_tables_env_blocks_json_inline_pairs_and_fuzzy_names() -> None:
    parsed = parse_markdown("""
## Accounts
| Bookmaker | Username | Password | Currency | Max Stake |
|-----------|----------|----------|----------|-----------|
| Pari match | t_pm_1 | pw-a | | 5,000 |
| 1x-Bet | t_1x_1 | pw-b | INR | |
| Stake.com | t_st_1 | pw-c | USDT | |

```env
ODDS_API_KEY=env-odds-key-0009
BETFAIR_USERNAME=env_bf_user
BETFAIR_PASSWORD=env-bf-pass
BETFAIR_APP_KEY=env-bf-app
```

```json
{"pinnacle": {"username": "json_pin", "password": "json-pin-pw"}}
```

Betfair Exchange: user: inl_bf | pass: inl-bf-pw | app key: inl-bf-app
| Field | Value |
|---|---|
| Parimatch username | kv_pm |
| Parimatch password | kv-pm-pw |
""")
    got = {(a.bookmaker_id, a.username, a.password, a.api_key, a.currency, a.stake_cap) for a in parsed.accounts}
    assert ("parimatch", "t_pm_1", "pw-a", None, None, D("5000")) in got
    assert ("1xbet", "t_1x_1", "pw-b", None, "INR", None) in got
    assert ("stake", "t_st_1", "pw-c", None, "USDT", None) in got
    assert ("betfair", "env_bf_user", "env-bf-pass", "env-bf-app", None, None) in got
    assert ("pinnacle", "json_pin", "json-pin-pw", None, None, None) in got
    assert ("betfair", "inl_bf", "inl-bf-pw", "inl-bf-app", None, None) in got
    assert ("parimatch", "kv_pm", "kv-pm-pw", None, None, None) in got
    assert [(p.provider_id, p.api_key) for p in parsed.providers] == [("odds_api", "env-odds-key-0009")]


def test_fuzzy_matching_names_fields_and_entities() -> None:
    assert match_entity("Pari-Match accounts").key == "parimatch"  # type: ignore[union-attr]
    assert match_entity("1X BET #2").key == "1xbet"  # type: ignore[union-attr]
    assert match_entity("Pinn API keys").key == "pinnacle_api" and match_entity("Pinnacle").kind == "bookmaker"  # type: ignore[union-attr]
    assert match_entity("The-Odds-API").key == "odds_api"  # type: ignore[union-attr]
    assert match_entity("Sharp API").key == "sharpapi"  # type: ignore[union-attr]
    assert match_entity("Groceries") is None and match_entity("Bet") is None
    entity, field_name = split_entity_label("PINNACLE_USERNAME")  # type: ignore[misc]
    assert (entity.key, field_name) == ("pinnacle", "username")
    entity, field_name = split_entity_label("Odds API key 3")  # type: ignore[misc]
    assert (entity.key, field_name) == ("odds_api", "api_key")


def test_placeholders_orphans_and_duplicates_warn_but_never_show_a_value() -> None:
    parsed = parse_markdown("""
## Parimatch
Username: dup_user
Password: first-pw
## Parimatch
Username: dup_user
Password: second-pw
## Stake
Username: TBD
Password: your password here
## Random notes
Password: orphan-pw
## 1xBet
Password: no-login-pw
""")
    assert [(a.bookmaker_id, a.username, a.password) for a in parsed.accounts] == [("parimatch", "dup_user", "second-pw")]  # merged, the later value wins
    warnings = " | ".join(parsed.warnings)
    assert "placeholder" in warnings and "merged" in warnings and "names no bookmaker" in warnings and "no login" in warnings
    assert not any(v in warnings for v in ("first-pw", "second-pw", "orphan-pw", "no-login-pw", "dup_user"))


def test_the_dry_run_payload_is_counts_and_masks() -> None:
    preview = preview_payload(parse_markdown(MASTER_FILE))
    assert (preview["accounts_found"], preview["providers_found"], preview["sports_found"]) == (6, 4, 5)
    pm = next(a for a in preview["accounts"] if a["username_hint"] == mask_identity("pm_fake_alpha"))
    assert pm["username_hint"] == "pm***ha" and pm["has_password"] and pm["has_2fa"] and pm["target_host"] == "parimatch.in" and pm["adapter"] == "ParimatchAdapter"
    assert {a["currency"] for a in preview["accounts"] if a["bookmaker"] == "stake"} == {"USDT"}
    assert {a["currency"] for a in preview["accounts"] if a["bookmaker"] == "betfair"} == {"GBP"}
    assert all("…" in p["key_hint"] for p in preview["providers"])
    assert not any(secret in json.dumps(preview) for secret in FAKE_SECRETS)


def test_path_imports_only_from_allowed_directories(tmp_path: Path, settings: Settings) -> None:
    inside = tmp_path / "confidential"
    inside.mkdir()
    (inside / "keys.md").write_bytes(MASTER_FILE.encode("utf-8"))
    (tmp_path / "elsewhere.md").write_bytes(MASTER_FILE.encode("utf-8"))
    with pytest.raises(ImportSourceError, match="path imports are off"):
        read_path(str(inside / "keys.md"), settings)
    allowed = settings.model_copy(update={"VAULT_IMPORT_ALLOWED_DIRS": [str(inside)]})
    assert read_path(f'"{inside / "keys.md"}"', allowed) == MASTER_FILE
    with pytest.raises(ImportSourceError, match="outside"):
        read_path(str(inside / ".." / "elsewhere.md"), allowed)  # no climbing out
    with pytest.raises(ImportSourceError, match=r"\.md"):
        read_path(str(inside / "keys.exe"), allowed)
    assert read_path(str(tmp_path / "elsewhere.md"), settings, enforce_allowlist=False) == MASTER_FILE  # the CLI, run by the owner


# ================================================================ 2. the cipher
def test_aes_256_gcm_unique_nonces_field_binding_rotation_and_old_fernet_tokens() -> None:
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    v1 = VaultCrypto(k1)
    a, b = v1.encrypt_key("same", context="x"), v1.encrypt_key("same", context="x")
    assert a.startswith(GCM_PREFIX) and a != b  # a fresh nonce per record
    assert v1.decrypt_key(a, context="x") == "same"
    with pytest.raises(VaultDecryptionError):
        v1.decrypt_key(a, context="y")  # moved to another field or row: refused
    tampered = a[:-2] + ("A" if a[-2] != "A" else "B") + a[-1]
    with pytest.raises(VaultDecryptionError):
        v1.decrypt_key(tampered, context="x")
    legacy = Fernet(k1).encrypt(b"from-before").decode()
    assert v1.decrypt_key(legacy) == "from-before"  # pre-Group 70 Fernet still opens
    rotated = VaultCrypto(k2, [k1])
    assert rotated.decrypt_key(a, context="x") == "same" and not rotated.is_current(a)
    resealed = rotated.rotate(a, context="x")
    assert rotated.is_current(resealed) and rotated.decrypt_key(resealed, context="x") == "same"
    with pytest.raises(VaultDecryptionError, match="does not hold"):
        VaultCrypto(k2).decrypt_key(a, context="x")
    assert v1.blind_index("login") in rotated.blind_indexes("login")  # a row indexed before rotation is still found


# ================================================================ 3. encrypted at rest, opened by execution
@pytest.mark.asyncio
async def test_every_secret_is_ciphertext_in_the_database_and_opens_for_execution(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, settings: Settings) -> None:
    await _import(sessions, vault, settings, MASTER_FILE)
    async with sessions() as session:
        dump = []
        for table in ("vault_bookmaker_accounts", "vault_provider_credentials", "omni_fleet_sources", "vault_import_runs", "vault_fleet_config"):
            for row in (await session.execute(text(f"SELECT * FROM {table}"))).all():  # noqa: S608 - fixed table names
                dump.append(" ".join(str(v) for v in row))
        raw = "\n".join(dump)
        assert not [s for s in FAKE_SECRETS if s in raw]  # no plaintext anywhere, hints included
        accounts = (await session.execute(select(VaultBookmakerAccount))).scalars().all()
        assert len(accounts) == 6 and all(r.encrypted_password.startswith(GCM_PREFIX) for r in accounts if r.encrypted_password)
        betfair = next(r for r in accounts if r.bookmaker_id == "betfair")
        with registry.account_secrets(vault, betfair) as creds:
            assert (creds["username"], creds["password"], creds["api_key"]) == ("bf_fake", "Fake#Bf1", "BFAPPKEY-FAKE-01")
        venue = VenueConfig(id="betfair", display_name="Betfair", adapter="betfair", base_url="https://api.betfair.example", auth_type="betfair",
                            place_path="/p", status_path="/s", bets_per_second=D(5), burst=1)
        routed = registry.venue_for_account(venue, betfair, vault)
        assert routed.session_key == f"betfair@{betfair.id}" and routed.currency == "GBP"
        with revealed(vault, routed.encrypted_credentials) as creds:  # exactly how the execution engine opens venue credentials
            assert (creds["username"], creds["password"], creds["app_key"]) == ("bf_fake", "Fake#Bf1", "BFAPPKEY-FAKE-01")
        other = next(r for r in accounts if r.bookmaker_id == "stake")
        betfair.encrypted_password = other.encrypted_password  # a ciphertext copied across rows
        with pytest.raises(VaultDecryptionError), registry.account_secrets(vault, betfair):
            pass
        fleet = await session.get(OmniFleetSource, "odds_api")
        assert vault.decrypt_key(fleet.encrypted_api_key) == "oddsapi-fake-0001-aaaa"  # the fleet now runs on the first key
        linked = (await session.execute(select(VaultProviderCredential).where(VaultProviderCredential.linked_source_id == "odds_api"))).scalars().all()
        assert len(linked) == 1


# ================================================================ 4. idempotency
@pytest.mark.asyncio
async def test_importing_the_same_file_twice_creates_nothing_and_a_change_updates_in_place(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, settings: Settings) -> None:
    _, first = await _import(sessions, vault, settings, MASTER_FILE)
    assert (first.accounts_created, first.providers_created, first.providers_linked) == (6, 4, ["odds_api"])
    assert any("already runs on another key" in w for w in first.warnings)  # the second Odds API key never swaps the first silently
    async with sessions() as session:
        stake = (await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.bookmaker_id == "stake"))).scalars().one()
        stake.is_active, stake.priority = False, 7  # the user's own state
        await session.commit()
    _, second = await _import(sessions, vault, settings, MASTER_FILE)
    assert (second.accounts_created, second.accounts_updated, second.accounts_unchanged) == (0, 0, 6)
    assert (second.providers_created, second.providers_updated, second.providers_unchanged) == (0, 0, 4)
    assert second.sports_added == []
    _, third = await _import(sessions, vault, settings, MASTER_FILE.replace("Fake#Stake1", "Fake#Stake2"))
    assert (third.accounts_created, third.accounts_updated, third.accounts_unchanged) == (0, 1, 5)
    async with sessions() as session:
        assert (await session.execute(select(func.count()).select_from(VaultBookmakerAccount))).scalar_one() == 6
        assert (await session.execute(select(func.count()).select_from(VaultProviderCredential))).scalar_one() == 4
        stake = (await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.bookmaker_id == "stake"))).scalars().one()
        with registry.account_secrets(vault, stake) as creds:
            assert creds["password"] == "Fake#Stake2"
        assert (stake.is_active, stake.priority, stake.verification_status) == (False, 7, "UNVERIFIED")  # untouched by the file
        runs = (await session.execute(select(func.count()).select_from(VaultImportRun))).scalar_one()
        assert runs == 3
        config = await session.get(VaultFleetConfig, 1)
        assert config.sports == ["cricket_ipl", "basketball_nba", "soccer_spain_la_liga", "soccer_italy_serie_a"]  # soccer_epl was already in the environment


# ================================================================ 5. the load balancer
async def _account(session: AsyncSession, vault: VaultCrypto, book: str, priority: int, balance: str | None, *, cap: str | None = None, currency: str = "INR",
                   active: bool = True, status: str = "UNVERIFIED") -> VaultBookmakerAccount:
    row = VaultBookmakerAccount(
        id=uuid.uuid4(), bookmaker_id=book, label=f"{book} #{priority}", identity_digest=uuid.uuid4().hex, currency=currency, is_active=active,
        priority=priority, balance=None if balance is None else D(balance), stake_cap=None if cap is None else D(cap), reserved=D(0),
        secrets_fingerprint="x", verification_status=status, source="manual",
    )
    row.encrypted_password = vault.encrypt_key("pw", context=f"vault-account:{row.id}:password")
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_the_load_balancer_moves_to_the_next_account_as_funds_are_held_and_never_splits(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto) -> None:
    async with sessions() as session:
        a1 = await _account(session, vault, "1xbet", 1, "10000")
        a2 = await _account(session, vault, "1xbet", 2, "10000")
        await _account(session, vault, "1xbet", 3, "50000", active=False)
        await _account(session, vault, "1xbet", 4, "50000", status="FAILED")
        await _account(session, vault, "1xbet", 5, "90000", currency="USD")
        await session.commit()
    async with sessions() as session:
        best = await account_rotator.get_optimal_account(session, "1xbet", D("6000"), currency="INR")
        assert isinstance(best, AccountChoice) and best.account_id == a1.id  # the primary first
        picks = []
        for ref in ("o-1", "o-2", "o-3"):
            choice = await account_rotator.reserve(session, "1xbet", D("6000"), ref, currency="INR")
            picks.append(choice.account_id if isinstance(choice, AccountChoice) else choice.reason)
        await session.commit()
        assert picks == [a1.id, a2.id, "INSUFFICIENT_FUNDS"]  # 4,000 free on each: no single account carries 6,000, and it is not split
        again = await account_rotator.reserve(session, "1xbet", D("6000"), "o-1", currency="INR")
        assert isinstance(again, AccountChoice) and again.account_id == a1.id  # a retried order holds once
        small = await account_rotator.reserve(session, "1xbet", D("3000"), "o-4", currency="INR")
        assert isinstance(small, AccountChoice) and small.account_id == a1.id  # back to the primary: priority, then free funds
        await session.commit()
        assert await account_rotator.release(session, "o-2", "rejected") and not await account_rotator.release(session, "o-2", "rejected")
        await session.commit()
        reserved = dict((await session.execute(select(VaultBookmakerAccount.id, VaultBookmakerAccount.reserved).where(VaultBookmakerAccount.id.in_([a1.id, a2.id])))).all())
        assert (reserved[a1.id], reserved[a2.id]) == (D("9000"), D(0))
        usd = await account_rotator.get_optimal_account(session, "1xbet", D("60000"), currency="USD")
        assert isinstance(usd, AccountChoice) and usd.currency == "USD"
        assert (await account_rotator.get_optimal_account(session, "1xbet", D(10), currency="GBP")).reason == "CURRENCY"  # type: ignore[union-attr]
        assert (await account_rotator.get_optimal_account(session, "parimatch", D(10))).reason == "NO_ACCOUNTS"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stake_caps_unknown_balances_and_the_sweep_releasing_finished_orders(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, admin: User) -> None:
    async with sessions() as session:
        capped = await _account(session, vault, "parimatch", 1, None, cap="2500")
        open_ended = await _account(session, vault, "parimatch", 2, None)
        await session.commit()
    async with sessions() as session:
        small = await account_rotator.reserve(session, "parimatch", D("2000"), str(uuid.uuid4()))
        big = await account_rotator.reserve(session, "parimatch", D("8000"), str(settled_ref := uuid.uuid4()))
        stale_ref = str(uuid.uuid4())
        stale = await account_rotator.reserve(session, "parimatch", D("100"), stale_ref, now=datetime.now(UTC) - timedelta(hours=2))
        await session.commit()
        assert isinstance(small, AccountChoice) and small.account_id == capped.id
        assert isinstance(big, AccountChoice) and big.account_id == open_ended.id  # over the primary's own cap
        assert isinstance(stale, AccountChoice)
        session.add(PhantomLedger(
            user_id=admin.id, idempotency_key=settled_ref, fixture_id="f", market="Match Odds", selection="HOME", bookmaker_id="parimatch",
            stake_inr=D("8000"), odds=D("2"), potential_pnl=D("8000"), status=LedgerStatus.WON, settled_at=datetime.now(UTC),
        ))
        await session.commit()
    swept = await account_rotator.release_finished(sessions, timedelta(minutes=30))
    assert swept == {"settled": 1, "abandoned": 1}  # the small order is still in flight (no ledger row yet, not stale)
    async with sessions() as session:
        open_holds = (await session.execute(select(func.count()).select_from(VaultAccountReservation).where(VaultAccountReservation.released_at.is_(None)))).scalar_one()
        assert open_holds == 1


# ================================================================ 6. the fleet overlay: sports, markets, currencies
@pytest.mark.asyncio
async def test_imported_sports_join_the_coverage_and_markets_switch_per_sport_with_quiet_hours(sessions: async_sessionmaker[AsyncSession], redis: Redis, vault: VaultCrypto, settings: Settings) -> None:
    assert OddsApiIngestor.coverage(settings) == {"soccer_epl": "soccer_epl"}
    await _import(sessions, vault, settings, MASTER_FILE, redis=redis)
    assert set(OddsApiIngestor.coverage(settings)) == {"soccer_epl", "cricket_ipl", "basketball_nba", "soccer_spain_la_liga", "soccer_italy_serie_a"}
    async with sessions() as session:
        config = await fleet_config.get_config(session)
        config.markets_by_sport = {"soccer_epl": "h2h,spreads,totals", "*": "h2h"}
        config.quiet_start, config.quiet_end, config.timezone = "01:00", "07:00", "Asia/Kolkata"
        await fleet_config.bump(session, None)
        await session.commit()
        await fleet_config.publish(session, redis, settings)
    day = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)  # 15:30 in Kolkata
    night = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)  # 02:30 in Kolkata
    assert odds_api_markets(settings, "soccer_epl", day) == "h2h,spreads,totals" and credits_per_call(settings, "soccer_epl", day) == 6
    assert odds_api_markets(settings, "cricket_ipl", day) == "h2h" and credits_per_call(settings, "cricket_ipl", day) == 2
    assert odds_api_markets(settings, "soccer_epl", night) == "h2h"  # quiet hours conserve the quota
    assert odds_api_markets(settings.model_copy(update={"ODDS_MARKETS_BY_SPORT": {"cricket_ipl": "h2h,totals"}}), "cricket_ipl", day) == "h2h"  # the runtime "*" wins
    # another process sees the same configuration through Redis
    fleet_overlay.reset()
    await fleet_config.refresh(redis, settings, None, force=True)
    assert "cricket_ipl" in settings.odds_sport_keys and fleet_overlay.current().markets_by_sport["soccer_epl"] == "h2h,spreads,totals"
    # once the Odds API's free index is known, a sport it does not offer stops being polled
    await fleet_config.remember_offered_sports(redis, settings, {"soccer_epl", "cricket_ipl", "basketball_nba", "soccer_italy_serie_a"})
    async with sessions() as session:
        await fleet_config.publish(session, redis, settings)
    assert "soccer_spain_la_liga" not in settings.odds_sport_keys and "cricket_ipl" in settings.odds_sport_keys


@pytest.mark.asyncio
async def test_imported_account_currencies_route_into_bookmaker_terms(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, settings: Settings) -> None:
    assert bookmaker_terms("stake", settings).currency == "INR"  # an unknown book, before the import
    await _import(sessions, vault, settings, MASTER_FILE)
    assert {b: bookmaker_terms(b, settings).currency for b in ("parimatch", "1xbet", "stake", "betfair")} == {"parimatch": "INR", "1xbet": "INR", "stake": "USDT", "betfair": "GBP"}
    assert bookmaker_terms("stake", settings.model_copy(update={"BOOKMAKER_CURRENCIES": {"stake": "USD"}})).currency == "USD"  # the environment is explicit: it wins


# ================================================================ 7. Parimatch direct injection
def _feed_app(redis: Redis | None, settings: Settings, user: User | None, vault: VaultCrypto | None = None, sessions: Any = None) -> FastAPI:
    app = FastAPI()
    app.include_router(parimatch_feed.router, prefix="/api/v1")
    app.include_router(vault_admin.router, prefix="/api/v1")
    app.state.redis, app.state.vault = redis, vault
    app.dependency_overrides[get_settings] = lambda: settings
    if user is not None:
        app.dependency_overrides[get_current_admin] = lambda: user
        app.dependency_overrides[get_current_user] = lambda: user
    if sessions is not None:
        async def db() -> AsyncIterator[AsyncSession]:
            async with sessions() as session:
                yield session
        app.dependency_overrides[get_db] = db
    return app


@pytest.mark.asyncio
async def test_parimatch_prices_are_normalised_into_the_quote_stream(monkeypatch: pytest.MonkeyPatch, settings: Settings, admin: User) -> None:
    published: list[Any] = []

    async def capture(_redis: Any, frames: Any, _settings: Any) -> bool:
        published.extend(frames)
        return True

    monkeypatch.setattr(parimatch_feed, "publish_market_quotes", capture)

    class _Redis:
        async def hset(self, *_: Any, **__: Any) -> None:
            return None

    kickoff = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    body = {"events": [{"home": "Arsenal", "away": "Chelsea", "sport_key": "soccer_epl", "kickoff": kickoff, "markets": [
        {"market": "1X2", "prices": {"1": "2.10", "X": "3.40", "2": "3.60"}},
        {"market": "Total 2.5", "prices": {"Over 2.5": "1.95", "Under 2.5": "1.90"}},
        {"market": "Both Teams To Score", "prices": {"Yes": "1.80", "No": "2.00"}},
        {"market": "Asian Handicap (-0.5)", "prices": {"Arsenal": "2.05", "Chelsea": "1.85"}},
        {"market": "Correct Score", "prices": {"1-0": "7.5", "0-1": "9"}},
        {"market": "1X2", "prices": {"1": "0.5", "2": "2"}},
    ]}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_feed_app(_Redis(), settings, admin)), base_url="http://t") as client:  # type: ignore[arg-type]
        response = await client.post("/api/v1/parimatch/odds", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["frames"] == 4 and sorted(result["markets"]) == ["Asian Handicap -0.5", "BTTS", "Match Odds", "Totals 2.5"]
        assert [r["reason"][:14] for r in result["rejected"]] == ["unknown market", "odds 0.5 for H"]
        frames = {f.market_type: f for f in published}
        assert {k: str(v) for k, v in frames["Match Odds"].books[0].prices.items()} == {"HOME": "2.10", "DRAW": "3.40", "AWAY": "3.60"}
        assert set(frames["Totals 2.5"].books[0].prices) == {"OVER", "UNDER"} and set(frames["Asian Handicap -0.5"].books[0].prices) == {"HOME", "AWAY"}
        assert all(f.source == "parimatch_direct" and f.books[0].bookmaker_id == "parimatch" for f in published)
        assert len({f.match_id for f in published}) == 1  # the same canonical fixture id the odds normaliser gives the match
        stale = body | {"observed_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat()}
        assert (await client.post("/api/v1/parimatch/odds", json=stale)).status_code == 422
    webhook_settings = settings.model_copy(update={"PARIMATCH_FEED_TOKEN": None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_feed_app(_Redis(), webhook_settings, None)), base_url="http://t") as client:  # type: ignore[arg-type]
        assert (await client.post("/api/v1/parimatch/webhook", json=body)).status_code == 404  # off without a token
    from pydantic import SecretStr  # noqa: PLC0415

    token_settings = settings.model_copy(update={"PARIMATCH_FEED_TOKEN": SecretStr("feed-token-fake-123")})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_feed_app(_Redis(), token_settings, None)), base_url="http://t") as client:  # type: ignore[arg-type]
        assert (await client.post("/api/v1/parimatch/webhook", json=body, headers={"X-Parimatch-Feed-Token": "wrong"})).status_code == 401
        assert (await client.post("/api/v1/parimatch/webhook", json=body, headers={"X-Parimatch-Feed-Token": "feed-token-fake-123"})).status_code == 200


# ================================================================ 8. scores for every sport
@pytest.mark.asyncio
async def test_scores_poll_every_sport_by_its_own_length_and_read_cricket_runs(sessions: async_sessionmaker[AsyncSession], admin: User) -> None:
    assert (score_value(2), score_value("187/6"), score_value("187/6 (20)"), score_value("3")) == (2, 187, 187, 3)
    events = [{"completed": True, "home_team": "Mumbai Indians", "away_team": "Chennai Super Kings", "commence_time": "2026-10-09T14:00:00Z",
               "scores": [{"name": "Mumbai Indians", "score": "187/6"}, {"name": "Chennai Super Kings", "score": "180/9"}]}]
    parsed = parse_scores(events, "cricket_ipl")
    assert (parsed[0].home_goals, parsed[0].away_goals) == (187, 180)
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    async with sessions() as session:
        bet = UserPlacedBet(user_id=admin.id, bookmaker="PARIMATCH", structure="SINGLE", stake_inr=D(100), status="PENDING", source="manual", placed_at=now - timedelta(days=1))
        session.add(bet)
        await session.flush()
        for i, (sport, hours) in enumerate((("soccer_epl", 2.0), ("cricket_ipl", 3.0), ("cricket_odi", 5.0), ("basketball_nba", 3.0), ("cricket_test_match", 30.0))):
            session.add(UserPlacedLeg(bet_id=bet.id, position=i, fixture_id=f"fx-{i}", home="H", away="A", sport_key=sport, kickoff=now - timedelta(hours=hours),
                                      market="Match Odds", selection="HOME", odds=D(2), result="PENDING"))
        await session.commit()
        due = await sports_awaiting_scores(session, now)
    assert due == {"soccer_epl", "basketball_nba"}  # T20 not yet 4 h, the ODI not yet 9 h, a Test match is left to the user
    assert finished_after("cricket_ipl") == timedelta(hours=4) and finished_after("tennis_atp_us_open") == timedelta(hours=3)
    tie = FixtureScore(fixture_id="t", home="H", away="A", home_goals=1, away_goals=1, status="FINAL")
    tennis = UserPlacedLeg(fixture_id="t", home="H", away="A", sport_key="tennis_atp_us_open", market="Match Odds", selection="HOME", odds=D(2), result="PENDING")
    football = UserPlacedLeg(fixture_id="t", home="H", away="A", sport_key="soccer_epl", market="Match Odds", selection="HOME", odds=D(2), result="PENDING")
    assert leg_result_from_score(tennis, tie, now) is None  # a two-way tie: the book's rules, the user settles it
    assert leg_result_from_score(football, tie, now).value == "LOST"  # type: ignore[union-attr]


# ================================================================ 9. the prober
@pytest.mark.asyncio
async def test_the_prober_uses_sanctioned_apis_only_paces_itself_and_never_retries_a_failure(sessions: async_sessionmaker[AsyncSession], redis: Redis, vault: VaultCrypto, settings: Settings) -> None:
    await _import(sessions, vault, settings, MASTER_FILE + "\n## Betfair\n- **Username:** bf_fake_two\n- **Password:** Fake#Bf2\n- **App Key:** BFAPPKEY-FAKE-02\n")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.url.path.endswith("/sports"):
            return httpx.Response(200, json=[{"key": "soccer_epl"}, {"key": "cricket_ipl"}], headers={"x-requests-remaining": "480", "x-requests-used": "20"})
        if request.url.path.endswith("/login"):
            ok = b"Fake%23Bf1" in request.content
            return httpx.Response(200, json={"token": "t", "status": "SUCCESS"} if ok else {"status": "FAIL", "error": "INVALID_USERNAME_OR_PASSWORD"})
        if request.url.path.endswith("/logout"):
            return httpx.Response(200, json={"status": "SUCCESS"})
        if request.url.path.endswith("/client/balance"):
            return httpx.Response(403)
        return httpx.Response(599)

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if seconds >= 30:  # waiting out a bookmaker's minute: let it pass
            for key in [k async for k in redis.scan_iter(f"{settings.omni_redis_prefix}:vault:probe:*")]:
                await redis.delete(key)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        summary = await vault_prober.probe_all(sessions, redis, settings, vault, http=http, sleep=sleep)
    hosts = {c.split()[1].split("/")[0] for c in calls}
    assert hosts <= {"api.the-odds-api.com", "identitysso.betfair.com", "api.pinnacle.com"}  # never parimatch / 1xbet / stake
    assert sum(c.endswith("/sports") for c in calls) == 2  # each Odds API key once: GET /sports costs nothing
    assert sum(c.endswith("/login") for c in calls) == 2 and sum(c.endswith("/logout") for c in calls) == 1  # a session opened is closed
    spacing = [s for s in sleeps if s < 30]
    assert spacing and all(3.0 <= s <= 7.0 for s in spacing)  # a random 3-7 s between network checks
    assert any(s >= 30 for s in sleeps)  # the second Betfair check waited for its bookmaker's minute
    assert summary["ok"] >= 3 and summary["failed"] == 2 and summary["unsupported"] >= 5  # betfair #2 and pinnacle (403) failed; soft books, 2FA, providers unsupported
    async with sessions() as session:
        rows = {(r.bookmaker_id, r.username_hint): r for r in (await session.execute(select(VaultBookmakerAccount))).scalars()}
        assert rows["parimatch", mask_identity("pm_fake_alpha")].verification_status == "UNSUPPORTED"
        assert rows["betfair", mask_identity("bf_fake")].verification_status == "OK" and rows["betfair", mask_identity("bf_fake")].last_verified_at is not None
        assert rows["betfair", mask_identity("bf_fake_two")].verification_status == "FAILED"
    assert await redis.sismember(fleet_config.offered_key(settings), "cricket_ipl")
    calls.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        again = await vault_prober.probe_all(sessions, redis, settings, vault, http=http, sleep=sleep)
    assert sum(c.endswith("/login") for c in calls) == 1 and again["skipped"] == 2  # a failed login is not retried until it changes


# ================================================================ 10. the admin API, the backup
@pytest.mark.asyncio
async def test_the_vault_api_end_to_end_with_masking_toggles_routing_and_the_encrypted_backup(sessions: async_sessionmaker[AsyncSession], redis: Redis, vault: VaultCrypto, settings: Settings, admin: User, tmp_path: Path) -> None:
    folder = tmp_path / "confidential"
    folder.mkdir()
    (folder / "API Keys & TARGET URL (Read me).md").write_bytes(MASTER_FILE.encode("utf-8"))
    allowed = settings.model_copy(update={"VAULT_IMPORT_ALLOWED_DIRS": [str(folder)]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_feed_app(redis, allowed, admin, vault, sessions)), base_url="http://t") as client:
        preview = (await client.post("/api/v1/vault/import-preview", data={"text": MASTER_FILE})).json()
        assert (preview["accounts_found"], preview["providers_found"], preview["sports_found"]) == (6, 4, 5) and preview["changes"]["accounts_created"] == 6
        assert (await client.get("/api/v1/vault/accounts")).json() == []  # the preview wrote nothing
        assert (await client.post("/api/v1/vault/import-markdown", data={"text": MASTER_FILE, "path": "x"})).status_code == 422  # one source only
        bad_path = await client.post("/api/v1/vault/import-markdown", data={"path": str(tmp_path / "nope.md")})
        assert bad_path.status_code == 422 and "outside" in bad_path.json()["detail"]
        done = await client.post("/api/v1/vault/import-markdown", data={"path": str(folder / "API Keys & TARGET URL (Read me).md")})
        assert done.status_code == 200 and done.json()["report"]["accounts_created"] == 6
        upload = await client.post("/api/v1/vault/import-markdown", files={"file": ("keys.md", MASTER_FILE.encode(), "text/markdown")})
        assert upload.json()["report"]["accounts_unchanged"] == 6
        accounts = (await client.get("/api/v1/vault/accounts")).json()
        body = json.dumps(accounts) + json.dumps((await client.get("/api/v1/vault/providers")).json()) + json.dumps((await client.get("/api/v1/vault/imports")).json())
        assert not [s for s in FAKE_SECRETS if s in body]
        pm = next(a for a in accounts if a["username_hint"] == "pm***ha")
        assert pm["has_password"] and pm["has_2fa"] and pm["currency"] == "INR" and pm["adapter"] == "ParimatchAdapter"
        toggled = (await client.patch(f"/api/v1/vault/accounts/{pm['id']}/toggle")).json()
        assert toggled["is_active"] is False
        edited = await client.patch(f"/api/v1/vault/accounts/{pm['id']}", json={"balance": "5000", "is_active": True, "secrets": {"password": "Fake#Pari9"}})
        assert edited.status_code == 200 and edited.json()["balance"] == "5000" and edited.json()["verification_status"] == "UNVERIFIED"
        route = (await client.get("/api/v1/vault/accounts/route", params={"bookmaker": "Pari Match", "stake": "6000"})).json()
        assert route["account"]["label"] == "Parimatch Account 2"  # the primary has 5,000: the next account carries it
        added = await client.post("/api/v1/vault/accounts", json={"bookmaker": "1xbet", "username": "onex_fake@example.com", "password": "x"})
        assert added.status_code == 409  # already in the Vault
        config = await client.put("/api/v1/vault/fleet-config", json={"markets_by_sport": {"soccer_epl": "full"}, "quiet_start": "01:00", "quiet_end": "06:00", "account_routing": True})
        assert config.status_code == 200 and config.json()["markets_by_sport"] == {"soccer_epl": "h2h,spreads,totals"} and config.json()["account_routing"]
        assert (await client.put("/api/v1/vault/fleet-config", json={"markets_by_sport": {"soccer_epl": "btts"}})).status_code == 422
        state = (await client.get("/api/v1/vault/status")).json()
        sports = {s["key"]: s for s in state["sports"]}
        assert sports["cricket_ipl"]["source"] == "vault" and sports["cricket_ipl"]["polling"] and state["account_routing"]
        assert state["currencies"]["stake"] == {"currency": "USDT", "fx_ok": False, "note": "no live USDT/INR rate: Stake prices are refused until one is published"}
        assert (await client.post("/api/v1/vault/export-backup", json={"passphrase": "a long passphrase", "current_password": "wrong"})).status_code == 403
        backup = (await client.post("/api/v1/vault/export-backup", json={"passphrase": "a long passphrase", "current_password": "Correct-Horse-9"})).json()
        assert backup["cipher"] == "AES-256-GCM" and backup["counts"] == {"accounts": 6, "providers": 4, "sports": 4}
        assert not [s for s in FAKE_SECRETS + ["Fake#Pari9"] if s in json.dumps(backup)]
        assert (await client.post("/api/v1/vault/restore-backup", json={"backup": backup, "passphrase": "the wrong passphrase"})).status_code == 422
        held = await client.delete(f"/api/v1/vault/accounts/{pm['id']}")
        assert held.status_code == 204
    # disaster recovery: a fresh vault under a different master key
    other_vault = VaultCrypto(Fernet.generate_key().decode())
    async with sessions() as session:
        for model in (VaultAccountReservation, VaultBookmakerAccount, VaultProviderCredential, VaultFleetConfig, OmniFleetSource):
            for row in (await session.execute(select(model))).scalars():
                await session.delete(row)
        await session.commit()
        report = await registry.restore_backup(session, other_vault, backup, "a long passphrase", settings, admin.id)
        await session.commit()
        assert report["accounts_created"] == 6 and report["providers_created"] == 4
        again = await registry.restore_backup(session, other_vault, backup, "a long passphrase", settings, admin.id)
        assert again["accounts_unchanged"] == 6  # idempotent
        pm_row = next(r for r in (await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.bookmaker_id == "parimatch"))).scalars() if r.username_hint == "pm***ha")
        with registry.account_secrets(other_vault, pm_row) as creds:
            assert creds["password"] == "Fake#Pari9"
        assert (await session.get(VaultFleetConfig, 1)).account_routing is True


@pytest.mark.asyncio
async def test_rotating_the_master_key_reseals_everything(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    await _import(sessions, VaultCrypto(old_key), settings, MASTER_FILE)
    promoted = VaultCrypto(new_key, [old_key])
    async with sessions() as session:
        counts = await registry.rotate_all(session, promoted)
        await session.commit()
    assert counts == {"accounts": 6, "providers": 4, "fleet_sources": 1}
    only_new = VaultCrypto(new_key)
    async with sessions() as session:
        for row in (await session.execute(select(VaultBookmakerAccount))).scalars():
            with registry.account_secrets(only_new, row):
                pass  # every field opens without the old key
    _, report = await _import(sessions, only_new, settings, MASTER_FILE)
    assert report.accounts_unchanged == 6  # the logins were re-indexed: still found, still no duplicates


# ================================================================ 11. the Omni-Sniper logs in as the chosen account
@pytest.mark.asyncio
async def test_the_sniper_logs_in_as_the_account_the_rotator_picks(sessions: async_sessionmaker[AsyncSession], redis: Redis, vault: VaultCrypto, settings: Settings) -> None:
    from app.services.sniper import SniperGateway  # noqa: PLC0415

    await _import(sessions, vault, settings, MASTER_FILE, redis=redis)
    venue = VenueConfig(id="1xbet", display_name="1xBet", adapter="generic_rest", base_url="https://api.example", auth_type="static_bearer",
                        place_path="/p", status_path="/s", bets_per_second=D(5), burst=1)
    order = BookmakerOrder(client_ref=str(uuid.uuid4()), bookmaker_id="onexbet", fixture_id="f", market="Match Odds", selection="HOME", odds=D(2),
                           stake_inr=D(500), min_acceptable_odds=D("1.9"))
    async with httpx.AsyncClient() as http:
        gateway = SniperGateway(sessions, redis, settings, vault, http)
        assert await gateway._vault_account(order, venue) is None  # routing off: the venue's own login
        async with sessions() as session:
            config = await fleet_config.get_config(session)
            config.account_routing = True
            await fleet_config.bump(session, None)
            await session.commit()
            await fleet_config.publish(session, redis, settings)
        routed = await gateway._vault_account(order, venue)
        assert isinstance(routed, VenueConfig) and routed.session_scope is not None
        with revealed(vault, routed.encrypted_credentials) as creds:
            assert (creds["username"], creds["password"]) == ("onex_fake@example.com", "Fake#Onex1")
        async with sessions() as session:
            hold = (await session.execute(select(VaultAccountReservation).where(VaultAccountReservation.order_ref == order.client_ref))).scalars().one()
            assert hold.amount == D(500) and hold.released_at is None
        await gateway._release_vault_hold(order, "rejected")
        usd = BookmakerOrder(client_ref=str(uuid.uuid4()), bookmaker_id="1xbet", fixture_id="f", market="Match Odds", selection="HOME", odds=D(2),
                             stake_inr=D(500), min_acceptable_odds=D("1.9"), currency="USD", stake=D(6))
        refused = await gateway._vault_account(usd, venue)
        assert not isinstance(refused, VenueConfig) and refused.reason == "NO_VAULT_ACCOUNT_CURRENCY"  # type: ignore[union-attr]
    async with sessions() as session:
        hold = (await session.execute(select(VaultAccountReservation).where(VaultAccountReservation.order_ref == order.client_ref))).scalars().one()
        assert hold.released_at is not None and hold.release_reason == "rejected"
    assert isinstance(NoAccount("X", "y"), NoAccount)
