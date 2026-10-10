"""Group 70, finished in Group 71: the importer reads the owner's real credentials file.

``D:\\confidential\\API Keys & TARGET URL (Read me).md`` is a numbered list of ``**N.) <Name> API Key :- <key>**``
and ``**N.) Target URL (<Name>) :- <url>**`` lines. Re-running the importer on it must capture every
bookmaker target URL as an active account on that bookmaker's adapter, keep every API key (generic
providers included, none dropped for want of an alias), and import idempotently. The real-file tests
skip on a machine without the file; every assertion compares counts, ids and booleans, so a failure can
never print a secret. The synthetic tests prove each rule with fakes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import DEFAULT_VAULT_IMPORT_DIRS, PROJECT_ROOT, Settings, get_settings
from app.core.security_vault import VaultCrypto
from app.models import User
from app.models.omni_vault import OmniFleetSource, VaultBookmakerAccount, VaultFleetConfig, VaultImportRun, VaultProviderCredential
from app.services.vault import catalog
from app.services.vault.markdown_importer import content_digest, import_parsed, parse_markdown, preview_payload, read_path

MASTER = Path(r"D:\confidential\API Keys & TARGET URL (Read me).md")
needs_master = pytest.mark.skipif(not MASTER.is_file(), reason="the owner's credentials file is not on this machine")
TABLES = [User.__table__, OmniFleetSource.__table__, VaultBookmakerAccount.__table__, VaultProviderCredential.__table__, VaultFleetConfig.__table__, VaultImportRun.__table__]

# The real file's layout, with fakes: numbered bold lines, ":-", &#x20; padding, escaped underscores
LAYOUT = """#### &#x20;      **API Keys \\& TARGET URL**

**CATEGORY-A :-**

**"BOOKMAKERS API's" :-**

**1.) Odds API Key :- fakeoddskey0000000000000000000001**

**2.) Pinn API Key :- fakepinnkey0000000000000000000002**

**3.) Target URL (Betfair Exchange) :- https://www.betfair.com/exchange/plus/**

**4.) Target URL (Stake) :- https://stake.com/sports**

**5.) Target URL (Parimatch) :- https://parimatch.example/en/**

**6.) Target URL (1XBet) :- https://indian.1xbet.example/en**

**7.) Oddspapi.io API Key :- fakeoddspapi000000000000000000003**

**8.) SerpAPI Key :- fakeserp\\_0000000000000000000000004**

**9.) Sports-API Key :- fakesportsapi00000000000000000005**

**10.) Rapid API :- (same key as above, in Rapid API)**

**11.) Kraken Pro API Key :- fakekrakenkey000000000000000000006, fakekrakensecret0000000000000000007 (Private Key)**

&#x20;                 **\\*All Providers (List of RapidAPI) :- see below**

**"ESPORTS API's"**

**1.) Target URL (OpenDota) :- https://api.opendota.example**

