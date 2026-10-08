"""Single definition of every Omni Redis key (shared by API, dispatcher, workers)."""

from __future__ import annotations

from uuid import UUID


class OmniRedisKeys:
    __slots__ = ("_prefix",)

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def breaker_open(self, provider_id: UUID | str) -> str:
        return f"{self._prefix}:breaker:{provider_id}:open"

    def breaker_failures(self, provider_id: UUID | str) -> str:
        return f"{self._prefix}:breaker:{provider_id}:failures"

    def breaker_half_open(self, provider_id: UUID | str) -> str:
        return f"{self._prefix}:breaker:{provider_id}:half_open"

    def rate_window(self, provider_id: UUID | str, window: int) -> str:
        return f"{self._prefix}:rpm:{provider_id}:{window}"

    def dispatch_claim(self, endpoint_id: UUID | str) -> str:
        return f"{self._prefix}:dispatch:{endpoint_id}"

    # ---- ingestion fleet (keyed by data source id: "polymarket", "odds_api") ----
    def fleet_lock(self, source_id: str) -> str:
        """Distributed lock: at most one run per source at a time, cluster-wide."""
        return f"{self._prefix}:fleet:lock:{source_id}"

    def fleet_claim(self, source_id: str) -> str:
        """Set by the beat tick when it enqueues a run, so a queued run is not enqueued twice."""
        return f"{self._prefix}:fleet:claim:{source_id}"

    def fleet_probe_claim(self, source_id: str) -> str:
        """Set while a quota recheck (probe) of a source in reserve is pending."""
        return f"{self._prefix}:fleet:probe:{source_id}"

    def fleet_metrics(self, source_id: str) -> str:
        """Hash: last run outcome, latency, tick counts, quota, schedule state."""
        return f"{self._prefix}:fleet:{source_id}:metrics"

    def fleet_runs(self, source_id: str) -> str:
        """List of "1"/"0", newest first: the success-rate window."""
        return f"{self._prefix}:fleet:{source_id}:runs"

    def fleet_heartbeat(self) -> str:
        """Present while a Celery worker is executing fleet tasks; the in-process fallback stands down."""
        return f"{self._prefix}:fleet:celery_heartbeat"

    def fleet_deadletter(self) -> str:
        """List of JSON records for sources paused after repeated failures, newest first."""
        return f"{self._prefix}:fleet:deadletter"

    # ---- quorum buffer: latest normalised event per provider, per topic ----
    def quorum_topics(self) -> str:
        """Sorted set: topic -> unix time of its last event."""
        return f"{self._prefix}:quorum:topics"

    def quorum_topic(self, topic: str) -> str:
        """Hash: provider key -> StandardizedEvent JSON."""
        return f"{self._prefix}:quorum:topic:{topic}"

    def quorum_fingerprint(self, topic: str) -> str:
        """The event set last resolved for a topic, so an unchanged quorum is not re-run or re-quarantined."""
        return f"{self._prefix}:quorum:fp:{topic}"

    def quorum_consensus(self, topic: str) -> str:
        return f"{self._prefix}:quorum:consensus:{topic}"
