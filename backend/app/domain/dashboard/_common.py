"""Shared helpers for the dashboard domain (time alignment, numeric & JSON safety)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(dt: datetime) -> datetime:
    """Normalize any datetime to tz-aware UTC (naive values are assumed to be UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def align_to_column(dt: datetime, column: Any) -> datetime:
    """Match a bound datetime's tz-awareness to the column type (avoids asyncpg naive/aware errors)."""
    col_type = getattr(column, "type", None)
    column_is_tz_aware = bool(getattr(col_type, "timezone", False))
    aware = ensure_aware_utc(dt)
    return aware if column_is_tz_aware else aware.replace(tzinfo=None)


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def json_safe(value: Any) -> Any:
    """Recursively convert values into JSON-serializable primitives."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return ensure_aware_utc(value).isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump(mode="json"))
    return str(value)
