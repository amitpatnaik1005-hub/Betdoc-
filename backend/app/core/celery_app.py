"""Celery 5 application: Redis broker, dedicated Omni queues, JSON-only, at-least-once semantics."""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging
from kombu import Queue

from app.core.config import get_settings
from app.core.logging import setup_json_logging

_settings = get_settings()

celery_app = Celery(
    "betdoc_omni",
    broker=_settings.celery_broker_url.get_secret_value(),
    include=["app.workers.omni_poller"],
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
)


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    """Route Celery's loggers through the Group 58 JSON formatter."""
    setup_json_logging()
