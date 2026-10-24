"""Live sports feeds into the desk (Group 112, points b.13 and b.13a). Developed for Amit Ashok Kumar Patnaik.

One ingestion path, not two. Odds are bought only by the ingestion fleet's Odds API source (``omni_fleet.run_source``):
Fleet Command's switch and key, the circuit breaker, the quota reserve and floor, the cache shield and the tiered
pre-match cadence all apply, and the fleet's normaliser, quorum merge and stores do the rest. So a run from the CLI
(``app.workers.live_ingest_runner``) or the admin endpoint can never spend a credit the fleet would refuse to spend, and
never prices a board two ways. This module adds:

* ``ingest``: the sports asked for (``cricket``, ``football`` or Odds API sport keys) narrowed to the fleet's coverage
  and, unless forced, to the sports the cadence says are due; one fleet run; then the fixtures' context. When nothing is
  bought (nothing due, the source switched off, no usable key, the breaker, the reserve) the outcome says why, and the
  desk is served from the stored boards: ``HYDRATED_FROM_DATABASE``. ``force`` skips the cadence, never the breaker,
  the reserve, the floor or the shield.
* ``hydrate``: Redis's live board and Aryabhata's books rebuilt from the stored boards with no provider call, each price
  stamped with the time it was bought. Only boards Redis would still hold (``LIVE_ODDS_SNAPSHOT_TTL_SECONDS``) are
  replayed: an older price is not live, and re-stamping it would let it be vetted and staked. The match board lists the
  older ones as stored prices instead (``match_board_service``).
* ``refresh_context``: Football-Data.org and Sportmonks paired to the stored fixtures (``app.domain.live_context``) and
  kept per canonical fixture in Redis, one hash per provider, rewritten only when that provider answered.
* ``status``: what stands between the desk and live prices (the switch, the key and its quota, the sports in season, the
  store) and what the cadence would spend a day at today's fixtures.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.adapters.ingestion.base import IngestionBatch, SourcePayload
from app.adapters.ingestion.odds_api_adapter import OddsApiIngestor, credits_per_call
from app.adapters.sports_data import SportsDataError
from app.adapters.sports_data.football_data import PROVIDER as FOOTBALL_DATA, FootballDataClient, window
from app.adapters.sports_data.sportsmonks import PROVIDER as SPORTMONKS, SportmonksClient
from app.core.config import Settings
from app.core.security_vault import VaultConfigurationError, VaultCrypto, get_vault_crypto
from app.domain.live_context import ProviderFixture, StoredFixture, TableRow, context, pair
from app.domain.pre_match.cadence import aware, max_age_seconds, tier_for
from app.models.pre_match_odds import PreMatchOddsSnapshotModel
from app.services import pre_match_odds_service as store
from app.services.aryabhata_pipeline import publish_market_quotes
from app.services.omni_fleet import (
    FleetDeps,
    candidate_api_keys,
    env_key_field,
    fleet_keys,
    publish_fleet_ticks,
    read_shield_mode,
    refusal_reason,
    resolve_api_key,
    run_source,
    schedule_state,
    tick_interval,
)
from app.services.omni_normalizer import AliasDictionary, OmniNormalizer
from app.services.omni_router import SourceDescriptor, load_registry

logger = logging.getLogger("betdoc.live_ingest")
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"
SOURCE_ID = store.SOURCE_ID  # "odds_api"
CONTEXT_PROVIDERS = (FOOTBALL_DATA, SPORTMONKS)  # read in this order: Football-Data.org first where both pair a fixture
IngestStatus = Literal["COMPLETED", "HYDRATED_FROM_DATABASE"]
_SKIPPED = {
    "disabled": "the Odds API source is switched off in Fleet Command",
    "paused": "the Odds API source is paused in Fleet Command (FATAL)",
    "needs_key": "no Odds API key: Fleet Command holds none and THE_ODDS_API_KEY / ODDS_API_KEY are unset",
    "locked": "an Odds API run is already in progress",
    "throttled": "the fleet's own rate limiter deferred the run",
    "not_due": "the fleet ran the source moments ago",
    "circuit_open": "the Odds API circuit breaker is open",
    "quota_reserve": "the Odds API quota is under the fleet's reserve",
    "unknown_source": "the fleet does not know the Odds API source",
}


def _secret(value: Any) -> str | None:
    raw = value.get_secret_value() if value is not None and hasattr(value, "get_secret_value") else value
    return str(raw) if raw else None


def _float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


# ================================================================ standalone wiring (CLI, Celery)
@asynccontextmanager
async def standalone_fleet_deps(settings: Settings) -> AsyncIterator[FleetDeps]:
    """The fleet's dependencies for one event loop outside the API process (a CLI run, a Celery task)."""
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    http = httpx.AsyncClient(timeout=settings.omni_http_timeout_seconds, follow_redirects=False,
                             limits=httpx.Limits(max_connections=settings.omni_http_max_connections))
    try:
        vault: VaultCrypto | None = get_vault_crypto()
    except VaultConfigurationError:
        vault = None
    try:
        yield FleetDeps(redis=redis, session_factory=async_sessionmaker(engine, expire_on_commit=False), http=http, vault=vault, settings=settings)
    finally:
        await http.aclose()
        await redis.aclose()
        await engine.dispose()


