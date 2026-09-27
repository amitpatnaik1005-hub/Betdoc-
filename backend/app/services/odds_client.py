import logging
import re
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings

logger = logging.getLogger("betdoc.odds_client")

# httpx logs every request URL at INFO, and the Odds API key is a query parameter.
# Raise the level so the key never reaches the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

_SPORT_KEY_RE = re.compile(r"^[a-z0-9_]+$")


class OddsAPIError(RuntimeError):
    """Sanitised error: never contains the request URL (which holds the API key)."""


class OddsAPIAuthError(OddsAPIError):
    """401/403: bad key or exhausted account. Retrying is pointless."""


class QuotaExceededError(OddsAPIError):
    """Remaining request credits fell below the configured floor."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code: int = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


class OddsAPIClient:
    _client: httpx.AsyncClient | None = None
    _requests_remaining: float | None = None

    @classmethod
    def _http(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            cls._client = httpx.AsyncClient(
                base_url=settings.ODDS_API_BASE_URL,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers={"Accept": "application/json", "User-Agent": "betdoc/1.0"},
            )
        return cls._client

    @classmethod
    async def aclose(cls) -> None:
        if cls._client is not None and not cls._client.is_closed:
            await cls._client.aclose()
        cls._client = None

    @classmethod
    def requests_remaining(cls) -> float | None:
        return cls._requests_remaining

    @classmethod
    def _record_quota(cls, response: httpx.Response) -> float | None:
        raw: str | None = response.headers.get("x-requests-remaining")
        if raw is None:
            return None
        try:
            value: float = float(raw)
        except ValueError:
            logger.warning("Unparseable x-requests-remaining header: %r", raw)
            return None
        cls._requests_remaining = value
        return value

    @classmethod
    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get(cls, path: str, params: dict[str, str]) -> httpx.Response:
        response: httpx.Response = await cls._http().get(path, params=params)
        response.raise_for_status()
        return response

    @classmethod
    async def fetch_odds(
        cls,
        sport: str,
        regions: str = "uk,eu",
        markets: str = "h2h",
    ) -> list[dict[str, Any]]:
        if not _SPORT_KEY_RE.match(sport):
            raise OddsAPIError(f"Invalid sport key: {sport!r}")

        api_key: str | None = (
            settings.ODDS_API_KEY.get_secret_value() if settings.ODDS_API_KEY else None
        )
        if not api_key:
            raise OddsAPIAuthError("ODDS_API_KEY is not configured")

        # Gate BEFORE spending a credit, using the last known quota.
        floor: int = settings.ODDS_QUOTA_FLOOR
        if cls._requests_remaining is not None and cls._requests_remaining < floor:
            raise QuotaExceededError(
                f"Odds API quota below floor: remaining={cls._requests_remaining:g} < {floor}"
            )

        params: dict[str, str] = {
            "apiKey": api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }

        try:
            response: httpx.Response = await cls._get(f"/sports/{sport}/odds", params)
        except httpx.HTTPStatusError as exc:
            cls._record_quota(exc.response)
            code: int = exc.response.status_code
            # `from None` drops the original exception, whose message contains the URL + key.
            if code in (401, 403):
                raise OddsAPIAuthError(f"Odds API auth failure: HTTP {code}") from None
            raise OddsAPIError(f"Odds API returned HTTP {code} for sport={sport}") from None
        except httpx.TransportError as exc:
            raise OddsAPIError(
                f"Odds API transport failure for sport={sport}: {exc.__class__.__name__}"
            ) from None

        remaining: float | None = cls._record_quota(response)
        if remaining is not None and remaining < floor:
            logger.warning("Odds API quota nearly exhausted: remaining=%g", remaining)

        try:
            data: Any = response.json()
        except ValueError:
            raise OddsAPIError("Odds API returned non-JSON body") from None
        if not isinstance(data, list):
            raise OddsAPIError(f"Odds API returned {type(data).__name__}, expected list")
        return data
