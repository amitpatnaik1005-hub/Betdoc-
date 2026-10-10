"""Parimatch direct odds injection under ``/api/v1/parimatch`` (Group 70).

The Odds API does not reliably carry Parimatch, and Parimatch publishes no odds API. This is the door
for Parimatch prices that come from an authorised source: the user typing what the site shows, or a
licensed feed that pushes to the webhook. Nothing here fetches a bookmaker's site.

    POST /parimatch/odds       admin login: post prices, normalised straight into the quote stream
    POST /parimatch/webhook    the same for a feed without a login: X-Parimatch-Feed-Token (PARIMATCH_FEED_TOKEN)
    GET  /parimatch/status     the last injection

Each event's markets are normalised to the platform grammar (``"1X2"`` -> ``Match Odds``, ``"Total 2.5"`` ->
``Totals 2.5``, ``"Handicap (-0.5)"`` -> ``Asian Handicap -0.5``, ``"Both Teams To Score"`` -> ``BTTS``) and
their selections to its codes (``1`` / ``W1`` / the home team -> HOME, ``X`` -> DRAW, ``Over`` -> OVER...).
The fixture id is the one the odds normaliser gives the same match, so these prices sit alongside
``onexbet``'s in the Aryabhata frames (source ``parimatch_direct``, book ``parimatch``) and Ashoka
compares them like any other. Prices older than ten minutes are refused: a stale price is worse than none.
"""

from __future__ import annotations

import hmac
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.deps import CurrentAdmin
from app.core.config import Settings, get_settings
from app.domain.oracle.markets import MarketKind, MarketRef, parse_market
from app.schemas.aryabhata import BookQuote, MarketQuote
from app.services.aryabhata_pipeline import publish_market_quotes

logger = logging.getLogger("betdoc.parimatch.feed")

router = APIRouter(prefix="/parimatch", tags=["Parimatch · direct odds"])

AppSettings = Annotated[Settings, Depends(get_settings)]
SOURCE = "parimatch_direct"
BOOK = "parimatch"
MAX_AGE = timedelta(minutes=10)
MIN_ODDS, MAX_ODDS = Decimal("1.01"), Decimal("1000")


class FeedMarket(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    market: str = Field(min_length=1, max_length=64)  # "1X2", "Total 2.5", "Both Teams To Score", "Asian Handicap -0.5"
    prices: dict[str, Decimal] = Field(min_length=1, max_length=12)  # selection -> decimal odds, as Parimatch shows them


class FeedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    fixture_id: str | None = Field(default=None, max_length=128)  # BetDoc's id, when the caller knows it
    home: str = Field(min_length=1, max_length=128)
    away: str = Field(min_length=1, max_length=128)
    sport_key: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
    kickoff: datetime
    markets: list[FeedMarket] = Field(min_length=1, max_length=50)


class ParimatchFeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[FeedEvent] = Field(min_length=1, max_length=2000)
    observed_at: datetime | None = None  # when the prices were read; default now


_MARKET_SYNONYMS = (
    (re.compile(r"^(?:1x2|match result|full ?time result|ft result|result|winner|match winner|moneyline|to win)$", re.I), "Match Odds"),
    (re.compile(r"^(?:both teams to score|both teams score|btts|gg/ng|goal/no goal)$", re.I), "BTTS"),
    (re.compile(r"^(?:total|totals|total goals|over/under|o/u)\s*:?\s*\(?\s*([0-9]+(?:\.[0-9]+)?)\s*\)?$", re.I), "Totals {0}"),
    (re.compile(r"^(?:asian handicap|handicap|ah|spread)\s*:?\s*\(?\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s*\)?$", re.I), "Asian Handicap {0}"),
    (re.compile(r"^(?:double chance|dc)$", re.I), "Double Chance"),
    (re.compile(r"^(?:draw no bet|dnb)$", re.I), "Draw No Bet"),
)


def normalise_market(raw: str) -> MarketRef | None:
    text = " ".join(raw.split())
    for pattern, template in _MARKET_SYNONYMS:
        match = pattern.match(text)
        if match:
            text = template.format(*match.groups())
            break
    try:
        return parse_market(text)
    except ValueError:
        return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def normalise_selection(ref: MarketRef, raw: str, home: str, away: str) -> str | None:
    s = _norm(raw)
    team_home, team_away = _norm(home), _norm(away)
    if ref.kind in (MarketKind.MATCH_ODDS, MarketKind.ASIAN_HANDICAP, MarketKind.DRAW_NO_BET):
        if s in ("1", "w1", "p1", "h1", "home", "hometeam") or (team_home and s.startswith(team_home)):
            return "HOME"
        if s in ("2", "w2", "p2", "h2", "away", "awayteam") or (team_away and s.startswith(team_away)):
            return "AWAY"
        if ref.kind is MarketKind.MATCH_ODDS and s in ("x", "draw", "tie"):
            return "DRAW"
        return None
    if ref.kind is MarketKind.TOTALS:
        line = f"{ref.line:g}".replace(".", "")
        stripped = s.removesuffix(line) if line and s.endswith(line) and s != line else s
        return {"over": "OVER", "o": "OVER", "tb": "OVER", "under": "UNDER", "u": "UNDER", "tm": "UNDER"}.get(stripped)
    if ref.kind is MarketKind.BTTS:
        return {"yes": "YES", "gg": "YES", "y": "YES", "no": "NO", "ng": "NO", "n": "NO"}.get(s)
    if ref.kind is MarketKind.DOUBLE_CHANCE:
        return {"1x": "1X", "12": "12", "x2": "X2", "2x": "X2", "x1": "1X", "21": "12"}.get(s)
    return None


def fixture_id_for(event: FeedEvent) -> str:
    if event.fixture_id:
        return event.fixture_id
    from app.services.omni_normalizer import default_alias_dictionary  # noqa: PLC0415

    aliases = default_alias_dictionary()
    home, away = aliases.resolve(event.sport_key, event.home), aliases.resolve(event.sport_key, event.away)
    return aliases.match_id(event.sport_key, home.id, away.id, event.kickoff.astimezone(UTC))


def build_frames(feed: ParimatchFeed, now: datetime) -> tuple[list[MarketQuote], list[dict[str, Any]]]:
    observed = (feed.observed_at or now).astimezone(UTC)
    if observed > now + timedelta(minutes=1):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="observed_at is in the future")
    if now - observed > MAX_AGE:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="these prices are over ten minutes old: re-check them on Parimatch")
    frames: list[MarketQuote] = []
    rejected: list[dict[str, Any]] = []
    for i, event in enumerate(feed.events):
        kickoff = event.kickoff if event.kickoff.tzinfo else event.kickoff.replace(tzinfo=UTC)
        if kickoff < now - timedelta(hours=4):
            rejected.append({"event": i, "reason": "kicked off over four hours ago"})
            continue
        try:
            fixture = fixture_id_for(event.model_copy(update={"kickoff": kickoff}))
        except Exception:  # noqa: BLE001 - an unmatched name never breaks the batch
            rejected.append({"event": i, "reason": "the teams could not be matched to a fixture: send fixture_id"})
            continue
        for j, market in enumerate(event.markets):
            ref = normalise_market(market.market)
            if ref is None:
                rejected.append({"event": i, "market": j, "reason": f"unknown market {market.market[:40]!r}"})
                continue
            prices: dict[str, Decimal] = {}
            bad = None
            for raw, odds in market.prices.items():
                code = normalise_selection(ref, raw, event.home, event.away)
                if code is None:
                    bad = f"selection {raw[:30]!r} is not one of {'/'.join(ref.selections)}"
                    break
                if not MIN_ODDS <= odds <= MAX_ODDS:
                    bad = f"odds {odds} for {code} are outside {MIN_ODDS}-{MAX_ODDS}"
                    break
                if code in prices:
                    bad = f"{code} is given twice"
                    break
                prices[code] = odds
            if bad is None and len(prices) < 2:
                bad = "a market needs at least two selections priced"
            if bad is not None:
                rejected.append({"event": i, "market": j, "reason": bad})
                continue
            frames.append(MarketQuote(
                match_id=fixture, market_type=ref.key, home_team=event.home, away_team=event.away, sport_key=event.sport_key, commence_time=kickoff,
                source=SOURCE, fetched_at=now, books=(BookQuote(bookmaker_id=BOOK, prices=prices, observed_at=observed),),
            ))
    return frames, rejected


