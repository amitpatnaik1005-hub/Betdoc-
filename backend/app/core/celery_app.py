"""Celery 5 application: Redis broker, dedicated Omni queues, JSON-only, at-least-once semantics.

Processes (docker-compose runs each as its own service):

* worker: ``celery -A app.core.celery_app:celery_app worker -l info``
* beat:   ``celery -A app.core.celery_app:celery_app beat -l info -s /tmp/celerybeat-schedule``

Beat drives the ingestion fleet tick (which enqueues each due source; per-source intervals live in
Fleet Command, so the schedule itself stays fixed), the quorum sweep, the CFO, Nalanda and the
Sentinel's checks (see each entry).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from celery import Celery
from celery.schedules import crontab
from celery.utils.time import ffwd
from celery.signals import setup_logging
from kombu import Queue

from app.core.config import get_settings
from app.core.logging import setup_json_logging

_settings = get_settings()

# Fleet tick cadence: the finest interval granularity a source can have
FLEET_TICK_SECONDS = 5.0


def _now_in(zone: str) -> datetime:
    return datetime.now(ZoneInfo(zone))


class ZonedCrontab(crontab):
    """A crontab read in one time zone. Celery's own reads the hour in ``last_run_at``'s zone, which
    beat keeps in the app's (UTC): "08:00" would fire at 13:30 in Kolkata. This one converts first,
    so 08:00 means 08:00 in ``zone``, daylight saving included, and it pickles by its arguments."""

    def __init__(self, minute: str | int = "*", hour: str | int = "*", *, zone: str) -> None:
        self.zone = zone
        super().__init__(minute=minute, hour=hour, nowfun=partial(_now_in, zone))

    def remaining_delta(self, last_run_at: datetime, tz: object = None, ffwd: type = ffwd) -> tuple[datetime, timedelta, datetime]:  # type: ignore[override]
        aware = last_run_at if last_run_at.tzinfo else last_run_at.replace(tzinfo=UTC)
        return super().remaining_delta(aware.astimezone(ZoneInfo(self.zone)), tz, ffwd)

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        return (_zoned_crontab, (self._orig_minute, self._orig_hour, self.zone))

    def __repr__(self) -> str:
        return f"<zoned crontab: {self._orig_minute} {self._orig_hour} * * * {self.zone}>"


def _zoned_crontab(minute: str | int, hour: str | int, zone: str) -> ZonedCrontab:
    return ZonedCrontab(minute, hour, zone=zone)


celery_app = Celery(
    "betdoc_omni",
    broker=_settings.celery_broker_url.get_secret_value(),
    include=["app.workers.omni_poller", "app.workers.omni_quorum", "app.workers.cfo_settlement", "app.workers.sniper", "app.workers.hive_worker", "app.workers.lab_worker", "app.workers.nalanda_maintenance", "app.workers.sentinel_tasks", "app.workers.oracle_tasks", "app.workers.vault_prober", "app.workers.execution_dispatcher", "app.workers.twin_tasks", "app.workers.feedback_tasks"],
)

celery_app.conf.update(
    task_queues=tuple(Queue(name, routing_key=name) for name in _settings.omni_queues),
    task_default_queue=_settings.omni_default_queue,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": _settings.omni_broker_visibility_timeout_seconds},
    task_time_limit=_settings.omni_task_time_limit_seconds,
    task_soft_time_limit=_settings.omni_task_soft_time_limit_seconds,
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "omni-fleet-tick": {
            "task": "omni.fleet.tick",
            "schedule": FLEET_TICK_SECONDS,
            # A tick nobody ran in time is worthless: the next one is seconds away
            "options": {"expires": FLEET_TICK_SECONDS},
        },
        "omni-quorum-sweep": {
            "task": "omni.run_scheduled_quorum",
            "schedule": _settings.omni_quorum_interval_seconds,
            "options": {"expires": _settings.omni_quorum_interval_seconds},
        },
        "cfo-settle-markets": {
            "task": "cfo.settle_markets",
            "schedule": _settings.CFO_SETTLE_INTERVAL_SECONDS,
            "options": {"expires": _settings.CFO_SETTLE_INTERVAL_SECONDS},
        },
        "sniper-resolve-pending-orders": {
            "task": "sniper.resolve_pending_orders",
            "schedule": _settings.SNIPER_RESOLVE_INTERVAL_SECONDS,
            "options": {"expires": _settings.SNIPER_RESOLVE_INTERVAL_SECONDS},
        },
        "sniper-refresh-sessions": {
            "task": "sniper.refresh_sessions",
            "schedule": 60.0,  # well inside the 5-minute refresh margin
            "options": {"expires": 60.0},
        },
        # Nalanda (Group 67): partitions ahead of need, the archive's upkeep, the cold tier
        "nalanda-preallocate-partitions": {"task": "nalanda.preallocate_partitions", "schedule": crontab(minute=5, hour=0, day_of_week="sun")},
        "nalanda-vacuum-partitions": {"task": "nalanda.vacuum_partitions", "schedule": crontab(minute=30, hour=3)},
        "nalanda-compress-historical-ticks": {"task": "nalanda.compress_historical_ticks", "schedule": crontab(minute=15, hour=2)},
        "nalanda-mirror-ledger": {
            "task": "nalanda.mirror_ledger",
            "schedule": _settings.NALANDA_MIRROR_INTERVAL_SECONDS,
            "options": {"expires": _settings.NALANDA_MIRROR_INTERVAL_SECONDS},
        },
        "nalanda-anchor-chain": {"task": "nalanda.anchor_chain", "schedule": crontab(minute=0)},
        "nalanda-verify-ledger": {"task": "nalanda.verify_ledger", "schedule": crontab(minute=30)},  # a broken chain pages (Sentinel)
        # The Sentinel (Group 68): dependency health, the dead man's switch on Garuda, the 08:00 forecast
        "sentinel-dependency-health": {
            "task": "sentinel.dependency_health",
            "schedule": _settings.SENTINEL_HEALTH_INTERVAL_SECONDS,
            "options": {"expires": _settings.SENTINEL_HEALTH_INTERVAL_SECONDS},
        },
        "sentinel-liveness-check": {
            "task": "sentinel.liveness_check",
            "schedule": _settings.SENTINEL_HEARTBEAT_SECONDS,
            "options": {"expires": _settings.SENTINEL_HEARTBEAT_SECONDS},
        },
        # Ashoka (Group 69): the users' placed bets settle, their scores arrive, the trend feed refreshes
        "oracle-settle-user-bets": {
            "task": "oracle.settle_user_bets",
            "schedule": _settings.ORACLE_SETTLE_INTERVAL_SECONDS,
            "options": {"expires": _settings.ORACLE_SETTLE_INTERVAL_SECONDS},
        },
        "oracle-poll-scores": {"task": "oracle.poll_scores", "schedule": 900.0, "options": {"expires": 900.0}},
        "oracle-scan-trending": {"task": "oracle.scan_trending", "schedule": 600.0, "options": {"expires": 600.0}},
        # The Vault (Group 70): credential health (sanctioned APIs, paced), and stake held by finished orders
        "vault-probe-credentials": {
            "task": "vault.probe_credentials",
            "schedule": _settings.VAULT_PROBE_INTERVAL_MINUTES * 60.0,
            "options": {"expires": _settings.VAULT_PROBE_INTERVAL_MINUTES * 60.0},
        },
        "vault-release-reservations": {"task": "vault.release_reservations", "schedule": 300.0, "options": {"expires": 300.0}},
        # Group 71: the router's sweep (hold sync, ledger reconciliation, expired venue pauses, Nalanda receipts)
        "router-sweep": {"task": "router.sweep", "schedule": _settings.ROUTER_SWEEP_INTERVAL_SECONDS, "options": {"expires": _settings.ROUTER_SWEEP_INTERVAL_SECONDS}},
        # Group 72: the twin's in-play watch (a Redis lock keeps one tick at a time; a late tick expires unrun)
        "twin-inplay-tick": {"task": "twin.inplay_tick", "schedule": _settings.TWIN_INPLAY_POLL_SECONDS, "options": {"expires": _settings.TWIN_INPLAY_POLL_SECONDS}},
        # Group 73: the feedback loop (settle + attribute; nightly inverse-Brier weights for pillar 1)
        "feedback-sweep": {"task": "feedback.sweep", "schedule": _settings.FEEDBACK_SWEEP_INTERVAL_SECONDS, "options": {"expires": _settings.FEEDBACK_SWEEP_INTERVAL_SECONDS}},
        "feedback-recalibrate": {
            "task": "feedback.recalibrate",
            "schedule": ZonedCrontab(minute=_settings.FEEDBACK_RECALIBRATE_MINUTE, hour=_settings.FEEDBACK_RECALIBRATE_HOUR, zone=_settings.ORACLE_TIMEZONE),
        },
        "sentinel-market-forecast-hype": {
            "task": "sentinel.market_forecast_hype",
            "schedule": ZonedCrontab(minute=_settings.SENTINEL_HYPE_MINUTE, hour=_settings.SENTINEL_HYPE_HOUR, zone=_settings.SENTINEL_TIMEZONE),
        },
    },
)


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    """Route Celery's loggers through the Group 58 JSON formatter."""
    setup_json_logging()
