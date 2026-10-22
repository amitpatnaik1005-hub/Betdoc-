"""The neural event bus's transport and memory (Group 108, point b.9). Developed for Amit Ashok Kumar Patnaik.

* **Frames** go out on Redis pub/sub ``<NEURAL_BUS_PREFIX>:pipeline`` (``betdoc:neural:pipeline``), so a chain the
  Wire's scan starts in a Celery worker reaches every API process. Without Redis (or when the publish fails) the frame
  goes to this process's own listeners instead (``Hub``): a chain run in the API still reaches its sockets. A frame
  goes to exactly one of the two, never both. Each frame names its audience: ``system`` (every viewer) or one user.
* **History**: each audience's last ``NEURAL_BUS_HISTORY`` chains, for ``NEURAL_BUS_HISTORY_HOURS`` (a Redis list,
  else this process's memory).
* **Memory between runs**: the Oracle step's best leg per fixture and the Consultant step's Slip Score per slip, so
  the next chain can say what changed since. Publishing and remembering never raise: the chain's result is the truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings

logger = logging.getLogger("betdoc.neural.bus")

SYSTEM = "system"
_QUEUE_SIZE = 256
_LOCAL_VALUES_MAX = 2000


class Hub:
    """This process's listeners, for frames that could not go through Redis."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[str]] = set()

    def listen(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._queues.add(q)
        return q

    def drop(self, q: asyncio.Queue[str]) -> None:
        self._queues.discard(q)

    def deliver(self, raw: str) -> int:
        for q in list(self._queues):
            if q.full():  # a stalled socket loses its oldest frame, never blocks the chain
                q.get_nowait()
            q.put_nowait(raw)
        return len(self._queues)


HUB = Hub()
_LOCAL: dict[str, deque[str]] = {}
_LOCAL_VALUES: dict[str, str] = {}


def channel(settings: Settings) -> str:
    return f"{settings.NEURAL_BUS_PREFIX}:pipeline"


def history_key(settings: Settings, audience: str) -> str:
    return f"{settings.NEURAL_BUS_PREFIX}:chains:{audience}"


def edge_key(settings: Settings, fixture_id: str) -> str:
    return f"{settings.NEURAL_BUS_PREFIX}:edge:{fixture_id}"


def slip_key(settings: Settings, user_id: str, signature: str) -> str:
    return f"{settings.NEURAL_BUS_PREFIX}:slip:{user_id}:{signature}"


def lock_key(settings: Settings, name: str) -> str:
    return f"{settings.NEURAL_BUS_PREFIX}:lock:{name}"


def _ttl(settings: Settings) -> int:
    return max(1, int(settings.NEURAL_BUS_HISTORY_HOURS * 3600))


async def publish(redis: Redis | None, settings: Settings, frame: dict[str, Any]) -> str:
    """'redis' or 'local': where the frame went."""
    raw = json.dumps(frame, separators=(",", ":"), default=str)
    if redis is not None:
        try:
            await redis.publish(channel(settings), raw)
            return "redis"
        except (RedisError, OSError):
            logger.warning("neural bus: Redis publish failed; delivering in process")
    HUB.deliver(raw)
    return "local"


async def remember(redis: Redis | None, settings: Settings, audience: str, chain: dict[str, Any]) -> None:
    raw = json.dumps(chain, separators=(",", ":"), default=str)
    if redis is not None:
        try:
            key = history_key(settings, audience)
            pipe = redis.pipeline(transaction=False)
            pipe.lpush(key, raw)
            pipe.ltrim(key, 0, settings.NEURAL_BUS_HISTORY - 1)
            pipe.expire(key, _ttl(settings))
            await pipe.execute()
            return
        except (RedisError, OSError):
            logger.warning("neural bus: chain %s kept in process only", chain.get("chain_id"))
    _LOCAL.setdefault(audience, deque(maxlen=settings.NEURAL_BUS_HISTORY)).appendleft(raw)


async def recent(redis: Redis | None, settings: Settings, audiences: list[str], limit: int) -> list[dict[str, Any]]:
    """The audiences' chains, newest first."""
    raws: list[str] = []
    for audience in audiences:
        got: list[str] | None = None
        if redis is not None:
            try:
                got = list(await redis.lrange(history_key(settings, audience), 0, settings.NEURAL_BUS_HISTORY - 1))
            except (RedisError, OSError):
                got = None
        raws.extend(got if got is not None else list(_LOCAL.get(audience, ())))
    chains = []
    for raw in raws:
        try:
            chains.append(json.loads(raw))
        except ValueError:
            continue
    chains.sort(key=lambda c: c.get("started_at") or "", reverse=True)
    return chains[:limit]


async def read_value(redis: Redis | None, key: str) -> dict[str, Any] | None:
    raw: str | None = None
    if redis is not None:
        try:
            raw = await redis.get(key)
        except (RedisError, OSError):
            raw = None
    raw = raw if raw is not None else _LOCAL_VALUES.get(key)
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def read_values(redis: Redis | None, keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    raws: list[str | None] = [None] * len(keys)
    if redis is not None:
        try:
            raws = list(await redis.mget(keys))
        except (RedisError, OSError):
            raws = [None] * len(keys)
    out = {}
    for key, raw in zip(keys, raws, strict=True):
        raw = raw if raw is not None else _LOCAL_VALUES.get(key)
        try:
            value = json.loads(raw) if raw else None
        except ValueError:
            value = None
        if isinstance(value, dict):
            out[key] = value
    return out


async def write_value(redis: Redis | None, settings: Settings, key: str, value: dict[str, Any]) -> None:
    raw = json.dumps(value, separators=(",", ":"), default=str)
    if redis is not None:
        try:
            await redis.set(key, raw, ex=_ttl(settings))
            return
        except (RedisError, OSError):
            pass
    _LOCAL_VALUES.pop(key, None)
    _LOCAL_VALUES[key] = raw
    while len(_LOCAL_VALUES) > _LOCAL_VALUES_MAX:  # without a TTL, the oldest go first
        _LOCAL_VALUES.pop(next(iter(_LOCAL_VALUES)))


async def claim(redis: Redis | None, settings: Settings, name: str, seconds: int) -> bool:
    """A lock for ``seconds`` (set-if-absent). Without Redis every claim succeeds: there is nothing shared to guard."""
    if redis is None or seconds <= 0:
        return True
    try:
        return bool(await redis.set(lock_key(settings, name), "1", ex=seconds, nx=True))
    except (RedisError, OSError):
        return True


async def release(redis: Redis | None, settings: Settings, name: str) -> None:
    if redis is None:
        return
    try:
        await redis.delete(lock_key(settings, name))
    except (RedisError, OSError):
        pass