def _status_key(settings: Settings) -> str:
    return f"{settings.omni_redis_prefix}:parimatch:feed"


async def _inject(request: Request, feed: ParimatchFeed, settings: Settings, by: str) -> dict[str, Any]:
    if len(feed.events) > settings.PARIMATCH_FEED_MAX_EVENTS:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"at most {settings.PARIMATCH_FEED_MAX_EVENTS} events per post")
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is down: prices cannot reach the quote stream")
    now = datetime.now(UTC)
    frames, rejected = build_frames(feed, now)
    published = await publish_market_quotes(redis, frames, settings) if frames else False
    try:
        await redis.hset(_status_key(settings), mapping={"last_at": now.isoformat(), "frames": len(frames), "events": len(feed.events), "rejected": len(rejected), "by": by})
    except (RedisError, OSError):
        pass
    logger.info("Parimatch injection by %s: %d frames from %d events, %d rejected", by, len(frames), len(feed.events), len(rejected))
    return {"frames": len(frames), "events": len(feed.events), "published": bool(published), "rejected": rejected,
            "markets": sorted({f.market_type for f in frames}), "fixtures": sorted({f.match_id for f in frames})}


@router.post("/odds")
async def inject_odds(feed: ParimatchFeed, request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:
    return await _inject(request, feed, settings, f"user:{admin.username}")


@router.post("/webhook")
async def inject_webhook(
    feed: ParimatchFeed, request: Request, settings: AppSettings, token: Annotated[str | None, Header(alias="X-Parimatch-Feed-Token")] = None,
) -> dict[str, Any]:
    expected = settings.PARIMATCH_FEED_TOKEN.get_secret_value() if settings.PARIMATCH_FEED_TOKEN else ""
    if not expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="the Parimatch webhook is off (PARIMATCH_FEED_TOKEN)")
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad feed token")
    return await _inject(request, feed, settings, "webhook")


@router.get("/status")
async def feed_status(request: Request, admin: CurrentAdmin, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis: Redis | None = getattr(request.app.state, "redis", None)
    try:
        last = await redis.hgetall(_status_key(settings)) if redis is not None else {}
    except (RedisError, OSError):
        last = {}
    return {"source": SOURCE, "book": BOOK, "last": last or None, "webhook_enabled": bool(settings.PARIMATCH_FEED_TOKEN and settings.PARIMATCH_FEED_TOKEN.get_secret_value())}
