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