async def _odds_source(deps: FleetDeps) -> tuple[SourceDescriptor | None, Any]:
    async with deps.session_factory() as session:
        registry, rows = await load_registry(session, deps.settings, deps.aliases)
    return registry.get(SOURCE_ID), rows.get(SOURCE_ID)


async def _metrics(deps: FleetDeps) -> dict[str, str]:
    try:
        return await deps.redis.hgetall(fleet_keys(deps.settings).fleet_metrics(SOURCE_ID)) or {}
    except (RedisError, OSError):
        return {}


# ================================================================ scope
def resolve_scope(requested: Sequence[str], settings: Settings) -> tuple[list[str], list[str]]:
    """``(covered Odds API keys, names not covered)``. A name is a sport group of MANUAL_PARLAY_SPORTS (``cricket``,
    ``football``) or one Odds API key; only keys the fleet covers (ODDS_SPORT_KEYS and the Vault's sports) count."""
    covered = list(OddsApiIngestor.coverage(settings))
    groups = {label.casefold(): prefixes for label, prefixes in settings.MANUAL_PARLAY_SPORTS.items()}
    groups.setdefault("soccer", groups.get("football", ("soccer",)))
    keys: list[str] = []
    missing: list[str] = []
    for name in (n.strip() for n in requested if n.strip()):
        folded = name.casefold()
        if folded in groups:
            found = [k for k in covered if any(k.startswith(p) for p in groups[folded])]
        elif folded in covered:
            found = [folded]
        else:
            found = []
        if not found:
            missing.append(name)
        keys.extend(k for k in found if k not in keys)
    return keys, missing


async def _stretch(deps: FleetDeps, descriptor: SourceDescriptor) -> float:
    """The cache shield's stretch of every tier, as the fleet's beat applies it."""
    quota = _float((await _metrics(deps)).get("quota_fraction"))
    mode = await read_shield_mode(deps.redis, deps.settings)
    return tick_interval(descriptor, quota, mode, deps.settings) / descriptor.interval_seconds


async def _refusal(deps: FleetDeps) -> str | None:
    """Why a credit must not be spent now, forced or not: the breaker, the reserve, the floor, a forced cache shield."""
    reason = await refusal_reason(deps, SOURCE_ID)
    if reason is not None:
        return reason
    remaining = _float((await _metrics(deps)).get("quota_remaining"))
    if remaining is not None and remaining < deps.settings.ODDS_QUOTA_FLOOR:
        return f"the Odds API key has {remaining:g} credits left, under the floor of {deps.settings.ODDS_QUOTA_FLOOR}"
    if await read_shield_mode(deps.redis, deps.settings) == "FORCE_CACHE_SHIELD":
        return "the operator forced the quota cache shield: stored boards are served"
    return None


