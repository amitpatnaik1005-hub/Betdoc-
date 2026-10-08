"""Quota-aware failover routing across every fleet source (built-in and config-driven).

Market groups are canonical sport keys (``soccer_epl``). Each source covers some groups. Free
sources always run everything they cover. Metered sources are ranked per group by ``priority``
(lower wins); the best ``OMNI_FLEET_REDUNDANCY`` *available* ones run for that group and the rest
stand by without spending quota. A source is unavailable while it is disabled, dead-lettered
(FATAL), missing its key, its circuit breaker is open, or its remaining quota is under
``OMNI_FLEET_QUOTA_RESERVE``: its groups fail over to the next source in line, and come back
automatically once it recovers (breaker cooldown, quota reset).

``compute_plan`` is pure: the beat tick, the in-process fallback and the Fleet Command API all call
it with the same inputs and get the same answer.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.ingestion import INGESTORS, OddsApiIngestor
from app.adapters.ingestion.factory import ProviderSpec
from app.core.config import Settings
from app.models.omni_vault import OmniFleetSource
from app.services.omni_normalizer import AliasDictionary, DevigMethod
from app.services.omni_throttle import RateLimit

logger = logging.getLogger("betdoc.omni.router")

Cost = Literal["free", "metered"]
Availability = Literal["available", "disabled", "paused", "needs_key", "circuit_open", "quota_reserve"]
Role = Literal["always_on", "primary", "failover", "standby", "unavailable"]

BUILTIN_PRIORITY = {"odds_api": 10, "polymarket": 50}
BUILTIN_COST: dict[str, Cost] = {"odds_api": "metered", "polymarket": "free"}


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    source_id: str
    display_name: str
    description: str
    docs_url: str | None
    kind: Literal["builtin", "config"]
    cost: Cost
    priority: int
    coverage: Mapping[str, str]  # canonical sport key -> provider-native id
    interval_seconds: float
    default_interval_seconds: float
    requires_api_key: bool
    rate_limit: RateLimit
    devig: DevigMethod
    spec: ProviderSpec | None = None


@dataclass(frozen=True, slots=True)
class FailoverNote:
    group: str
    replacing: str  # the higher-priority source this one stands in for
    reason: Availability


@dataclass(slots=True)
class PlanEntry:
    role: Role
    availability: Availability
    scope: list[str] = field(default_factory=list)  # canonical groups this source should fetch now
    native_scope: list[str] = field(default_factory=list)
    covering: list[FailoverNote] = field(default_factory=list)


@dataclass(slots=True)
class GroupStatus:
    group: str
    active: list[str] = field(default_factory=list)  # metered sources fetching it now
    free: list[str] = field(default_factory=list)
    down: dict[str, Availability] = field(default_factory=dict)  # metered candidates out of action
    failover: bool = False  # a lower-priority source is covering for a higher one
    uncovered: bool = False  # nothing available fetches it


# ---------------------------------------------------------------- registry
def builtin_descriptors(settings: Settings, aliases: AliasDictionary, rows: Mapping[str, OmniFleetSource]) -> dict[str, SourceDescriptor]:
    coverage: dict[str, dict[str, str]] = {
        "odds_api": OddsApiIngestor.coverage(settings),
        "polymarket": {
            (spec.key if (spec := aliases.sport_for_polymarket(league)) else f"polymarket_{league}"): league
            for league in settings.polymarket_leagues
        },
    }
    descriptors: dict[str, SourceDescriptor] = {}
    for source_id, ingestor in INGESTORS.items():
        default = ingestor.interval_seconds(settings)
        row = rows.get(source_id)
        descriptors[source_id] = SourceDescriptor(
            source_id=source_id,
            display_name=ingestor.display_name,
            description=ingestor.description,
            docs_url=ingestor.docs_url,
            kind="builtin",
            cost=BUILTIN_COST.get(source_id, "metered"),
            priority=BUILTIN_PRIORITY.get(source_id, 100),
            coverage=coverage.get(source_id, {}),
            interval_seconds=(row.interval_seconds if row and row.interval_seconds else default),
            default_interval_seconds=default,
            requires_api_key=ingestor.requires_api_key,
            rate_limit=RateLimit(ingestor.requests_per_minute, ingestor.burst),
            # Polymarket mids carry no bookmaker margin to model; the normaliser handles it
            devig="shin",
        )
    return descriptors


def config_descriptor(row: OmniFleetSource) -> SourceDescriptor | None:
    """A stored spec that no longer validates is skipped (and logged), never a crash."""
    if row.spec is None:
        return None
    try:
        spec = ProviderSpec.model_validate(row.spec)
    except ValidationError as exc:
        logger.error("Fleet provider %s has an invalid spec (%d error(s)); skipping it", row.source_id, exc.error_count())
        return None
    return SourceDescriptor(
        source_id=row.source_id,
        display_name=spec.display_name,
        description=spec.description,
        docs_url=str(spec.docs_url) if spec.docs_url else None,
        kind="config",
        cost=spec.cost,
        priority=spec.priority,
        coverage=dict(spec.coverage),
        interval_seconds=row.interval_seconds or spec.interval_seconds,
        default_interval_seconds=spec.interval_seconds,
        requires_api_key=spec.auth.type != "none",
        rate_limit=RateLimit(spec.rate_limit.requests_per_minute, spec.rate_limit.burst),
        devig=spec.devig,
        spec=spec,
    )


async def load_registry(
    session: AsyncSession, settings: Settings, aliases: AliasDictionary
) -> tuple[dict[str, SourceDescriptor], dict[str, OmniFleetSource]]:
    """Every source the fleet knows: code-defined adapters plus provider specs stored in the DB."""
    rows = {row.source_id: row for row in (await session.execute(select(OmniFleetSource))).scalars()}
    registry = builtin_descriptors(settings, aliases, rows)
    for source_id, row in rows.items():
        if source_id not in registry and (descriptor := config_descriptor(row)) is not None:
            registry[source_id] = descriptor
    return registry, rows


# ---------------------------------------------------------------- plan
def compute_plan(
    registry: Mapping[str, SourceDescriptor],
    availability: Mapping[str, Availability],
    redundancy: int = 1,
) -> tuple[dict[str, PlanEntry], dict[str, GroupStatus]]:
    entries = {
        sid: PlanEntry(
            role="unavailable" if availability.get(sid, "available") != "available" else ("always_on" if d.cost == "free" else "standby"),
            availability=availability.get(sid, "available"),
        )
        for sid, d in registry.items()
    }
    groups: dict[str, GroupStatus] = {}
    candidates: dict[str, list[SourceDescriptor]] = defaultdict(list)
    for descriptor in registry.values():
        for group in descriptor.coverage:
            groups.setdefault(group, GroupStatus(group))
            candidates[group].append(descriptor)

    for group, status in groups.items():
        ranked = sorted(candidates[group], key=lambda d: (d.priority, d.source_id))
        for descriptor in ranked:
            if descriptor.cost == "free" and entries[descriptor.source_id].availability == "available":
                status.free.append(descriptor.source_id)
        metered = [d for d in ranked if d.cost == "metered"]
        chosen: list[SourceDescriptor] = []
        for descriptor in metered:
            state = entries[descriptor.source_id].availability
            if state != "available":
                status.down[descriptor.source_id] = state
            elif len(chosen) < redundancy:
                chosen.append(descriptor)
        status.active = [d.source_id for d in chosen]
        status.uncovered = not chosen and not status.free
        best_overall = metered[: max(redundancy, 0)]
        for descriptor in chosen:
            entry = entries[descriptor.source_id]
            entry.scope.append(group)
            if descriptor not in best_overall:
                # Standing in for every higher-ranked source that is out of action
                status.failover = True
                for better in metered:
                    if better is descriptor:
                        break
                    if better.source_id in status.down:
                        entry.covering.append(FailoverNote(group, better.source_id, status.down[better.source_id]))

    for sid, entry in entries.items():
        descriptor = registry[sid]
        if entry.availability != "available":
            entry.scope = []
            continue
        if descriptor.cost == "free":
            entry.scope = list(descriptor.coverage)
            entry.role = "always_on"
        elif entry.scope:
            entry.role = "failover" if entry.covering else "primary"
        else:
            entry.role = "standby"
        entry.native_scope = [descriptor.coverage[group] for group in entry.scope if group in descriptor.coverage]
    return entries, groups
