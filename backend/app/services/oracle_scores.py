"""Final scores for the user's placed bets, from The Odds API's scores feed (Group 69).

Quota first. A poll happens only for a sport that has a pending user leg whose match should be over
(kicked off more than 1h45 ago, less than three days ago), at most once per sport every
``ORACLE_SCORES_POLL_MINUTES`` (a Redis claim shared by every process), and never when the fleet's last
quota reading is under ``ODDS_QUOTA_FLOOR`` or its reserve. One call (``daysFrom=3``) costs two credits;
the reading in its response headers is written back to the fleet's metrics, so Fleet Command sees it.
The key is the fleet's own (Fleet Command's vault, then the environment). Nothing polls without one.

Each completed event becomes a ``fixture_scores`` row under Ashoka's canonical fixture id (the same
alias dictionary the odds normaliser uses), then ``settle_pending`` settles whatever it decides.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultCrypto
from app.models.user_bets_ledger import PlacedStatus, ScoreStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.ashoka import ScoreIn
from app.services.user_pnl_tracker import bump, record_score, settle_pending

logger = logging.getLogger("betdoc.ashoka.scores")

FINISHED_AFTER = timedelta(minutes=105)
LOOKBACK = timedelta(days=3)
SOURCE_ID = "odds_api"


async def sports_awaiting_scores(session: AsyncSession, now: datetime) -> set[str]:
    rows = await session.execute(
        select(UserPlacedLeg.sport_key)
        .join(UserPlacedBet, UserPlacedBet.id == UserPlacedLeg.bet_id)
        .where(
            UserPlacedBet.status == PlacedStatus.PENDING.value,
            UserPlacedLeg.result == PlacedStatus.PENDING.value,
            UserPlacedLeg.sport_key.is_not(None),
            UserPlacedLeg.kickoff <= now - FINISHED_AFTER,
            UserPlacedLeg.kickoff >= now - LOOKBACK,
        )
        .distinct()
    )
    return {s for s in rows.scalars() if s}


async def _api_key(session_factory: async_sessionmaker[AsyncSession], settings: Settings, vault: VaultCrypto | None) -> str | None:
    from app.services.omni_fleet import resolve_api_key  # noqa: PLC0415 - the fleet module is heavy
    from app.services.omni_normalizer import default_alias_dictionary  # noqa: PLC0415
    from app.services.omni_router import load_registry  # noqa: PLC0415

    async with session_factory() as session:
        registry, rows = await load_registry(session, settings, default_alias_dictionary())
    descriptor = registry.get(SOURCE_ID)
    if descriptor is None:
        return None
    try:
        return resolve_api_key(rows.get(SOURCE_ID), descriptor, settings, vault)
    except Exception:  # noqa: BLE001 - an undecryptable key means no poll, never a crash
        return None


async def _quota_allows(redis: Redis, settings: Settings) -> bool:
    try:
        remaining, fraction = await redis.hmget(OmniRedisKeys(settings.omni_redis_prefix).fleet_metrics(SOURCE_ID), ["quota_remaining", "quota_fraction"])
    except (RedisError, OSError):
        return False
    try:
        if remaining not in (None, "") and float(remaining) < settings.ODDS_QUOTA_FLOOR:
            return False
        if fraction not in (None, "") and float(fraction) < settings.OMNI_FLEET_QUOTA_RESERVE:
            return False
    except (TypeError, ValueError):
        return True
    return True


def parse_scores(events: Any, sport: str) -> list[ScoreIn]:
    """Completed events with both scores, as ``ScoreIn`` (fixture ids are filled in by the caller)."""
    out: list[ScoreIn] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or not event.get("completed"):
            continue
        home, away = event.get("home_team"), event.get("away_team")
        scores = {str(s.get("name")): s.get("score") for s in event.get("scores") or [] if isinstance(s, dict)}
        try:
            home_goals, away_goals = int(scores[home]), int(scores[away])
            kickoff = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        out.append(ScoreIn(home=home, away=away, sport_key=sport, kickoff=kickoff, home_goals=home_goals, away_goals=away_goals, status=ScoreStatus.FINAL))
    return out


def canonical_fixture_id(score: ScoreIn) -> str | None:
    from app.services.omni_normalizer import default_alias_dictionary  # noqa: PLC0415

    aliases = default_alias_dictionary()
    if score.sport_key is None or score.kickoff is None:
        return None
    home, away = aliases.resolve(score.sport_key, score.home), aliases.resolve(score.sport_key, score.away)
    return aliases.match_id(score.sport_key, home.id, away.id, score.kickoff.astimezone(UTC))


async def poll_scores(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, vault: VaultCrypto | None, *, http: httpx.AsyncClient | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    result: dict[str, Any] = {"polled": [], "skipped": {}, "scores": 0, "settled_bets": 0}
    if not settings.ORACLE_SCORES_POLL_ENABLED:
        result["skipped"]["*"] = "disabled"
        return result
    async with session_factory() as session:
        sports = await sports_awaiting_scores(session, now)
    if not sports:
        return result
    key = await _api_key(session_factory, settings, vault)
    if not key:
        result["skipped"]["*"] = "no Odds API key"
        return result
    own = http is None
    client = http or httpx.AsyncClient(timeout=20)
    try:
        for sport in sorted(sports):
            if not await _quota_allows(redis, settings):
                result["skipped"][sport] = "quota floor"
                continue
            try:
                claimed = await redis.set(f"oracle:scores:poll:{sport}", now.isoformat(), nx=True, ex=int(settings.ORACLE_SCORES_POLL_MINUTES * 60))
            except (RedisError, OSError):
                claimed = False
            if not claimed:
                result["skipped"][sport] = "polled recently"
                continue
            url = f"{settings.ODDS_API_BASE_URL.rstrip('/')}/sports/{sport}/scores/"
            try:
                response = await client.get(url, params={"apiKey": key, "daysFrom": 3, "dateFormat": "iso"})
            except httpx.HTTPError as exc:
                result["skipped"][sport] = type(exc).__name__  # never the URL: it carries the key
                continue
            await _record_quota(redis, settings, response, now)
            if response.status_code != 200:
                result["skipped"][sport] = f"HTTP {response.status_code}"
                continue
            parsed = parse_scores(response.json(), sport)
            async with session_factory() as session:
                for score in parsed:
                    score.fixture_id = canonical_fixture_id(score)
                    await record_score(session, score, source="odds_api_scores", by=None, now=now)
                await session.commit()
            result["polled"].append(sport)
            result["scores"] += len(parsed)
    finally:
        if own:
            await client.aclose()
    report = await settle_pending(session_factory, now)
    await bump(redis, report.users)
    result["settled_bets"] = report.bets
    return result


async def _record_quota(redis: Redis, settings: Settings, response: httpx.Response, now: datetime) -> None:
    remaining, used = response.headers.get("x-requests-remaining"), response.headers.get("x-requests-used")
    if remaining is None:
        return
    mapping: dict[str, Any] = {"quota_remaining": remaining, "quota_checked_at": now.timestamp()}
    try:
        if used is not None:
            total = float(remaining) + float(used)
            mapping["quota_used"] = used
            mapping["quota_limit"] = total
            mapping["quota_fraction"] = round(float(remaining) / total, 6) if total > 0 else ""
    except ValueError:
        pass
    try:
        await redis.hset(OmniRedisKeys(settings.omni_redis_prefix).fleet_metrics(SOURCE_ID), mapping=mapping)
    except (RedisError, OSError):
        pass