# ================================================================ ingest
async def ingest(deps: FleetDeps, *, sports: Sequence[str] = ("cricket", "football"), force: bool = False, with_context: bool = True,
                 now: datetime | None = None) -> dict[str, Any]:
    """Buy what is due through the fleet, then the context. Never raises for a provider's sake."""
    now = aware(now or datetime.now(UTC))
    settings = deps.settings
    scope, not_covered = resolve_scope(sports, settings)
    odds: dict[str, Any] = {"requested": list(sports), "scope": scope, "not_covered": not_covered, "forced": force}
    status: IngestStatus = "HYDRATED_FROM_DATABASE"
    descriptor, row = await _odds_source(deps)
    state = None if descriptor is None else schedule_state(row, descriptor, settings)
    if descriptor is None:
        odds.update(outcome="SKIPPED", reason=_SKIPPED["unknown_source"])
    elif state in ("disabled", "paused", "needs_key"):
        # Fleet Command's own switch: said as it is (the fleet's interval gate would report a switched-off source as not due)
        odds.update(outcome="SKIPPED", reason=_SKIPPED[state])
    elif not scope:
        odds.update(outcome="NOTHING_IN_SCOPE", reason="none of the requested sports is in the fleet's Odds API coverage (ODDS_SPORT_KEYS)")
    else:
        due = scope if force else await store.due_scope(deps.session_factory, deps.redis, settings, scope, now, stretch=await _stretch(deps, descriptor))
        odds["due"] = due
        refusal = await _refusal(deps) if due else None
        if not due:
            odds.update(outcome="UP_TO_DATE", reason="every requested sport's stored board is within its tier's age")
        elif refusal is not None:
            odds.update(outcome="REFUSED", reason=refusal)
        else:
            summary = await run_source(SOURCE_ID, deps, runner="manual", force=force, scope=due)
            odds["run"] = summary.as_dict()
            if summary.status == "ok":
                status = "COMPLETED"
                odds.update(outcome="BOUGHT", reason=None)
            elif summary.status == "skipped":
                odds.update(outcome="SKIPPED", reason=_SKIPPED.get(summary.reason or "", f"the fleet did not run ({summary.reason})"))
            else:
                odds.update(outcome="FAILED", reason=summary.reason or summary.status)
        metrics = await _metrics(deps)
        odds["quota_remaining"] = _float(metrics.get("quota_remaining"))
    result: dict[str, Any] = {"status": status, "odds_api": odds, "hydration": None, "context": None}
    if status != "COMPLETED":
        result["hydration"] = await hydrate(deps, sports=scope or None, now=now)  # what Redis may still serve from the store
    if with_context:
        result["context"] = await refresh_context(deps, now=now)
    result["store"] = await store_summary(deps, now=now)
    result.update(at=now.isoformat(), developer_credit=DEVELOPER_CREDIT)
    return result


# ================================================================ the sport catalogue and the board-load nudge (Group 127)
def _catalog_key(settings: Settings) -> str:
    return f"{settings.omni_redis_prefix}:vault:odds_in_season"


def sport_category(sport_key: str, settings: Settings) -> str | None:
    """The board's sport family (MANUAL_PARLAY_SPORTS) a key belongs to, or None when the board does not show it."""
    return next((label for label, prefixes in settings.MANUAL_PARLAY_SPORTS.items() if any(sport_key.startswith(p) for p in prefixes)), None)


def suggested(sport_key: str, settings: Settings) -> bool:
    for item in (s.strip() for s in settings.ODDS_SUGGESTED_SPORT_KEYS.split(",") if s.strip()):
        if sport_key == item or (item.endswith("_") and sport_key.startswith(item)):
            return True
    return False


def credits_per_day_range(sport_key: str, settings: Settings, now: datetime) -> tuple[float, float]:
    """(far from kickoff, near kickoff): what a sport costs a day at its slowest and fastest cadence tier."""
    per_call = credits_per_call(settings, sport_key, now)
    far = max(max_age_seconds(tier_for(30 * 86400.0, settings), settings, 1.0), float(settings.ODDS_POLLING_INTERVAL_SEC))
    near = max(max_age_seconds(tier_for(60.0, settings), settings, 1.0), float(settings.ODDS_POLLING_INTERVAL_SEC))
    return round(86400.0 / far * per_call, 1), round(86400.0 / near * per_call, 1)


