"""
Multi-Sport Omni-Provider Odds Synchronization & Multi-Venue Edge Screener Service (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Two Celery beat jobs drive it (``app.workers.sports_tasks``):

* ``poll_multi_sport_consensus_odds`` (every ``SPORTS_ODDS_POLL_SECONDS``, 60 s): for every active sport's feed
  keys (its primary key and its active leagues'), the head-to-head board. Group 112: by default
  (``SPORTS_ODDS_SOURCE="store"``) the board the ingestion fleet last bought for the key (``pre_match_odds_snapshots``),
  stamped with that purchase's time: no call, no credit. ``"direct"`` fetches it from The Odds API, each key at most
  every ``SPORTS_FEED_MIN_INTERVAL_SECONDS`` (the quota is finite: ``x-requests-remaining`` under ``ODDS_QUOTA_FLOOR``
  stops the tick). Every event is normalised onto the OmniRoute venues (``onexbet`` ->
  ``1xbet``, Betfair net of commission) inside the FA-3 budget (``SPORTS_NORMALIZATION_BUDGET_MS``, 12 ms), given
  its de-vigged consensus and its sharp benchmark (Pinnacle, else the Betfair exchange), and stored in Redis.
* ``screen_cross_provider_arbitrage`` (every ``SPORTS_ARB_SCREEN_SECONDS``, 30 s; the task keeps its historical name
  so running workers and beats need no change): every stored board screened across all venues for value lines (a
  retail price over the sharp fair price by ``SPORTS_MIN_EDGE``) from boards no older than
  ``SPORTS_VALUE_LINE_MAX_AGE_SECONDS``; the edge list is stored and published live.
  Group 96 retired cross-venue arbitrage.

``screen_multi_provider_edges`` prices one match with its sport's quantitative adapter instead of the market, so
the desk can shop a model price across every venue it holds quotes for.

Credentials: the vault's ``vault_provider_credentials`` first (decrypted with the master vault key, bound to the
row), the Settings field second, ``OMNI_<ID>_API_KEY`` third. A key is never returned by the API or logged.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.provider_secrets import provider_secret
from app.core.security_vault import VaultConfigurationError, VaultCrypto
from app.domain.bookmakers.adapters import canonical_bookmaker
from app.domain.sports.providers import PROVIDERS, ProviderSpec, provider
from app.domain.sports.registry import SportAdapterRegistry, sport_for_odds_key
from app.domain.sports.twin_bridge import (
    DEFAULT_COMMISSION,
    SHARP_BOOKS,
    canonical_quotes,
    confidence_index,
    consensus_verdict,
    net_odds,
    route_best_price,
    sharp_benchmark,
)
from app.models.omni_vault import VaultProviderCredential
from app.models.sports_config import LeagueRegistry, SportConfiguration

logger = logging.getLogger("betdoc.sports_sync")
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"


# ================================================================ credentials
@dataclass(frozen=True)
class ProviderCredential:
    spec: ProviderSpec
    api_key: SecretStr | None
    base_url: str | None
    source: str  # vault | settings | environment | none

    def public_view(self) -> dict[str, Any]:
        return {"provider": self.spec.provider_id, "name": self.spec.name, "category": self.spec.category, "role": self.spec.role,
                "base_url": self.base_url, "target_url": self.base_url if self.spec.bookmaker else None, "bookmaker": self.spec.bookmaker,
                "configured": self.api_key is not None, "key_source": self.source, "is_active": True}


# ================================================================ normalised boards
@dataclass
class NormalizedEvent:
    event_id: str
    feed_key: str
    sport_key: str
    commence_time: str
    home: str
    away: str
    venue_odds: dict[str, dict[str, float]]
    outcomes: tuple[str, ...]
    consensus: dict[str, float] = field(default_factory=dict)
    sharp: dict[str, Any] | None = None
    normalization_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "feed_key": self.feed_key, "sport_key": self.sport_key, "commence_time": self.commence_time,
                "home": self.home, "away": self.away, "venue_odds": self.venue_odds, "outcomes": list(self.outcomes), "consensus": self.consensus,
                "sharp": self.sharp, "normalization_ms": round(self.normalization_ms, 4)}


def board_key(settings: Settings, feed_key: str) -> str:
    return f"{settings.SPORTS_PREFIX}:board:{feed_key}"


def edges_key(settings: Settings) -> str:
    return f"{settings.SPORTS_PREFIX}:edges"


def edges_channel(settings: Settings) -> str:
    return f"{settings.SPORTS_PREFIX}:edges:live"


def consensus_probabilities(venue_odds: Mapping[str, Mapping[str, float]], outcomes: Sequence[str]) -> dict[str, float]:
    """Every complete book de-vigged proportionally, then averaged (the market's consensus)."""
    rows = []
    for quotes in venue_odds.values():
        if all(o in quotes and quotes[o] > 1.0 for o in outcomes):
            implied = [1.0 / quotes[o] for o in outcomes]
            total = sum(implied)
            rows.append([p / total for p in implied])
    if not rows:
        return {}
    return {o: sum(r[i] for r in rows) / len(rows) for i, o in enumerate(outcomes)}


def normalize_event(raw: Mapping[str, Any], feed_key: str) -> NormalizedEvent | None:
    """One Odds API event (h2h market) onto the OmniRoute venues. Outcome names are matched to the home and away
    sides; ``Draw`` is the third outcome of a 3-way market. Returns None for an event without a usable price."""
    started = time.perf_counter()
    home, away = str(raw.get("home_team") or ""), str(raw.get("away_team") or "")
    sport = sport_for_odds_key(feed_key) or sport_for_odds_key(str(raw.get("sport_key") or ""))
    if not home or not away or sport is None:
        return None
    feed: dict[str, dict[str, float]] = {}
    has_draw = False
    for book in raw.get("bookmakers") or []:
        key = str(book.get("key") or "")
        for market in book.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            row: dict[str, float] = {}
            for outcome in market.get("outcomes") or []:
                name, price = str(outcome.get("name") or ""), outcome.get("price")
                try:
                    value = float(price)
                except (TypeError, ValueError):
                    continue
                side = "home" if name == home else "away" if name == away else "draw" if name.casefold() == "draw" else None
                if side is None or not value > 1.0:
                    continue
                has_draw = has_draw or side == "draw"
                row[side] = value
            if row:
                feed[key] = row
    venue_odds = canonical_quotes(feed)
    outcomes: tuple[str, ...] = ("home", "draw", "away") if has_draw else ("home", "away")
    if not venue_odds:
        return None
    event = NormalizedEvent(str(raw.get("id") or f"{feed_key}:{home}:{away}"), feed_key, sport, str(raw.get("commence_time") or ""),
                            home, away, venue_odds, outcomes)
    event.consensus = consensus_probabilities(venue_odds, outcomes)
    bench = sharp_benchmark(venue_odds, outcomes)
    event.sharp = bench.as_dict() if bench is not None else None
    event.normalization_ms = (time.perf_counter() - started) * 1000.0
    return event


def screen_event(event: Mapping[str, Any], *, min_edge: float) -> list[dict[str, Any]]:
    """+EV value lines on one stored board event, priced against the sharp benchmark (Group 96: no arbitrage)."""
    venue_odds: dict[str, dict[str, float]] = event["venue_odds"]
    outcomes: list[str] = list(event["outcomes"])
    found: list[dict[str, Any]] = []
    sharp = event.get("sharp")
    if sharp:
        fair = sharp["fair_probabilities"]
        for venue, quotes in venue_odds.items():
            if venue in SHARP_BOOKS:
                continue
            for o in outcomes:
                if o not in quotes or o not in fair:
                    continue
                ev = net_odds(quotes[o], DEFAULT_COMMISSION.get(venue, 0.0)) * fair[o] - 1.0
                if ev >= min_edge:
                    found.append({"kind": "value", "event_id": event["event_id"], "sport_key": event["sport_key"], "feed_key": event["feed_key"],
                                  "home": event["home"], "away": event["away"], "commence_time": event["commence_time"], "outcome": o,
                                  "venue": venue, "odds": quotes[o], "fair_probability": round(fair[o], 5), "benchmark": sharp["book"],
                                  "edge_pct": round(ev * 100.0, 3), "developer_credit": DEVELOPER_CREDIT})
    return found


# ================================================================ the service
class MultiSportOddsSyncService:
    """
    Omni-provider odds synchronization and line shopping engine.
    Ingests, normalizes, and compares odds across all active bookmakers and feeds:
    - The Odds API (Global multi-book consensus quotes)
    - Polymarket, Kalshi, Manifold (Prediction market wisdom-of-the-crowd probabilities)
    - Pinnacle (Sharp benchmark pricing and reference spreads)
    - Betfair (Exchange order book depth and zero-vig implied odds)
    - Parimatch (Primary capital account: direct feed injection and live micro-markets)
    - Stake & 1xBet (Retail bookmaker execution lines)
    - ESPN (Live in-play game state, scores, lineups, and injury feeds)
    - Open-Meteo (Venue weather, temperature, wind, and evening dew factor)
    - NewsAPI (Breaking squad news and steam move catalysts)
    """

    def __init__(self, api_key: str | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._override_key = SecretStr(api_key) if api_key else None
        self.odds_api_url = self.settings.ODDS_API_BASE_URL.rstrip("/")
        self.last_normalization_ms: list[float] = []

    # ------------------------------------------------------------------ credentials
    async def resolve_credential(self, db: AsyncSession | None, provider_name: str) -> ProviderCredential | None:
        spec = provider(provider_name)
        if spec is None:
            return None
        base_url = getattr(self.settings, spec.url_setting) if spec.url_setting else spec.base_url
        if spec.provider_id == "the_odds_api" and self._override_key is not None:
            return ProviderCredential(spec, self._override_key, base_url, "override")
        if db is not None and spec.vault_id:
            try:
                row = (await db.execute(select(VaultProviderCredential).where(VaultProviderCredential.provider_id == spec.vault_id,
                                                                              VaultProviderCredential.is_active.is_(True))
                                        .order_by(VaultProviderCredential.created_at))).scalars().first()
                if row is not None:
                    from app.services.vault.markdown_importer import provider_context  # noqa: PLC0415 - the importer is heavy

                    vault = VaultCrypto.from_settings(self.settings)
                    key = vault.decrypt_key(row.encrypted_api_key, context=provider_context(row.id, "api_key"))
                    return ProviderCredential(spec, SecretStr(key), row.base_url or base_url, "vault")
            except VaultConfigurationError:
                logger.warning("sports: the master vault key is not configured; %s falls back to settings", spec.provider_id)
            except Exception as exc:  # noqa: BLE001 - a broken vault row must not stop the poll
                logger.warning("sports: vault lookup for %s failed (%s); falling back to settings", spec.provider_id, type(exc).__name__)
        if spec.key_setting:
            # Group 112: The Odds API's key is read as the fleet reads it (THE_ODDS_API_KEY, else ODDS_API_KEY)
            value = self.settings.odds_api_env_key() if spec.provider_id == "the_odds_api" else getattr(self.settings, spec.key_setting, None)
            if isinstance(value, SecretStr) and value.get_secret_value():
                return ProviderCredential(spec, value, base_url, "settings")
        env = provider_secret(spec.env_key)
        if env is not None:
            return ProviderCredential(spec, env, base_url, "environment")
        return ProviderCredential(spec, None, base_url, "none")

    async def get_provider_credentials(self, db: AsyncSession | None, provider_name: str) -> dict[str, Any] | None:
        """The provider's public view (never its key): endpoint, category and whether a key is configured and where."""
        credential = await self.resolve_credential(db, provider_name)
        return None if credential is None else credential.public_view()

    async def provider_catalog(self, db: AsyncSession | None) -> list[dict[str, Any]]:
        out = []
        for spec in PROVIDERS:
            credential = await self.resolve_credential(db, spec.provider_id)
            if credential is not None:
                out.append(credential.public_view())
        return out

    async def get_active_sports_keys(self, db: AsyncSession) -> list[str]:
        """Feed keys the desk polls: every active sport's primary key, then its active leagues' feed keys."""
        sports = (await db.execute(select(SportConfiguration.sport_key, SportConfiguration.primary_odds_api_sport_key)
                                   .where(SportConfiguration.is_active.is_(True)).order_by(SportConfiguration.sport_key))).all()
        active = {s for s, _ in sports}
        keys = [k for _, k in sports if k]
        leagues = (await db.execute(select(LeagueRegistry.sport_key, LeagueRegistry.odds_api_sport_key)
                                    .where(LeagueRegistry.is_active.is_(True), LeagueRegistry.odds_api_sport_key.is_not(None))
                                    .order_by(LeagueRegistry.tier, LeagueRegistry.league_key))).all()
        keys += [k for s, k in leagues if s in active and k]
        return list(dict.fromkeys(keys))

    # ------------------------------------------------------------------ fetching
    async def fetch_board(self, client: httpx.AsyncClient, feed_key: str, credential: ProviderCredential) -> tuple[list[dict[str, Any]], int | None]:
        if credential.api_key is None:
            raise RuntimeError("no Odds API key configured")
        base = (credential.base_url or self.odds_api_url).rstrip("/")
        response = await client.get(f"{base}/sports/{feed_key}/odds", params={
            "apiKey": credential.api_key.get_secret_value(), "regions": self.settings.SPORTS_ODDS_REGIONS, "markets": "h2h", "oddsFormat": "decimal",
        })
        response.raise_for_status()
        remaining_raw = response.headers.get("x-requests-remaining")
        try:
            remaining = int(float(remaining_raw)) if remaining_raw is not None else None
        except ValueError:
            remaining = None
        payload = response.json()
        return (payload if isinstance(payload, list) else []), remaining

    def normalize_board(self, raw_events: Sequence[Mapping[str, Any]], feed_key: str) -> list[NormalizedEvent]:
        events = []
        timings = []
        for raw in raw_events:
            event = normalize_event(raw, feed_key)
            if event is not None:
                events.append(event)
                timings.append(event.normalization_ms)
                if event.normalization_ms > self.settings.SPORTS_NORMALIZATION_BUDGET_MS:
                    logger.warning("sports: FA-3 budget exceeded normalising %s (%.2f ms > %.1f ms)", event.event_id, event.normalization_ms,
                                   self.settings.SPORTS_NORMALIZATION_BUDGET_MS)
        self.last_normalization_ms = timings
        return events

    async def poll_consensus(self, sessions: async_sessionmaker[AsyncSession], redis: Redis, now: datetime, *,
                             transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
        """One beat tick of ``poll_multi_sport_consensus_odds``."""
        s = self.settings
        async with sessions() as db:
            from app.services.sports_catalog import ensure_seeded  # noqa: PLC0415 - avoids an import cycle with the catalogue service

            await ensure_seeded(db)
            feed_keys = await self.get_active_sports_keys(db)
            credential = await self.resolve_credential(db, "the_odds_api")
        if s.SPORTS_ODDS_SOURCE == "store":
            return await self._poll_from_store(sessions, redis, feed_keys)
        report: dict[str, Any] = {"feed_keys": len(feed_keys), "fetched": [], "skipped": 0, "events": 0, "errors": {}, "quota_remaining": None,
                                  "max_normalization_ms": 0.0}
        if credential is None or credential.api_key is None:
            report["reason"] = "no Odds API key configured (vault, settings or environment)"
            return report
        async with httpx.AsyncClient(timeout=s.WIRE_HTTP_TIMEOUT_SECONDS, transport=transport) as client:
            for feed_key in feed_keys:
                claimed = await redis.set(f"{s.SPORTS_PREFIX}:fetched:{feed_key}", now.isoformat(), nx=True, ex=int(s.SPORTS_FEED_MIN_INTERVAL_SECONDS))
                if not claimed:
                    report["skipped"] += 1
                    continue
                try:
                    raw, remaining = await self.fetch_board(client, feed_key, credential)
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    report["errors"][feed_key] = type(exc).__name__
                    continue
                events = self.normalize_board(raw, feed_key)
                await redis.set(board_key(s, feed_key), json.dumps({"feed_key": feed_key, "fetched_at": now.isoformat(),
                                                                     "events": [e.as_dict() for e in events]}), ex=s.SPORTS_BOARD_TTL_SECONDS)
                report["fetched"].append(feed_key)
                report["events"] += len(events)
                report["max_normalization_ms"] = max([report["max_normalization_ms"], *self.last_normalization_ms])
                report["quota_remaining"] = remaining
                if remaining is not None and remaining < s.ODDS_QUOTA_FLOOR:
                    report["reason"] = f"quota at {remaining}, under the {s.ODDS_QUOTA_FLOOR} floor: polling stopped this tick"
                    break
        return report

    async def _poll_from_store(self, sessions: async_sessionmaker[AsyncSession], redis: Redis, feed_keys: Sequence[str]) -> dict[str, Any]:
        """Group 112: each feed key's board from the one the ingestion fleet last bought, stamped with when it was bought.
        A feed key the fleet does not buy (not in ODDS_SPORT_KEYS) is listed as missing, never fetched here."""
        from app.services import pre_match_odds_service as store  # noqa: PLC0415 - the store pulls in the fleet's cadence

        s = self.settings
        report: dict[str, Any] = {"source": "store", "feed_keys": len(feed_keys), "fetched": [], "missing": [], "skipped": 0, "events": 0,
                                  "errors": {}, "quota_remaining": None, "max_normalization_ms": 0.0}
        async with sessions() as db:
            boards = await store.stored_boards(db, feed_keys, datetime.now(UTC), s)
        for feed_key in feed_keys:
            rows = boards.get(feed_key)
            if not rows:
                report["missing"].append(feed_key)
                continue
            bought = max(store.price_time(row) for row in rows)
            events = self.normalize_board([store.snapshot_event(row, polled_at=store.price_time(row)) for row in rows], feed_key)
            await redis.set(board_key(s, feed_key), json.dumps({"feed_key": feed_key, "fetched_at": bought.isoformat(), "source": "store",
                                                                 "events": [e.as_dict() for e in events]}), ex=s.SPORTS_BOARD_TTL_SECONDS)
            report["fetched"].append(feed_key)
            report["events"] += len(events)
            report["max_normalization_ms"] = max([report["max_normalization_ms"], *self.last_normalization_ms])
        if report["missing"]:
            report["reason"] = (f"{len(report['missing'])} feed key(s) not bought by the ingestion fleet (add them to ODDS_SPORT_KEYS): "
                                f"{', '.join(report['missing'][:8])}")
        return report

    async def stored_boards(self, redis: Redis) -> list[dict[str, Any]]:
        boards = []
        async for key in redis.scan_iter(match=f"{self.settings.SPORTS_PREFIX}:board:*", count=200):
            raw = await redis.get(key)
            if raw:
                try:
                    boards.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return boards

    async def screen_value_lines(self, redis: Redis, now: datetime) -> dict[str, Any]:
        """One beat tick of ``screen_cross_provider_arbitrage`` (historical task name): the +EV value lines."""
        s = self.settings
        edges: list[dict[str, Any]] = []
        boards = await self.stored_boards(redis)
        stale = 0
        for board in boards:
            # Group 112: a value line is only as good as its price; an old board lists none
            try:
                fetched = datetime.fromisoformat(str(board.get("fetched_at")))
            except ValueError:
                fetched = None
            if fetched is None or fetched.tzinfo is None or (now - fetched).total_seconds() > s.SPORTS_VALUE_LINE_MAX_AGE_SECONDS:
                stale += 1
                continue
            for event in board.get("events", []):
                edges.extend(screen_event(event, min_edge=s.SPORTS_MIN_EDGE))
        edges.sort(key=lambda e: e.get("edge_pct", 0.0), reverse=True)
        edges = edges[: s.SPORTS_MAX_EDGES]
        payload = json.dumps({"screened_at": now.isoformat(), "edges": edges})
        try:
            await redis.set(edges_key(s), payload, ex=s.SPORTS_BOARD_TTL_SECONDS)
            if edges:
                await redis.publish(edges_channel(s), payload)
        except RedisError as exc:
            logger.warning("sports: storing the edge list failed: %s", exc)
        return {"boards": len(boards), "stale_boards": stale, "edges": len(edges), "value_lines": sum(1 for e in edges if e["kind"] == "value")}

    async def latest_edges(self, redis: Redis) -> dict[str, Any]:
        raw = await redis.get(edges_key(self.settings))
        return json.loads(raw) if raw else {"screened_at": None, "edges": []}

    # ------------------------------------------------------------------ model line shopping
    async def screen_multi_provider_edges(
        self,
        sport_key: str,
        match_id: str,
        home_team: str,
        away_team: str,
        venue_odds: dict[str, dict[str, float]],
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Performs multi-venue line shopping across all bookmaker quotes
        (Pinnacle, Betfair, The Odds API, Parimatch, Stake, 1xBet),
        compares against the quantitative sport adapter fair odds,
        and identifies positive EV value bets (single venue, best net price; Group 96 retired arbitrage).
        """
        adapter = SportAdapterRegistry.get_adapter(sport_key)
        fair = adapter.calculate_fair_odds(home_stats, away_stats, context)
        quotes: dict[str, dict[str, float]] = {}
        for venue, row in venue_odds.items():
            key = canonical_bookmaker(venue) or str(venue).strip().casefold()
            bucket = quotes.setdefault(key, {})
            for outcome, price in row.items():
                if isinstance(price, (int, float)) and price > 1.0:
                    bucket[outcome] = max(bucket.get(outcome, 0.0), float(price))
        sides = [("home", home_team), ("away", away_team)] + ([("draw", "Draw")] if fair.draw_prob > 0.02 and any("draw" in q for q in quotes.values()) else [])
        bench = sharp_benchmark(quotes, [s for s, _ in sides])
        edges: list[dict[str, Any]] = []
        best: dict[str, tuple[str, float, float]] = {}
        for side, _ in sides:
            for venue, row in quotes.items():
                if side in row:
                    n = net_odds(row[side], DEFAULT_COMMISSION.get(venue, 0.0))
                    if side not in best or n > best[side][2]:
                        best[side] = (venue, row[side], n)
        for side, label in sides:
            if side not in best:
                continue
            venue, odds, n = best[side]
            p = fair.probability_of(side)
            ev = p * n - 1.0
            if ev <= self.settings.SPORTS_MIN_EDGE:
                continue
            route = route_best_price({v: r[side] for v, r in quotes.items() if side in r}, bet_kind="single")
            verdict = consensus_verdict(fair.sub_models, side, n, None, min_models=3, min_ev=self.settings.TWIN_MIN_CONSENSUS_EV)
            sharp_p = bench.fair_probabilities.get(side) if bench else None
            confidence = confidence_index(sport_key=adapter.sport_key, agreement=fair.consensus.agreement if fair.consensus else 0.0,
                                          model_count=len(fair.sub_models), model_probability=p, sharp_probability=sharp_p, edge=ev,
                                          evidence=fair.evidence, consensus_status=verdict.status)
            edges.append({
                "match_id": match_id, "sport_key": adapter.sport_key, "selection": label, "side": side, "best_venue": venue,
                "bookmaker_odds": odds, "net_odds": round(n, 4), "fair_odds": round(1.0 / p, 3), "true_probability": round(p, 4),
                "edge_pct": round(ev * 100.0, 2), "is_sharp_backed": venue in SHARP_BOOKS,
                "sharp_fair_probability": None if sharp_p is None else round(sharp_p, 4),
                "route": None if route is None else route.as_dict(), "pillar_1": verdict.as_dict(), "confidence": confidence.as_dict(),
                "kelly_multiplier": fair.kelly_multiplier, "developer_credit": DEVELOPER_CREDIT,
            })
        return edges
