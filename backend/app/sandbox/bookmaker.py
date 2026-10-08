"""Sandbox bookmaker: a simulated execution venue that speaks the generic order API for real.

It runs in-process (an ``httpx.ASGITransport`` client; its host, ``sandbox.invalid``, can never
resolve), so the live pipeline can be exercised end to end without a partner account:

* ``POST /oauth/token``: client-credentials and refresh-token grants; access tokens expire
  (``SNIPER_SANDBOX_TOKEN_TTL_SECONDS``), so session refresh is real.
* ``GET /events``: its catalog is the fixtures on BetDoc's live board, under its own ids.
* ``POST /bets``: bearer auth (401 when missing or expired), its own rate limit (429 above
  ``SNIPER_SANDBOX_BETS_PER_SECOND``), idempotent per ``client_ref``, priced off the live board:
  a price below ``min_acceptable_odds`` is refused (409 ``PRICE_BELOW_MINIMUM``).
* ``GET /bets``: "my bets", graded from BetDoc's recorded market results.

No money moves anywhere. The credentials derive from SECRET_KEY, so nothing secret is hardcoded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Form, Header, Query, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.ingestion.base import ThrottledError
from app.core.config import Settings
from app.core.live_odds import read_snapshot
from app.models.cfo_vault import MarketResult
from app.services.omni_throttle import RateLimit, TokenBucket

SANDBOX_BASE_URL = "http://sandbox.invalid"
CLIENT_ID = "betdoc-sandbox"
LABEL_CODES = {"HOME": "H", "DRAW": "D", "AWAY": "A"}
CODE_LABELS = {v: k for k, v in LABEL_CODES.items()}
MARKET = "Match Odds"


def sandbox_credentials(settings: Settings) -> dict[str, str]:
    """Deterministic per deployment (HMAC of SECRET_KEY), so a restart never orphans the stored copy."""
    secret = hmac.new(settings.SECRET_KEY.get_secret_value().encode(), b"betdoc-sniper-sandbox", hashlib.sha256).hexdigest()[:40]
    return {"client_id": CLIENT_ID, "client_secret": secret}


def event_id_for(match_id: str) -> str:
    return "SBX" + hashlib.sha1(match_id.encode()).hexdigest()[:10].upper()


def _err(status: int, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, **extra})


def build_sandbox_app(redis: Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> FastAPI:
    prefix = f"{settings.SNIPER_PREFIX}:sandbox"
    bucket = TokenBucket(redis, f"{prefix}:rate", max_wait_seconds=0.0)
    limit = RateLimit(requests_per_minute=settings.SNIPER_SANDBOX_BETS_PER_SECOND * 60, burst=max(1, int(settings.SNIPER_SANDBOX_BETS_PER_SECOND)))
    router = APIRouter()

    async def authorised(authorization: str | None) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        return bool(await redis.exists(f"{prefix}:token:{authorization[7:]}"))

    async def issue() -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ttl = settings.SNIPER_SANDBOX_TOKEN_TTL_SECONDS
        await redis.set(f"{prefix}:token:{access}", "1", ex=ttl)
        await redis.set(f"{prefix}:refresh:{refresh}", "1", ex=86_400)
        return {"access_token": access, "token_type": "bearer", "expires_in": ttl, "refresh_token": refresh}

    @router.post("/oauth/token")
    async def token(
        grant_type: Annotated[str, Form()],
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
        refresh_token: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        if grant_type == "client_credentials":
            expected = sandbox_credentials(settings)
            ok = hmac.compare_digest((client_id or "").encode(), expected["client_id"].encode()) and hmac.compare_digest(
                (client_secret or "").encode(), expected["client_secret"].encode()
            )
            return JSONResponse(await issue()) if ok else _err(401, "invalid_client")
        if grant_type == "refresh_token" and refresh_token:
            if await redis.delete(f"{prefix}:refresh:{refresh_token}"):  # single use: it rotates
                return JSONResponse(await issue())
            return _err(400, "invalid_grant")
        return _err(400, "unsupported_grant_type")

    async def board() -> dict[str, dict[str, Any]]:
        """The live board grouped by fixture: names, kick-off, and each selection's best price."""
        events: dict[str, dict[str, Any]] = {}
        for tick in await read_snapshot(redis) or []:
            if tick.market_type != MARKET or tick.selection not in LABEL_CODES:
                continue
            event = events.setdefault(
                tick.match_id,
                {"home": tick.home_team, "away": tick.away_team, "sport_key": tick.sport_key or "", "commence_time": tick.commence_time, "prices": {}},
            )
            if not tick.is_suspended:
                event["prices"][tick.selection] = tick.odds
        return events

    @router.get("/events")
    async def events(authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
        if not await authorised(authorization):
            return _err(401, "invalid_token")
        catalog = []
        index: dict[str, str] = {}
        for match_id, event in (await board()).items():
            if event["commence_time"] is None or not event["sport_key"]:
                continue
            eid = event_id_for(match_id)
            index[eid] = match_id
            catalog.append(
                {
                    "id": eid,
                    "sport_key": event["sport_key"],
                    "home": event["home"],
                    "away": event["away"],
                    "commence_time": event["commence_time"].isoformat(),
                    "outcomes": {label: f"{eid}-{LABEL_CODES[label]}" for label in event["prices"]},
                }
            )
        if index:
            await redis.hset(f"{prefix}:events", mapping=index)
            await redis.expire(f"{prefix}:events", 86_400 * 7)
        return JSONResponse({"events": catalog})

    @router.post("/bets")
    async def place(request: Request, authorization: Annotated[str | None, Header()] = None) -> JSONResponse:
        if not await authorised(authorization):
            return _err(401, "invalid_token")
        try:
            await bucket.acquire(CLIENT_ID, limit)
        except ThrottledError:
            return _err(429, "rate_limited")
        try:
            body = await request.json()
            client_ref = str(body["client_ref"])
            event_id, selection_id = str(body["event_id"]), str(body["selection_id"])
            odds, floor, stake = Decimal(str(body["odds"])), Decimal(str(body["min_acceptable_odds"])), Decimal(str(body["stake"]))
        except (ValueError, KeyError, TypeError, InvalidOperation, json.JSONDecodeError):
            return _err(422, "invalid_order")
        if not (odds > 1 and floor > 1 and stake > 0 and floor <= odds):
            return _err(422, "invalid_order")
        existing = await redis.hget(f"{prefix}:refs", client_ref)
        if existing:  # the same order again (a retry after a 401, say): the same bet, never a second one
            bet = json.loads(await redis.hget(f"{prefix}:bets", existing) or "{}")
            return JSONResponse({"remote_bet_id": existing, "status": bet.get("status", "OPEN"), "matched_odds": bet.get("matched_odds")})
        match_id = await redis.hget(f"{prefix}:events", event_id)
        label = CODE_LABELS.get(selection_id.rsplit("-", 1)[-1]) if selection_id.startswith(f"{event_id}-") else None
        if match_id is None or label is None:
            return _err(404, "unknown_selection")
        current = (await board()).get(match_id, {}).get("prices", {}).get(label)
        if current is None:
            return _err(409, "MARKET_UNAVAILABLE")
        if current < floor:
            return _err(409, "PRICE_BELOW_MINIMUM", current_odds=str(current))
        matched = odds if current >= odds else current  # filled at the asked price, or the slipped one above the floor
        remote_id = f"SBX-B-{uuid.uuid4().hex[:12].upper()}"
        bet = {
            "client_ref": client_ref, "event_id": event_id, "selection_id": selection_id, "fixture_id": match_id, "selection": label,
            "odds": str(odds), "matched_odds": str(matched), "stake": str(stake), "placed_at": datetime.now(UTC).isoformat(),
        }
        await redis.hset(f"{prefix}:bets", remote_id, json.dumps(bet))
        await redis.hset(f"{prefix}:refs", client_ref, remote_id)
        for key in (f"{prefix}:bets", f"{prefix}:refs"):
            await redis.expire(key, 86_400 * 30)
        return JSONResponse({"remote_bet_id": remote_id, "status": "OPEN", "matched_odds": str(matched)})

    @router.get("/bets")
    async def my_bets(
        authorization: Annotated[str | None, Header()] = None, ids: Annotated[str, Query()] = "", client_refs: Annotated[str, Query()] = ""
    ) -> JSONResponse:
        if not await authorised(authorization):
            return _err(401, "invalid_token")
        wanted = [i for i in ids.split(",") if i]
        for ref in (r for r in client_refs.split(",") if r):
            remote = await redis.hget(f"{prefix}:refs", ref)
            wanted.append(remote or f"ref:{ref}")
        rows = []
        async with session_factory() as session:
            for remote in wanted:
                if remote.startswith("ref:"):
                    rows.append({"remote_bet_id": None, "client_ref": remote[4:], "status": "NOT_FOUND"})
                    continue
                raw = await redis.hget(f"{prefix}:bets", remote)
                if raw is None:
                    rows.append({"remote_bet_id": remote, "status": "NOT_FOUND"})
                    continue
                bet = json.loads(raw)
                result = await session.scalar(select(MarketResult).where(MarketResult.fixture_id == bet["fixture_id"], MarketResult.market == MARKET))
                status = "OPEN" if result is None else "VOID" if result.is_void else ("WON" if result.winning_selection == bet["selection"] else "LOST")
                rows.append({"remote_bet_id": remote, "client_ref": bet["client_ref"], "status": status, "matched_odds": bet["matched_odds"]})
        return JSONResponse({"bets": rows})

    app = FastAPI(title="BetDoc sandbox bookmaker", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    return app