async def in_season_index(deps: FleetDeps, *, refresh: bool = False) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The Odds API's in-season sports ``[{key, title, group}]`` (``GET /sports``, not metered), cached in Redis for
    ODDS_SPORT_CATALOG_TTL_SECONDS; read with the first configured key the provider accepts. (None, why) when unreadable."""
    settings = deps.settings
    if not refresh:
        try:
            cached = await deps.redis.get(_catalog_key(settings))
        except (RedisError, OSError):
            cached = None
        if cached:
            try:
                return json.loads(cached), None
            except ValueError:
                pass
    descriptor, row = await _odds_source(deps)
    if descriptor is None:
        return None, "the Odds API source is not registered"
    try:
        keys = candidate_api_keys(row, descriptor, settings, deps.vault)
    except Exception:  # noqa: BLE001 - an undecryptable vault key leaves the environment's keys
        keys = [(name, k) for name in ("THE_ODDS_API_KEY", "ODDS_API_KEY") if (k := _secret(getattr(settings, name, None)))]
    if not keys:
        return None, "no Odds API key is configured"
    reason = "every configured key was refused"
    for _, key in keys:
        try:
            response = await deps.http.get(f"{settings.ODDS_API_BASE_URL.rstrip('/')}/sports", params={"apiKey": key}, timeout=settings.LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            reason = f"The Odds API is unreachable ({type(exc).__name__})"
            continue
        if response.status_code != 200:
            reason = f"The Odds API answered HTTP {response.status_code}"
            continue
        try:
            listing = response.json()
        except ValueError:
            return None, "The Odds API's sport list was unreadable"
        index = sorted(({"key": str(s["key"]), "title": str(s.get("title") or s["key"]), "group": str(s.get("group") or "")}
                        for s in listing if isinstance(s, Mapping) and s.get("key") and s.get("active") is True and not s.get("has_outrights")),
                       key=lambda s: s["key"])
        try:
            await deps.redis.set(_catalog_key(settings), json.dumps(index), ex=settings.ODDS_SPORT_CATALOG_TTL_SECONDS)
        except (RedisError, OSError):
            pass
        return index, None
    return None, reason


async def sport_catalog(deps: FleetDeps, *, vault_sports: Sequence[str], refresh: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """Every in-season sport the board can show, whether it is polled and from where, and what it would cost: what the
    Vault offers to switch on. The environment's sports are always polled; the Vault's are the user's choice."""
    now = aware(now or datetime.now(UTC))
    settings = deps.settings
    index, reason = await in_season_index(deps, refresh=refresh)
    env = [s.strip() for s in settings.ODDS_SPORT_KEYS.split(",") if s.strip()]
    rows: dict[str, dict[str, Any]] = {}
    for entry in index or []:
        category = sport_category(entry["key"], settings)
        if category is not None:
            rows[entry["key"]] = {**entry, "category": category, "in_season": True}
    for key in [*env, *vault_sports]:  # a polled sport is listed even out of season
        rows.setdefault(key, {"key": key, "title": key, "group": "", "category": sport_category(key, settings), "in_season": None if index is None else False})
    sports = []
    for key, row in sorted(rows.items(), key=lambda kv: (kv[1]["category"] or "~", kv[0])):
        low, high = credits_per_day_range(key, settings, now)
        sports.append({**row, "source": "environment" if key in env else "vault" if key in vault_sports else None,
                       "polling": key in env or key in vault_sports, "suggested": suggested(key, settings),
                       "credits_per_call": credits_per_call(settings, key, now), "credits_per_day": {"far": low, "near": high}})
    return {"sports": sports, "index_read": index is not None, "reason": reason, "at": now.isoformat(), "developer_credit": DEVELOPER_CREDIT}


def _nudge_key(settings: Settings) -> str:
    return f"{settings.omni_redis_prefix}:odds_api:nudge"


async def nudge(deps: FleetDeps | None, *, fixtures_on_board: int, now: datetime | None = None) -> str:
    """A board page found the live board thin (under LIVE_ODDS_NUDGE_MIN_FIXTURES fixtures): run ``ingest`` for every
    covered sport, at most once per LIVE_ODDS_NUDGE_COOLDOWN_SECONDS across the cluster. Never forced: only sports whose
    stored board is past its tier's age are bought, a switched-off or paused source stays off, the breaker, reserve, floor
    and shield apply, and otherwise the stored boards are replayed for free. Returns what happened, in a word."""
    if deps is None or not deps.settings.LIVE_ODDS_NUDGE_ENABLED:
        return "disabled"
    settings = deps.settings
    if fixtures_on_board >= settings.LIVE_ODDS_NUDGE_MIN_FIXTURES:
        return "board_full"
    try:
        claimed = await deps.redis.set(_nudge_key(settings), "1", nx=True, ex=settings.LIVE_ODDS_NUDGE_COOLDOWN_SECONDS)
    except (RedisError, OSError):
        return "no_redis"
    if not claimed:
        return "cooling_down"
    try:
        result = await ingest(deps, sports=list(OddsApiIngestor.coverage(settings)), force=False, with_context=False, now=now)
    except Exception:  # noqa: BLE001 - a page's background refresh never breaks the page
        logger.warning("Board-load odds refresh failed", exc_info=True)
        return "failed"
    outcome = str(result["odds_api"].get("outcome") or result["status"])
    logger.info("Board-load odds refresh (%d fixture(s) on the board): %s", fixtures_on_board, outcome)
    return outcome


