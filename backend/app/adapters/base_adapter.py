"""Polymorphic provider adapters + the canonical StandardizedEvent contract."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "ADAPTER_REGISTRY",
    "AdapterError",
    "BaseProviderAdapter",
    "JsonPathAdapter",
    "StandardizedEvent",
    "build_adapter",
    "parse_http_date",
    "register_adapter",
    "resolve_path",
]

ScalarValue = float | int | str | bool | None


class AdapterError(ValueError):
    """Payload cannot be normalised with the configured adapter/spec."""


class StandardizedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    normalized_value: float | dict[str, ScalarValue]
    confidence_score: float = Field(ge=0.0, le=1.0)
    source_timestamp: datetime | None = None
    provider_id: UUID | None = None

    @field_validator("normalized_value")
    @classmethod
    def _finite(cls, value: float | dict[str, ScalarValue]) -> float | dict[str, ScalarValue]:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("normalized_value must be finite.")
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueError(f"normalized_value[{key!r}] must be finite.")
        return value


def resolve_path(document: object, path: str) -> object:
    """Dotted path resolver: ``data.items.0.price`` (integers index lists)."""
    current = document
    for token in (t for t in path.split(".") if t):
        if isinstance(current, Mapping):
            if token not in current:
                raise AdapterError(f"Path segment '{token}' not found in '{path}'.")
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise AdapterError(f"Invalid list index '{token}' in '{path}'.") from exc
        else:
            raise AdapterError(f"Cannot descend into scalar at '{token}' in '{path}'.")
    return current


TimestampFormat = Literal["iso8601", "epoch_s", "epoch_ms"]


def parse_timestamp(value: object, fmt: TimestampFormat) -> datetime:
    try:
        if fmt == "iso8601":
            if not isinstance(value, str):
                raise AdapterError("ISO-8601 timestamp must be a string.")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise AdapterError("Epoch timestamp must be numeric.")
            seconds = float(value) / (1000.0 if fmt == "epoch_ms" else 1.0)
            parsed = datetime.fromtimestamp(seconds, tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise AdapterError(f"Unparseable timestamp {value!r}.") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class BaseProviderAdapter(ABC):
    """Subclass per provider family; register with ``@register_adapter``."""

    adapter_key: ClassVar[str]

    def __init__(self, provider_id: UUID, spec: Mapping[str, Any] | None) -> None:
        self.provider_id = provider_id
        self.spec: Mapping[str, Any] = spec or {}

    @abstractmethod
    def normalize_payload(self, raw_payload: dict[str, Any]) -> StandardizedEvent:
        """Map a provider-native payload to the canonical event. Raise ``AdapterError`` on failure."""

    def extract_source_timestamp(self, raw_payload: object, headers: Mapping[str, str]) -> datetime | None:
        """Provider clock for the Double-Timestamp Law. Default: no payload timestamp."""
        return None


ADAPTER_REGISTRY: dict[str, type[BaseProviderAdapter]] = {}


def register_adapter(cls: type[BaseProviderAdapter]) -> type[BaseProviderAdapter]:
    key = getattr(cls, "adapter_key", None)
    if not isinstance(key, str) or not key:
        raise TypeError(f"{cls.__name__} must define a non-empty adapter_key.")
    if key in ADAPTER_REGISTRY and ADAPTER_REGISTRY[key] is not cls:
        raise ValueError(f"Adapter key '{key}' is already registered.")
    ADAPTER_REGISTRY[key] = cls
    return cls


def build_adapter(adapter_key: str | None, provider_id: UUID, spec: Mapping[str, Any] | None) -> BaseProviderAdapter | None:
    if not adapter_key:
        return None
    cls = ADAPTER_REGISTRY.get(adapter_key)
    if cls is None:
        raise AdapterError(f"Unknown adapter_key '{adapter_key}'.")
    return cls(provider_id, spec)


class JsonPathSpec(BaseModel):
    """Database-stored mapping that lets most providers be onboarded with zero code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id_path: str = Field(min_length=1)
    event_type: str | None = None
    event_type_path: str | None = None
    value_path: str | None = None
    value_paths: dict[str, str] | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    confidence_path: str | None = None
    timestamp_path: str | None = None
    timestamp_format: TimestampFormat = "iso8601"

    @model_validator(mode="after")
    def _exclusive(self) -> Self:
        if (self.value_path is None) == (self.value_paths is None):
            raise ValueError("Exactly one of value_path or value_paths is required.")
        if (self.event_type is None) == (self.event_type_path is None):
            raise ValueError("Exactly one of event_type or event_type_path is required.")
        return self


def _as_scalar(value: object) -> ScalarValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    raise AdapterError(f"Non-scalar value {type(value).__name__} cannot be normalised.")


@register_adapter
class JsonPathAdapter(BaseProviderAdapter):
    adapter_key = "jsonpath"

    def __init__(self, provider_id: UUID, spec: Mapping[str, Any] | None) -> None:
        super().__init__(provider_id, spec)
        try:
            self._spec = JsonPathSpec.model_validate(dict(self.spec))
        except ValidationError as exc:
            raise AdapterError(f"Invalid jsonpath normalization_spec: {exc.error_count()} error(s).") from exc

    def normalize_payload(self, raw_payload: dict[str, Any]) -> StandardizedEvent:
        s = self._spec
        entity = resolve_path(raw_payload, s.entity_id_path)
        event_type = s.event_type if s.event_type is not None else resolve_path(raw_payload, s.event_type_path or "")
        if s.value_path is not None:
            raw_value = resolve_path(raw_payload, s.value_path)
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
                raise AdapterError("value_path must resolve to a number.")
            try:
                value: float | dict[str, ScalarValue] = float(raw_value)
            except ValueError as exc:
                raise AdapterError(f"value_path resolved to non-numeric {raw_value!r}.") from exc
        else:
            value = {name: _as_scalar(resolve_path(raw_payload, path)) for name, path in (s.value_paths or {}).items()}
        confidence = s.confidence
        if s.confidence_path is not None:
            raw_conf = resolve_path(raw_payload, s.confidence_path)
            if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)):
                raise AdapterError("confidence_path must resolve to a number.")
            confidence = min(max(float(raw_conf), 0.0), 1.0)
        try:
            return StandardizedEvent(
                entity_id=str(entity),
                event_type=str(event_type),
                normalized_value=value,
                confidence_score=confidence,
                source_timestamp=self.extract_source_timestamp(raw_payload, {}),
                provider_id=self.provider_id,
            )
        except ValidationError as exc:
            raise AdapterError(f"Normalised event failed validation: {exc.error_count()} error(s).") from exc

    def extract_source_timestamp(self, raw_payload: object, headers: Mapping[str, str]) -> datetime | None:
        if self._spec.timestamp_path is None:
            return None
        try:
            return parse_timestamp(resolve_path(raw_payload, self._spec.timestamp_path), self._spec.timestamp_format)
        except AdapterError:
            return None
