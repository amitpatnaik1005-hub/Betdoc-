"""The Pocket Twin (Group 109, point b.10): what the PWA needs to put BetDoc in a pocket. Developed for Amit Ashok Kumar Patnaik.

    GET    /api/v1/mobile/status                        the Telegram desk (set up? linked chats?), the phone alerts and their channels
    POST   /api/v1/mobile/telegram/link-code            a one-time code that links a Telegram chat to this account (/link CODE)
    DELETE /api/v1/mobile/telegram/links/{chat_id}      unlink one of this account's chats

Mounted for signed-in users (``router.py``). The bot itself is the Sentinel's Telegram channel: its webhook is
``POST /api/v1/sentinel/webhook/telegram`` (secret header, configured chats only), and its token never leaves the
vault. Phone alerts are the Sentinel's: the stop-loss pullout and the Chef's Table DIAMOND plate route to the phone
row of its matrix (Telegram and SMS by default) and are sent only through channels that are set up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.models.sentinel import ChannelName, SentinelChannel
from app.services import pocket_twin as twin
from app.services import slip_ornament
from app.services.sentinel_routing import HYPE, PHONE_KINDS, load_matrix
from app.services.twin.vetting import developer_credit

router = APIRouter(tags=["Pocket Twin"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _deep_link(row: SentinelChannel | None, code: str | None = None) -> str | None:
    handle = ((row.config or {}).get("bot_username") or "") if row is not None else ""
    if not handle:
        return None
    return f"https://t.me/{handle}" + (f"?start={code}" if code else "")


def twin_commands() -> tuple[str, ...]:
    from app.services.sentinel_commands import known_commands  # noqa: PLC0415

    return tuple(known_commands())


@router.get("/status")
async def pocket_status(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    vault = getattr(request.app.state, "vault", None)
    async with sessions() as session:
        channels = await slip_ornament.channels(session, vault)
        telegram = await session.get(SentinelChannel, ChannelName.TELEGRAM.value)
        matrix = await load_matrix(session)
        credit = await developer_credit(session)
    phone_row = matrix.get(HYPE, [])
    return {
        "telegram": {**channels["telegram"], "bot": _deep_link(telegram), "commands": list(twin_commands()),
                     "linked_chats": await twin.links_of(sessions, user.id)},
        "phone_alerts": {"kinds": sorted(k.value for k in PHONE_KINDS), "routes_to": phone_row,
                         "channels": {k: v for k, v in channels.items() if k in ("telegram", "sms")}},
        "app_link": twin.app_link(settings, "/"),
        "developer_credit": credit,
    }


@router.post("/telegram/link-code", status_code=status.HTTP_201_CREATED)
async def link_code(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "Link codes need Redis, which is unreachable"})
    try:
        code, seconds = await twin.issue_link_code(redis, settings, user.id)
    except (RedisError, OSError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reason": "NO_REDIS", "message": "Link codes need Redis, which is unreachable"}) from exc
    async with sessions() as session:
        telegram = await session.get(SentinelChannel, ChannelName.TELEGRAM.value)
    return {"code": code, "command": f"/link {code}", "expires_in_seconds": seconds, "deep_link": _deep_link(telegram, code),
            "issued_at": datetime.now(UTC).isoformat()}


@router.delete("/telegram/links/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink(chat_id: int, user: CurrentUser, sessions: Sessions) -> None:
    if not any(link["chat_id"] == str(chat_id) for link in await twin.links_of(sessions, user.id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NOT_LINKED", "message": "That chat is not linked to your account"})
    await twin.set_link(sessions, chat_id, None, datetime.now(UTC))
