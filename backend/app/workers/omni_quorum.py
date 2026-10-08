"""Celery task: scheduled quorum resolution over the latest event from every provider, per topic.

Beat runs ``omni.run_scheduled_quorum`` every ``omni_quorum_interval_seconds``. Each sweep:

1. reads the topics with an event inside ``omni_quorum_max_age_seconds`` from the quorum buffer
   (``app.services.omni_quorum_buffer``; written by the ingestion fleet and the generic poller),
   pruning older ones;
2. skips topics with fewer than ``omni_quorum_min_providers`` providers (one source is not a quorum)
   and topics whose event set has not changed since the last sweep;
3. runs ``QuorumService``: agreement yields a consensus event (stored per topic in Redis);
   variance beyond the threshold writes an ``OmniQuarantineLog`` row. One ``omni.quorum`` summary
   per sweep goes out on the Omni live channel, and a ``fleet`` event on the section bus.

The live board applies the same engine inline when it merges sources; this sweep is the
auditable record of agreement and disagreement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.core.events import publish_event
from app.core.omni_keys import OmniRedisKeys
from app.services.omni_normalizer import QuorumConsensusEngine, QuorumPolicy, QuorumService
from app.services.omni_quorum_buffer import active_topics, fingerprint, read_topic_events

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def resolve_quorums(
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    clock: Callable[[], datetime] = _utcnow,
) -> dict[str, int]:
    keys = OmniRedisKeys(settings.omni_redis_prefix)
    policy = QuorumPolicy.from_settings(settings)
    service = QuorumService(QuorumConsensusEngine(policy, clock))
    summary = {"topics": 0, "resolved": 0, "quarantined": 0, "insufficient": 0, "unchanged": 0}
    ttl = max(1, int(policy.max_age_seconds))

    topics = await active_topics(redis, keys, policy.max_age_seconds)
    summary["topics"] = len(topics)
    if not topics:
        return summary

    now = clock()
    quarantined: list[str] = []
    async with session_factory() as session:
        for topic in topics:
            events = [
                e for e in await read_topic_events(redis, keys, topic)
                if e.source_timestamp is not None and (now - e.source_timestamp).total_seconds() <= policy.max_age_seconds
            ]
            if len({e.provider_id for e in events}) < settings.omni_quorum_min_providers:
                summary["insufficient"] += 1
                continue
            fp = fingerprint(events)
            if await redis.get(keys.quorum_fingerprint(topic)) == fp:
                summary["unchanged"] += 1
                continue

            consensus = await service.resolve(session, topic, events)  # quarantines (DB) on failure
            await redis.set(keys.quorum_fingerprint(topic), fp, ex=ttl)
            if consensus is None:
                summary["quarantined"] += 1
                quarantined.append(topic)
            else:
                summary["resolved"] += 1
                await redis.set(keys.quorum_consensus(topic), consensus.model_dump_json(), ex=ttl)

    if summary["resolved"] or summary["quarantined"]:
        # One message per sweep, not per topic: a sweep can settle dozens of cells at once
        message: dict[str, Any] = {
            "kind": "omni.quorum",
            "topic": "quorum.sweep",
            "provider_name": "Quorum",
            "payload": {**summary, "quarantined_topics": quarantined[:20]},
            "at": now.isoformat(),
        }
        try:
            await redis.publish(settings.omni_live_channel, json.dumps(message, separators=(",", ":"), default=str))
        except (RedisError, OSError):
            logger.debug("Quorum sweep publish skipped")
        await publish_event(redis, {"type": "fleet", "kind": "quorum", **summary})
    logger.info("Quorum sweep: %s", summary)
    return summary


@celery_app.task(name="omni.run_scheduled_quorum", acks_late=True, ignore_result=True)
def run_scheduled_quorum() -> dict[str, Any]:
    """Resolve every active topic once. A failed sweep is logged; the next beat tick retries."""
    try:
        return asyncio.run(_sweep())
    except Exception as exc:
        logger.exception("Scheduled quorum sweep failed")
        return {"error": type(exc).__name__}


async def _sweep() -> dict[str, int]:
    settings = get_settings()
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    try:
        return await resolve_quorums(redis, async_sessionmaker(engine, expire_on_commit=False), settings)
    finally:
        await redis.aclose()
        await engine.dispose()