**2.) Exa API Key :- fakeexa0000000000000000000000008**
"""
FAKES = ["fakeoddskey0000000000000000000001", "fakepinnkey0000000000000000000002", "fakeoddspapi000000000000000000003", "fakeserp_0000000000000000000000004",
         "fakesportsapi00000000000000000005", "fakekrakenkey000000000000000000006", "fakekrakensecret0000000000000000007", "fakeexa0000000000000000000000008"]


def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(_sqlite_metadata().create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"ODDS_SPORT_KEYS": "soccer_epl", "BOOKMAKER_CURRENCIES": {}, "omni_redis_prefix": "test_vault_master"})


@pytest.fixture
def vault() -> VaultCrypto:
    return VaultCrypto(Fernet.generate_key().decode())


async def _import(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, settings: Settings, text: str) -> object:
    async with sessions() as session:
        report = await import_parsed(session, vault, parse_markdown(text), settings, origin="path", content_sha256=content_digest(text))
        await session.commit()
    return report


# ================================================================ the rules, on fakes
def test_the_real_layout_parses_target_urls_generic_keys_and_the_colon_dash_separator() -> None:
    parsed = parse_markdown(LAYOUT)
    accounts = {a.bookmaker_id: a for a in parsed.accounts}
    assert set(accounts) == {"betfair", "stake", "parimatch", "1xbet"}
    assert {b: a.target_host for b, a in accounts.items()} == {
        "betfair": "www.betfair.com", "stake": "stake.com", "parimatch": "parimatch.example", "1xbet": "indian.1xbet.example"}
    assert all(a.url_only and a.identity == f"url:{a.target_host}" for a in accounts.values())
    providers = {p.provider_id: p for p in parsed.providers}
    assert providers["odds_api"].api_key == FAKES[0] and providers["pinnacle_api"].api_key == FAKES[1]  # known aliases, "-" never read as the key
    assert providers["oddspapi_io"].generic and providers["oddspapi_io"].label == "Oddspapi.io"
    assert providers["serpapi"].api_key == "fakeserp_0000000000000000000000004"  # markdown's "\\_" is the key's "_"
    assert providers["sports_api"].label == "Sports-API" and providers["exa"].api_key == FAKES[7]  # not swallowed by the "x api key" field
    assert (providers["kraken_pro"].api_key, providers["kraken_pro"].secret) == (FAKES[5], FAKES[6])  # key, secret on one line
    assert "rapidapi" not in providers  # prose, not a key: skipped with a note
    notes = " | ".join(parsed.warnings)
    assert "reads as text, not a key" in notes and "1 target URL(s) name no bookmaker (Opendota)" in notes
    assert [e.host for e in parsed.endpoints] == ["api.opendota.example"]
    payload = json.dumps(preview_payload(parsed))
    assert not any(fake in payload or fake in notes for fake in FAKES)


def test_a_target_url_joins_the_books_credentialed_account_in_the_same_file() -> None:
    parsed = parse_markdown("**1.) Target URL (Parimatch) :- https://pm.example**\n\n## Parimatch\n- Username: pm_fake\n- Password: Fake#1\n")
    assert [(a.bookmaker_id, a.username, a.target_host) for a in parsed.accounts] == [("parimatch", "pm_fake", "pm.example")]


def test_catalog_helpers() -> None:
    assert catalog.target_url_name("**5.) Target URL (Parimatch)") == "parimatch"
    assert catalog.normalise("**12.) Exa API Key") == "exa api key" and catalog.normalise("1xBet") == "1xbet" and catalog.normalise("2FA seed") == "2fa seed"
    assert catalog.generic_provider("**14.) Sportsmonks API Key") == catalog.GenericProvider("sportmonks", "Sportmonks", True)  # fuzzy to the known one
    assert catalog.generic_provider("NewsAPI.org API Key") == catalog.GenericProvider("newsapi_org", "NewsAPI.org", False)
    assert catalog.generic_provider("Stake API Key") is None and catalog.generic_provider("API Key") is None  # a book's own key; no name


def test_the_allowed_import_folders_default_to_confidential_and_the_project_root(tmp_path: Path) -> None:
    assert get_settings().model_fields["VAULT_IMPORT_ALLOWED_DIRS"].default_factory() == [r"D:\confidential", str(PROJECT_ROOT)]  # type: ignore[misc]
    assert DEFAULT_VAULT_IMPORT_DIRS[1] == str(Path(__file__).resolve().parents[2])
    inside = PROJECT_ROOT / "claude_code_handoff.md"
    if inside.is_file():
        assert read_path(str(inside), Settings.model_construct(VAULT_IMPORT_ALLOWED_DIRS=list(DEFAULT_VAULT_IMPORT_DIRS), VAULT_IMPORT_MAX_BYTES=10_000_000))


async def test_target_url_accounts_import_active_on_their_adapter_and_a_login_claims_them(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, settings: Settings) -> None:
    first = await _import(sessions, vault, settings, LAYOUT)
    assert (first.accounts_created, first.providers_created) == (4, 7)  # type: ignore[attr-defined]
    async with sessions() as session:
        rows = {r.bookmaker_id: r for r in (await session.execute(select(VaultBookmakerAccount))).scalars()}
        providers = list((await session.execute(select(VaultProviderCredential))).scalars())
    assert all(r.is_active and r.encrypted_target_url and r.adapter_key == catalog.adapter_key(b) for b, r in rows.items())
    assert rows["parimatch"].adapter_key == "ParimatchAdapter" and rows["parimatch"].target_host == "parimatch.example"
    assert {p.provider_id for p in providers} >= {"odds_api", "pinnacle_api", "oddspapi_io", "serpapi", "sports_api", "kraken_pro", "exa"}
    assert not any(fake in (p.encrypted_api_key or "") for p in providers for fake in FAKES)  # ciphertext only
    again = await _import(sessions, vault, settings, LAYOUT)
    assert (again.accounts_created, again.accounts_updated, again.providers_created, again.providers_updated) == (0, 0, 0, 0)  # type: ignore[attr-defined]
    login = await _import(sessions, vault, settings, "## Parimatch\n- Target URL: https://parimatch.example/en/\n- Username: pm_fake\n- Password: Fake#1\n")
    assert (login.accounts_created, login.accounts_updated) == (0, 1)  # type: ignore[attr-defined] - the URL-only row gained its login
    async with sessions() as session:
        pm = list((await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.bookmaker_id == "parimatch"))).scalars())
    assert len(pm) == 1 and pm[0].encrypted_username is not None and pm[0].username_hint == "pm***ke"
    after = await _import(sessions, vault, settings, LAYOUT)  # the old target-URL line still finds that row
    assert (after.accounts_created, after.accounts_updated) == (0, 0)  # type: ignore[attr-defined]


# ================================================================ the owner's real file
@needs_master
def test_rerunning_the_importer_on_the_master_file_captures_every_target_url_and_key() -> None:
    parsed = parse_markdown(read_path(str(MASTER), Settings.model_construct(VAULT_IMPORT_ALLOWED_DIRS=list(DEFAULT_VAULT_IMPORT_DIRS), VAULT_IMPORT_MAX_BYTES=1_000_000)))
    books = {a.bookmaker_id: a for a in parsed.accounts}
    assert {"betfair", "stake", "parimatch", "1xbet"} <= set(books)
    assert all(books[b].target_host for b in ("betfair", "stake", "parimatch", "1xbet"))
    assert [b for b in ("betfair", "stake", "parimatch", "1xbet") if catalog.adapter_key(b) is None] == []
    ids = [p.provider_id for p in parsed.providers]
    assert {"odds_api", "pinnacle_api", "sharpapi", "sportmonks", "football_data", "sportradar", "rapidapi"} <= set(ids)
    assert len(ids) >= 50 and sum(p.generic for p in parsed.providers) >= 30  # names with no alias are kept, not dropped
    malformed = [p.provider_id for p in parsed.providers if not p.api_key or p.api_key in ("-", ":", "**") or " " in p.api_key]
    assert malformed == []  # the ":-" separator never becomes the key
    secrets = [s for p in parsed.providers for s in (p.api_key, p.secret) if s and len(s) >= 6]
    exposed = json.dumps(preview_payload(parsed)) + " | ".join(parsed.warnings)
    assert sum(s in exposed for s in secrets) == 0  # the preview and the notes carry no value
    assert len(parsed.endpoints) >= 20  # open APIs reported, not stored


@needs_master
async def test_importing_the_master_file_is_complete_encrypted_and_idempotent(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, settings: Settings) -> None:
    text = read_path(str(MASTER), Settings.model_construct(VAULT_IMPORT_ALLOWED_DIRS=list(DEFAULT_VAULT_IMPORT_DIRS), VAULT_IMPORT_MAX_BYTES=1_000_000))
    parsed = parse_markdown(text)
    first = await _import(sessions, vault, settings, text)
    assert first.accounts_created == len(parsed.accounts) and first.providers_created == len(parsed.providers)  # type: ignore[attr-defined]
    second = await _import(sessions, vault, settings, text)
    assert (second.accounts_created, second.accounts_updated, second.providers_created, second.providers_updated) == (0, 0, 0, 0)  # type: ignore[attr-defined]
    async with sessions() as session:
        accounts = list((await session.execute(select(VaultBookmakerAccount))).scalars())
        providers = list((await session.execute(select(VaultProviderCredential))).scalars())
    assert all(a.is_active and a.encrypted_target_url and a.target_host for a in accounts)
    secrets = [s for p in parsed.providers for s in (p.api_key, p.secret) if s and len(s) >= 6]
    stored = " ".join(filter(None, [c for p in providers for c in (p.encrypted_api_key, p.encrypted_secret, p.api_key_hint, p.label)]))
    assert sum(s in stored for s in secrets) == 0  # ciphertext and pre-masked hints only
    keys = {p.api_key for p in parsed.providers}
    opened = sum(vault.decrypt_key(p.encrypted_api_key, context=f"vault-provider:{p.id}:api_key") in keys for p in providers)
    assert opened == len(providers)  # and every key opens back to exactly what the file said
