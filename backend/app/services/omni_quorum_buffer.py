"""Quorum buffer: the latest normalised event from each provider, per topic, held in Redis.

Every producer of ``StandardizedEvent``s writes here (the ingestion fleet for its MarketTicks,
the generic Omni poller for DB-configured providers), and ``omni.run_scheduled_quorum`` reads it
back to resolve consensus. One hash field per provider means one vote per provider per topic.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence

from pydantic import ValidationError
from redis import Redis as SyncRedis
from redis.asyncio import Redis

from app.adapters.base_adapter import StandardizedEvent
from app.core.omni_keys import OmniRedisKeys


def provider_key(event: StandardizedEvent) -> str:
    return str(event.provider_id) if event.provider_id is not None else "unattributed"


async def buffer_events(
    redis: Redis, keys: OmniRedisKeys, items: Sequence[tuple[str, StandardizedEvent]], ttl_seconds: float
) -> None:
    if not items:
        return
    now = time.time()
    ttl = max(1, int(ttl_seconds))
    pipe = redis.pipeline(transaction=False)
    for topic, event in items:
        pipe.hset(keys.quorum_topic(topic), provider_key(event), event.model_dump_json())
        pipe.expire(keys.quorum_topic(topic), ttl)
    pipe.zadd(keys.quorum_topics(), {topic: now for topic, _ in items})
    await pipe.execute()


def buffer_event_sync(redis: SyncRedis, keys: OmniRedisKeys, topic: str, event: StandardizedEvent, ttl_seconds: float) -> None:
    """Same write for synchronous callers (the generic Celery poller)."""
    pipe = redis.pipeline(transaction=False)
    pipe.hset(keys.quorum_topic(topic), provider_key(event), event.model_dump_json())
    pipe.expire(keys.quorum_topic(topic), max(1, int(ttl_seconds)))
    pipe.zadd(keys.quorum_topics(), {topic: time.time()})
    pipe.execute()


async def active_topics(redis: Redis, keys: OmniRedisKeys, max_age_seconds: float) -> list[str]:
    """Topics with an event inside the window; older ones are pruned from the index."""
    cutoff = time.time() - max_age_seconds
    await redis.zremrangebyscore(keys.quorum_topics(), "-inf", f"({cutoff}")
    topics: list[str] = await redis.zrangebyscore(keys.quorum_topics(), cutoff, "+inf")
    return topics


async def read_topic_events(redis: Redis, keys: OmniRedisKeys, topic: str) -> list[StandardizedEvent]:
    raw: dict[str, str] = await redis.hgetall(keys.quorum_topic(topic))
    events: list[StandardizedEvent] = []
    for value in raw.values():
        try:
            events.append(StandardizedEvent.model_validate_json(value))
        except ValidationError:
            continue
    return events


def fingerprint(events: Sequence[StandardizedEvent]) -> str:
    """Identity of an event set: unchanged inputs are not re-resolved or re-quarantined every sweep."""
    parts = sorted(
        f"{provider_key(e)}|{e.source_timestamp.isoformat() if e.source_timestamp else '-'}|{e.normalized_value!r}"
        for e in events
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
