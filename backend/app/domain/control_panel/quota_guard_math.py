"""Group 92: BHEESHMA's quota and token-bucket arithmetic. Pure: no I/O, no clock of its own.

Every figure comes from what the fleet measured (quota headers mirrored into ``fleet_metrics``, the token
bucket's stored level and stamp). A source whose provider never sent quota headers is UNMETERED: no
fraction is ever assumed for it.

The cache shield is a real lever: while it is engaged for a metered fleet source, the beat polls that
source every ``interval x multiplier`` instead of every ``interval``, and the desk serves the odds already
stored between polls. Under the fleet's quota reserve the reserve wins whatever the operator's mode:
the source stops polling and its market groups fail over (``omni_fleet``), so the quota is never spent
to zero.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

ShieldMode = Literal["AUTO", "FORCE_CACHE_SHIELD", "LIVE_UNRESTRICTED"]
GuardState = Literal["HEALTHY", "WARNING_CACHE_SHIELD", "CRITICAL_LOCKED", "UNMETERED"]
PollingMode = Literal["LIVE_FULL", "CACHE_SHIELD", "RESERVE_FAILOVER", "BREAKER_HOLD", "NOT_POLLING"]
SourceKind = Literal["fleet", "router"]

SHIELD_MODES: Final[tuple[ShieldMode, ...]] = ("AUTO", "FORCE_CACHE_SHIELD", "LIVE_UNRESTRICTED")
DEFAULT_SHIELD_MODE: Final[ShieldMode] = "AUTO"


def parse_shield_mode(raw: object) -> ShieldMode:
    """A stored mode, or AUTO for anything missing or unrecognised."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return raw if raw in SHIELD_MODES else DEFAULT_SHIELD_MODE  # type: ignore[return-value]


def shield_engaged(*, cost: str | None, quota_fraction: float | None, mode: ShieldMode, warn_fraction: float) -> bool:
    """Whether the beat stretches this source's cadence. Only metered sources have a quota to protect."""
    if cost != "metered" or mode == "LIVE_UNRESTRICTED":
        return False
    if mode == "FORCE_CACHE_SHIELD":
        return True
    return quota_fraction is not None and quota_fraction <= warn_fraction


def shielded_interval(base_interval: float, engaged: bool, multiplier: float) -> float:
    return base_interval * multiplier if engaged else base_interval


def bucket_tokens_now(
    *, stored_tokens: float | None, stamp: float | None, now: float, refill_per_second: float, capacity: float
) -> float | None:
    """The bucket's level now: the stored level refilled since its stamp, capped at capacity (as the Lua script does).

    None when the level was not read (Redis down: the fleet then throttles per process, unseen from here).
    """
    if stored_tokens is None or stamp is None:
        return None
    return max(0.0, min(capacity, stored_tokens + max(0.0, now - stamp) * refill_per_second))


@dataclass(frozen=True, slots=True)
class SourceQuotaEvaluation:
    guard_state: GuardState
    polling_mode: PollingMode
    shield_engaged: bool
    effective_interval_seconds: float | None
    remaining_pct: float | None
    used_pct: float | None
    tokens_available: float | None
    bucket_utilization_pct: float | None
    is_throttled: bool
    verdict_summary: str


def evaluate_source(
    *,
    source_id: str,
    kind: SourceKind,
    cost: str | None,
    schedule: str,
    breaker_remaining_seconds: float | None,
    quota_fraction: float | None,
    tokens_available: float | None,
    capacity: float,
    base_interval_seconds: float | None,
    reserve_fraction: float,
    warn_fraction: float,
    shield_multiplier: float,
    mode: ShieldMode,
) -> SourceQuotaEvaluation:
    """One source's guard state, the polling the fleet will actually do, and a one-line verdict."""
    fraction = quota_fraction if quota_fraction is not None and 0.0 <= quota_fraction <= 1.0 else None
    remaining_pct = round(fraction * 100.0, 2) if fraction is not None else None
    used_pct = round(100.0 - fraction * 100.0, 2) if fraction is not None else None
    breaker_open = breaker_remaining_seconds is not None and breaker_remaining_seconds > 0
    below_reserve = cost == "metered" and fraction is not None and fraction < reserve_fraction
    engaged = kind == "fleet" and shield_engaged(cost=cost, quota_fraction=fraction, mode=mode, warn_fraction=warn_fraction)

    tokens = round(tokens_available, 2) if tokens_available is not None else None
    utilization = round((capacity - tokens) / capacity * 100.0, 2) if tokens is not None and capacity > 0 else None
    throttled = tokens is not None and tokens < 1.0

    guard: GuardState
    if breaker_open or below_reserve:
        guard = "CRITICAL_LOCKED"
    elif fraction is None:
        guard = "UNMETERED"
    elif fraction <= warn_fraction:
        guard = "WARNING_CACHE_SHIELD"
    else:
        guard = "HEALTHY"

    polling: PollingMode
    if schedule != "enabled":
        polling, verdict = "NOT_POLLING", f"Not polling: source {schedule.replace('_', ' ')}"
    elif breaker_open:
        polling, verdict = "BREAKER_HOLD", f"Circuit breaker open: runs skipped for {breaker_remaining_seconds:.0f}s"
    elif below_reserve:
        polling = "RESERVE_FAILOVER"
        verdict = f"Quota {remaining_pct:.1f}% is under the {reserve_fraction * 100:.0f}% reserve: not polling, market groups fail over"
    elif engaged:
        polling = "CACHE_SHIELD"
        reason = "forced by operator" if mode == "FORCE_CACHE_SHIELD" else f"quota {remaining_pct:.1f}% at or under {warn_fraction * 100:.0f}%"
        verdict = f"Cache shield ({reason}): polling every {shielded_interval(base_interval_seconds or 0.0, True, shield_multiplier):.0f}s"
    else:
        polling = "LIVE_FULL"
        if fraction is None:
            verdict = "No quota headers from this provider" + (" · router window limiter" if kind == "router" else "")
        elif guard == "WARNING_CACHE_SHIELD":
            verdict = f"Quota {remaining_pct:.1f}% at or under {warn_fraction * 100:.0f}%: shield overridden (live unrestricted)"
        else:
            verdict = f"Quota healthy: {remaining_pct:.1f}% remaining"
    if throttled and polling in ("LIVE_FULL", "CACHE_SHIELD"):
        verdict += " · rate limiter saturated"

    return SourceQuotaEvaluation(
        guard_state=guard,
        polling_mode=polling,
        shield_engaged=engaged and polling == "CACHE_SHIELD",
        effective_interval_seconds=(
            shielded_interval(base_interval_seconds, engaged, shield_multiplier) if base_interval_seconds is not None else None
        ),
        remaining_pct=remaining_pct,
        used_pct=used_pct,
        tokens_available=tokens,
        bucket_utilization_pct=utilization,
        is_throttled=throttled,
        verdict_summary=verdict,
    )
