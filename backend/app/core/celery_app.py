"""Celery 5 application: Redis broker, dedicated Omni queues, JSON-only, at-least-once semantics.

Processes (docker-compose runs each as its own service):

* worker: ``celery -A app.core.celery_app:celery_app worker -l info``
* beat:   ``celery -A app.core.celery_app:celery_app beat -l info -s /tmp/celerybeat-schedule``

Beat drives two things: the ingestion fleet tick (which enqueues each due source; per-source
intervals live in Fleet Command, so the schedule itself stays fixed) and the quorum sweep.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging
from kombu import Queue

from app.core.config import get_settings
from app.core.logging import setup_json_logging

_settings = get_settings()

# Fleet tick cadence: the finest interval granularity a source can have
FLEET_TICK_SECONDS = 5.0

celery_app = Celery(
    "betdoc_omni",
    broker=_settings.celery_broker_url.get_secret_value(),
    include=["app.workers.omni_poller", "app.workers.omni_quorum", "app.workers.cfo_settlement", "app.workers.sniper", "app.workers.hive_worker", "app.workers.lab_worker", "app.workers.nalanda_maintenance"],
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
    },
)


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    """Route Celery's loggers through the Group 58 JSON formatter."""
    setup_json_logging()
