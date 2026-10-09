"""Request bodies of the Sentinel API (Group 68). Responses are plain dicts built in the router."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.sentinel import Severity


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelUpdate(_Body):
    enabled: bool | None = None
    config: dict[str, Any] | None = None  # the channel's non-secret settings (see registry.CONFIG_FIELDS)


class ChannelCredentials(_Body):
    """Secrets for one channel. A field left out keeps its stored value; an empty string clears it."""

    values: dict[str, str] = Field(min_length=1)


class RoutingUpdate(_Body):
    matrix: dict[str, list[str]]


class TestAlert(_Body):
    severity: Severity = Severity.WARNING
    title: str | None = Field(default=None, max_length=160)