# ================================================================ hydration (no provider call)
def canonical_fixture_id(snapshot: PreMatchOddsSnapshotModel, aliases: AliasDictionary) -> str:
    """The id the fleet's normaliser gives this fixture (sport, both canonical sides, UTC date), so a stored fixture and
    its live board cell are one fixture everywhere."""
    home, away = aliases.resolve(snapshot.sport_key, snapshot.home_team), aliases.resolve(snapshot.sport_key, snapshot.away_team)
    return aliases.match_id(snapshot.sport_key, home.id, away.id, aware(snapshot.commence_time))


async def hydrate(deps: FleetDeps, *, sports: Sequence[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Replay the stored boards Redis would still hold into the live board and Aryabhata's books, each with the time it
    was bought (``LIVE_ODDS_SNAPSHOT_TTL_SECONDS`` old at most); older boards are counted, not replayed."""
    started = time.perf_counter()
    now = aware(now or datetime.now(UTC))
    settings = deps.settings
    keys = list(sports) if sports else list(OddsApiIngestor.coverage(settings))
    max_age = float(settings.LIVE_ODDS_SNAPSHOT_TTL_SECONDS)
    async with deps.session_factory() as db:
        boards = await store.stored_boards(db, keys, now, settings)
    groups: dict[tuple[str, datetime], list[dict[str, Any]]] = defaultdict(list)
    stale = 0
    for sport, rows in boards.items():
        for row in rows:
            bought = store.price_time(row)
            if (now - bought).total_seconds() > max_age:
                stale += 1
                continue
            groups[(sport, bought)].append(store.snapshot_event(row, polled_at=bought))
    report: dict[str, Any] = {"fixtures_stored": sum(len(r) for r in boards.values()), "fixtures_hydrated": sum(len(e) for e in groups.values()),
                              "skipped_stale": stale, "max_age_seconds": max_age, "ticks": 0, "quotes": 0}
    if groups:
        descriptor, _ = await _odds_source(deps)
        normalizer = OmniNormalizer(deps.aliases)
        ticks, quotes = [], []
        for (sport, bought), events in groups.items():
            batch = IngestionBatch(source_id=SOURCE_ID, payloads=[SourcePayload(key=sport, data=events)], fetched_at=bought, latency_ms=0, requests=0, retries=0)
            normalized = normalizer.normalize(batch, devig_method=None if descriptor is None else descriptor.devig)
            ticks.extend(normalized.ticks)
            quotes.extend(normalized.quotes)
        await publish_fleet_ticks(deps, ticks)
        await publish_market_quotes(deps.redis, quotes, settings)
        report.update(ticks=len(ticks), quotes=len(quotes))
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return report


# ================================================================ fixture context (Football-Data.org, Sportmonks)
def _fixtures_key(settings: Settings, provider: str) -> str:
    return f"{settings.LIVE_CONTEXT_REDIS_PREFIX}:fixtures:{provider}"


def _table_key(settings: Settings, sport_key: str) -> str:
    return f"{settings.LIVE_CONTEXT_REDIS_PREFIX}:table:{sport_key}"


def _meta_key(settings: Settings) -> str:
    return f"{settings.LIVE_CONTEXT_REDIS_PREFIX}:meta"


async def _tables(deps: FleetDeps, client: FootballDataClient, now: datetime) -> tuple[dict[str, list[TableRow]], int]:
    """Each configured competition's table, from Redis while younger than LIVE_CONTEXT_STANDINGS_MAX_AGE_SECONDS, else one
    call. ``(tables by Odds API key, calls made)``; a table that cannot be read is left out."""
    settings = deps.settings
    tables: dict[str, list[TableRow]] = {}
    calls = 0
    for code, sport_key in settings.FOOTBALL_DATA_COMPETITIONS.items():
        cached = None
        try:
            raw = await deps.redis.get(_table_key(settings, sport_key))
            cached = json.loads(raw) if raw else None
        except (RedisError, OSError, ValueError):
            cached = None
        if cached and (now - datetime.fromisoformat(cached["fetched_at"])).total_seconds() < settings.LIVE_CONTEXT_STANDINGS_MAX_AGE_SECONDS:
            tables[sport_key] = [TableRow(team=tuple(r["team"]), position=r["position"], points=r.get("points"), played=r.get("played")) for r in cached["rows"]]
            continue
        try:
            rows = await client.table(code)
            calls += 1
        except SportsDataError as exc:
            logger.info("live context: %s table unavailable (%s)", code, exc)
            continue
        tables[sport_key] = rows
        payload = {"fetched_at": now.isoformat(), "rows": [{"team": list(r.team), "position": r.position, "points": r.points, "played": r.played} for r in rows]}
        try:
            await deps.redis.set(_table_key(settings, sport_key), json.dumps(payload), ex=int(settings.LIVE_CONTEXT_STANDINGS_MAX_AGE_SECONDS * 2))
        except (RedisError, OSError):
            pass
    return tables, calls


async def refresh_context(deps: FleetDeps, *, now: datetime | None = None) -> dict[str, Any]:
    """Pair each provider's fixtures to the stored fixtures and keep the context of every pair. A provider that cannot be
    read keeps its previous context (its hash is not rewritten) and reports why."""
    now = aware(now or datetime.now(UTC))
    settings = deps.settings
    if not settings.LIVE_CONTEXT_ENABLED:
        return {"enabled": False}
    sport_keys = sorted(set(settings.FOOTBALL_DATA_COMPETITIONS.values()) | set(settings.SPORTSMONKS_LEAGUE_SPORT_KEYS.values()))
    async with deps.session_factory() as db:
        boards = await store.stored_boards(db, sport_keys, now, settings)
    stored = [StoredFixture(canonical_fixture_id(row, deps.aliases), row.sport_key, row.home_team, row.away_team, aware(row.commence_time))
              for rows in boards.values() for row in rows]
    by_id = {s.fixture_id: s for s in stored}
    start, end = window(now.date(), settings.LIVE_CONTEXT_LOOKBACK_DAYS, settings.LIVE_CONTEXT_LOOKAHEAD_DAYS)
    tolerance = timedelta(minutes=settings.LIVE_CONTEXT_KICKOFF_TOLERANCE_MINUTES)
    report: dict[str, Any] = {"enabled": True, "stored_fixtures": len(stored), "providers": {}}
    timeout = settings.LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS

    async def read(provider: str) -> tuple[list[ProviderFixture], dict[str, list[TableRow]], dict[str, Any]] | None:
        if provider == FOOTBALL_DATA:
            key = _secret(settings.FOOTBALL_DATA_ORG_API_KEY)
            if key is None:
                report["providers"][provider] = {"error": "no key (FOOTBALL_DATA_ORG_API_KEY)"}
                return None
            fd = FootballDataClient(deps.http, key, settings.FOOTBALL_DATA_BASE_URL, timeout)
            fixtures = await fd.fixtures(settings.FOOTBALL_DATA_COMPETITIONS, start, end)
            tables, calls = await _tables(deps, fd, now)
            return fixtures, tables, {"calls": 1 + calls, "requests_available_minute": fd.requests_available, "tables": len(tables)}
        key = _secret(settings.SPORTSMONKS_API_KEY)
        if key is None:
            report["providers"][provider] = {"error": "no key (SPORTSMONKS_API_KEY)"}
            return None
        sm = SportmonksClient(deps.http, key, settings.SPORTSMONKS_BASE_URL, timeout)
        fixtures = await sm.fixtures(settings.SPORTSMONKS_LEAGUE_SPORT_KEYS, start, end)
        return fixtures, {}, {"rate_limit_remaining": sm.rate_limit_remaining}

    for provider in CONTEXT_PROVIDERS:
        try:
            got = await read(provider)
        except SportsDataError as exc:
            report["providers"][provider] = {"error": str(exc)}
            continue
        if got is None:
            continue
        fixtures, tables, extra = got
        pairing = pair(stored, fixtures, cutoff=settings.LIVE_CONTEXT_NAME_CUTOFF,
                       window=timedelta(hours=settings.LIVE_CONTEXT_PAIR_WINDOW_HOURS), tolerance=tolerance)
        contexts = {fid: context(by_id[fid], p, tolerance=tolerance, tables=tables, cutoff=settings.LIVE_CONTEXT_NAME_CUTOFF, checked_at=now)
                    for fid, p in pairing.pairs.items()}
        entry = {"fixtures": len(fixtures), "paired": len(contexts), "unpaired_stored": pairing.unpaired, "refreshed_at": now.isoformat(), **extra}
        try:
            hash_key = _fixtures_key(settings, provider)
            pipe = deps.redis.pipeline(transaction=True)
            pipe.delete(hash_key)
            if contexts:
                pipe.hset(hash_key, mapping={fid: json.dumps(c, separators=(",", ":")) for fid, c in contexts.items()})
                pipe.expire(hash_key, settings.LIVE_CONTEXT_TTL_SECONDS)
            pipe.hset(_meta_key(settings), provider, json.dumps(entry, separators=(",", ":")))
            await pipe.execute()
        except (RedisError, OSError):
            entry["error"] = "Redis unavailable: context not kept"
        report["providers"][provider] = entry
    return report


async def read_context(redis: Redis | None, settings: Settings, fixture_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Each fixture's context, Football-Data.org's first where both providers paired it. Unreadable: none."""
    if redis is None or not fixture_ids or not settings.LIVE_CONTEXT_ENABLED:
        return {}
    ids = list(dict.fromkeys(fixture_ids))
    out: dict[str, dict[str, Any]] = {}
    try:
        pipe = redis.pipeline(transaction=False)
        for provider in CONTEXT_PROVIDERS:
            pipe.hmget(_fixtures_key(settings, provider), ids)
        answers = await pipe.execute()
    except (RedisError, OSError):
        return {}
    for values in answers:
        for fid, raw in zip(ids, values or [], strict=False):
            if raw and fid not in out:
                try:
                    out[fid] = json.loads(raw)
                except ValueError:
                    continue
    return out


# ================================================================ status (what stands between the desk and live prices)
async def store_summary(deps: FleetDeps, *, now: datetime | None = None) -> dict[str, Any]:
    now = aware(now or datetime.now(UTC))
    async with deps.session_factory() as db:
        boards = await store.stored_boards(db, list(OddsApiIngestor.coverage(deps.settings)), now, deps.settings)
    sports = {}
    for sport, rows in boards.items():
        newest = max(store.price_time(r) for r in rows)
        sports[sport] = {"fixtures": len(rows), "newest_prices": newest.isoformat(), "age_minutes": round((now - newest).total_seconds() / 60)}
    return {"fixtures": sum(s["fixtures"] for s in sports.values()), "sports": sports}


async def _probe_key(http: httpx.AsyncClient, settings: Settings, key: str) -> dict[str, Any]:
    """``GET /sports`` (not metered): whether the key is accepted, its credits and the sports in season."""
    try:
        response = await http.get(f"{settings.ODDS_API_BASE_URL.rstrip('/')}/sports", params={"apiKey": key}, timeout=settings.LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": type(exc).__name__}
    out: dict[str, Any] = {"ok": response.status_code == 200, "http_status": response.status_code,
                           "remaining": _float(response.headers.get("x-requests-remaining")), "used": _float(response.headers.get("x-requests-used"))}
    if response.status_code == 200:
        try:
            out["in_season"] = sorted(str(s["key"]) for s in response.json() if isinstance(s, Mapping) and s.get("active") is True and s.get("key"))
        except ValueError:
            out["in_season"] = None
    return out


def projected_credits_per_day(nearest: Mapping[str, datetime | None], settings: Settings, now: datetime, stretch: float = 1.0) -> dict[str, float]:
    """Credits a day each sport costs at the tier its nearest fixture is on now (a sport with none stored: tier 1)."""
    out: dict[str, float] = {}
    for sport, kickoff in nearest.items():
        tier = 1 if kickoff is None else tier_for((aware(kickoff) - now).total_seconds(), settings)
        age = max(max_age_seconds(tier, settings, stretch), float(settings.ODDS_POLLING_INTERVAL_SEC))
        out[sport] = round(86400.0 / age * credits_per_call(settings, sport, now), 1)
    return out


async def status(deps: FleetDeps, *, probe: bool = False, now: datetime | None = None) -> dict[str, Any]:
    """The Odds API source as the fleet sees it, every key the fleet could use (probed for free when asked), the sports in
    season, the store, the context feeds and the plain reasons nothing is being bought. Never shows a key."""
    now = aware(now or datetime.now(UTC))
    settings = deps.settings
    descriptor, row = await _odds_source(deps)
    metrics = await _metrics(deps)
    coverage = list(OddsApiIngestor.coverage(settings))
    env_field = None if descriptor is None else env_key_field(descriptor, settings)
    source: dict[str, Any] = {
        "state": None if descriptor is None else schedule_state(row, descriptor, settings),
        "key_in_use": metrics.get("key_in_use") or ("fleet_command_vault" if row is not None and row.encrypted_api_key else (env_field or None)),
        "rejected_keys": [k for k in (metrics.get("rejected_keys") or "").split(",") if k],
        "last_status": metrics.get("last_status") or None, "last_error": metrics.get("last_error") or None,
        "quota_remaining": _float(metrics.get("quota_remaining")), "quota_used": _float(metrics.get("quota_used")),
        "quota_floor": settings.ODDS_QUOTA_FLOOR, "quota_reserve": settings.OMNI_FLEET_QUOTA_RESERVE,
    }
    keys: dict[str, Any] = {}
    if probe:
        candidates: list[tuple[str, str | None]] = []
        if row is not None and row.encrypted_api_key and descriptor is not None:
            try:
                candidates.append(("fleet_command_vault", resolve_api_key(row, descriptor, settings, deps.vault)))
            except Exception as exc:  # noqa: BLE001 - an undecryptable key is reported, never raised
                keys["fleet_command_vault"] = {"ok": False, "error": f"cannot decrypt ({type(exc).__name__})"}
        candidates += [(name, _secret(getattr(settings, name, None))) for name in ("THE_ODDS_API_KEY", "ODDS_API_KEY")]
        for origin, key in candidates:
            if key:
                keys[origin] = await _probe_key(deps.http, settings, key)
    in_season = next((k.get("in_season") for o, k in keys.items() if o == source["key_in_use"] and k.get("ok")), None)
    if in_season is None:
        in_season = next((k.get("in_season") for k in keys.values() if k.get("ok")), None)
    stretch = 1.0 if descriptor is None else await _stretch(deps, descriptor)
    async with deps.session_factory() as db:
        inputs = await store.cadence_inputs(db, deps.redis, settings, coverage, now)
    buying = [s for s in coverage if in_season is None or s in in_season]
    per_day = projected_credits_per_day({s: inputs.nearest_kickoff.get(s) for s in buying}, settings, now, stretch)
    total = round(sum(per_day.values()), 1)
    remaining = (keys.get(source["key_in_use"]) or {}).get("remaining") if source["key_in_use"] else None
    remaining = remaining if remaining is not None else source["quota_remaining"]
    blockers: list[str] = []
    if source["state"] in _SKIPPED:
        blockers.append(_SKIPPED[source["state"]])
    in_use = keys.get(source["key_in_use"] or "")
    if in_use is not None and not in_use.get("ok"):
        blockers.append(f"the key in use ({source['key_in_use']}) is rejected by The Odds API ({in_use.get('http_status') or in_use.get('error')})")
    if remaining is not None and remaining < settings.ODDS_QUOTA_FLOOR:
        blockers.append(f"the key in use has {remaining:g} credits left, under the floor of {settings.ODDS_QUOTA_FLOOR}")
    usable = [o for o, k in keys.items() if k.get("ok") and (k.get("remaining") or 0) >= settings.ODDS_QUOTA_FLOOR]
    meta: dict[str, Any] = {}
    try:
        meta = {p: json.loads(v) for p, v in (await deps.redis.hgetall(_meta_key(settings)) or {}).items()}
    except (RedisError, OSError, ValueError):
        meta = {}
    return {
        "source": source, "keys": keys, "usable_keys": usable, "blockers": blockers,
        "coverage": {"configured": coverage, "in_season": None if in_season is None else [s for s in coverage if s in in_season],
                     "out_of_season": None if in_season is None else [s for s in coverage if s not in in_season]},
        "cadence": {"tiers_seconds": {"tier1_beyond": settings.PRE_MATCH_TIER1_KICKOFF_SECONDS, "tier1_max_age": settings.PRE_MATCH_TIER1_MAX_AGE_SECONDS,
                                      "tier2_beyond": settings.PRE_MATCH_TIER2_KICKOFF_SECONDS, "tier2_max_age": settings.PRE_MATCH_TIER2_MAX_AGE_SECONDS,
                                      "tier3_max_age": settings.PRE_MATCH_TIER3_MAX_AGE_SECONDS},
                    "shield_stretch": stretch, "credits_per_day": per_day, "credits_per_day_total": total,
                    "days_of_quota": None if remaining is None or total <= 0 else round(remaining / total, 1),
                    # how long each accepted key would last at this pace (a key whose reading is under the floor buys nothing)
                    "days_by_key": {o: round(max(0.0, (k.get("remaining") or 0) - settings.ODDS_QUOTA_FLOOR) / total, 1)
                                    for o, k in keys.items() if k.get("ok") and total > 0}},
        "store": await store_summary(deps, now=now),
        "context": {"football_data_key": _secret(settings.FOOTBALL_DATA_ORG_API_KEY) is not None, "sportmonks_key": _secret(settings.SPORTSMONKS_API_KEY) is not None,
                    "competitions": settings.FOOTBALL_DATA_COMPETITIONS, "sportmonks_leagues": {str(k): v for k, v in settings.SPORTSMONKS_LEAGUE_SPORT_KEYS.items()},
                    "last_refresh": meta},
        "at": now.isoformat(), "developer_credit": DEVELOPER_CREDIT,
    }
