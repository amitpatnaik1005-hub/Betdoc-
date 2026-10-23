"""Sports-data feeds that price nothing (Group 112): Football-Data.org and Sportmonks. Developed for Amit Ashok Kumar Patnaik.

They say when a fixture kicks off, how it stands, its score and where its sides sit in their table; the match board shows
that beside the Odds API's prices. A client never logs or raises with a URL or a key.
"""

from __future__ import annotations

from typing import Any

import httpx


class SportsDataError(RuntimeError):
    """The feed could not be read (network, an unexpected answer). The message never carries a key."""


class SportsDataAuthError(SportsDataError):
    """401/403: the key is missing, wrong or outside its plan."""


class SportsDataRateLimited(SportsDataError):
    """429: the plan's rate limit; the next scheduled run tries again."""


async def get_json(http: httpx.AsyncClient, provider: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None, timeout: float) -> tuple[Any, httpx.Headers]:
    """GET one JSON document. Errors name the provider and the HTTP status, never the URL (a key may ride in it)."""
    try:
        response = await http.get(url, params=params, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise SportsDataError(f"{provider}: {type(exc).__name__}") from None
    if response.status_code in (401, 403):
        raise SportsDataAuthError(f"{provider}: HTTP {response.status_code} (the key is missing, wrong or outside its plan)")
    if response.status_code == 429:
        raise SportsDataRateLimited(f"{provider}: HTTP 429 (rate limited)")
    if response.status_code != 200:
        raise SportsDataError(f"{provider}: HTTP {response.status_code}")
    try:
        return response.json(), response.headers
    except ValueError:
        raise SportsDataError(f"{provider}: the answer is not JSON") from None


__all__ = ["SportsDataAuthError", "SportsDataError", "SportsDataRateLimited", "get_json"]
