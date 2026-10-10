"""The fortress's evidence store: one Redis hash per fixture, one field per section (Group 72).

    <TWIN_PREFIX>:intel:<fixture_id>   weather | travel | injuries | lineups | referee | motivation | public_splits | liquidity

A write replaces only the sections it carries and keeps the key for the longest section age limit, so
a fixture's evidence expires on its own. Feeds (a weather or injury-wire adapter) and administrators
write through ``write_intel``; the fortress reads with ``read_intel``. A section that fails validation on
read (a schema change) is dropped, never half-trusted.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.schemas.twin import INTEL_SECTIONS, FixtureIntel


def intel_key(settings: Settings, fixture_id: str) -> str:
    return f"{settings.TWIN_PREFIX}:intel:{fixture_id}"


def weights_key(settings: Settings) -> str:
    return f"{settings.TWIN_PREFIX}:model_weights"


def _ttl_seconds(settings: Settings) -> int:
    return int(max(settings.TWIN_INTEL_MAX_AGE_MINUTES.values(), default=60.0) * 60) + 3600


async def write_intel(redis: Redis, settings: Settings, fixture_id: str, intel: FixtureIntel) -> list[str]:
    """Store the sections ``intel`` carries; returns their names."""
    sections = {name: getattr(intel, name).model_dump_json() for name in INTEL_SECTIONS if getattr(intel, name) is not None}
    if not sections:
        return []
    key = intel_key(settings, fixture_id)
    pipe = redis.pipeline(transaction=True)
    pipe.hset(key, mapping=sections)
    pipe.expire(key, _ttl_seconds(settings))
    await pipe.execute()
    return sorted(sections)


async def clear_intel(redis: Redis, settings: Settings, fixture_id: str, sections: Iterable[str]) -> int:
    names = [s for s in sections if s in INTEL_SECTIONS]
    return int(await redis.hdel(intel_key(settings, fixture_id), *names)) if names else 0


async def read_intel(redis: Redis, settings: Settings, fixture_ids: Iterable[str]) -> dict[str, FixtureIntel]:
    """fixture id -> its evidence (fixtures with none are absent). Redis errors propagate: the caller decides."""
    ids = list(dict.fromkeys(fixture_ids))
    if not ids:
        return {}
    pipe = redis.pipeline(transaction=False)
    for fixture_id in ids:
        pipe.hgetall(intel_key(settings, fixture_id))
    rows = await pipe.execute()
    out: dict[str, FixtureIntel] = {}
    for fixture_id, raw in zip(ids, rows, strict=True):
        if not raw:
            continue
        parsed: dict[str, Any] = {}
        for name, value in raw.items():
            if name not in INTEL_SECTIONS:
                continue
            try:
                parsed[name] = json.loads(value)
                FixtureIntel.model_validate({name: parsed[name]})
            except (ValueError, ValidationError):
                parsed.pop(name, None)
        if parsed:
            out[fixture_id] = FixtureIntel.model_validate(parsed)
    return out


async def model_weights(redis: Redis, settings: Settings) -> dict[str, float]:
    """The calibration store's weight per model (a Brier-score job writes it); empty: equal weights."""
    try:
        raw = await redis.hgetall(weights_key(settings))
    except (RedisError, OSError):
        return {}
    out: dict[str, float] = {}
    for name, value in raw.items():
        try:
            weight = float(value)
        except ValueError:
            continue
        if weight >= 0:
            out[name] = weight
    return out


def weights_meta_key(settings: Settings) -> str:
    """The published weights' provenance (Group 74): the run, its trigger, each model's lifecycle state."""
    return f"{weights_key(settings)}:meta"


def weight_pins_key(settings: Settings) -> str:
    """Administrators' pinned weights (Group 74): every recalibration keeps them until they are lifted."""
    return f"{weights_key(settings)}:pins"
