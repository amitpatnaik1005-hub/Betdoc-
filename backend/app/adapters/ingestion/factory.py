"""Universal Ingestion Matrix: any sports-odds API described by configuration, not code.

A ``ProviderSpec`` (stored as JSON in ``omni_fleet_sources.spec``, created from Fleet Command's
"Add provider" dialog) says where to call, how to authenticate, how fast, which canonical sports it
covers, and how its JSON maps onto fixtures::

    events          $.data.events[*]            -> one node per fixture
    event_id / home / away / commence_time      -> fields of that node
    books           bookmakers[*]               -> optional: aggregators quote many books
    markets         markets[*]   + market_key / market_values ("1x2", "match_winner", "h2h")
    outcomes        outcomes[*]                 -> one node per selection ...
    outcome_name    name                        ... or "@key" when outcomes are object members
    price           {path: price, format: decimal | american | fractional | probability}

Selection names map to HOME / DRAW / AWAY through ``selections`` ("1", "X", "2", "W1", "Draw",
or ``{home}`` / ``{away}`` for the team names themselves, matched through the alias graph).

``UniversalDataIngestor`` fetches with the same retries, rate limiting and quota tracking as the
hand-written adapters; ``SpecMapper`` turns each response into ``RawFixture``s for the normaliser.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.adapters.base_adapter import TimestampFormat, parse_timestamp
from app.adapters.ingestion.base import BaseDataIngestor, IngestionBatch, IngestionError, SourcePayload
from app.adapters.ingestion.jsonpath import JsonPathError, compile_path, first, select
from app.core.config import Settings
from app.core.security_vault import UnsafeTargetError, assert_public_target
from app.services.omni_normalizer import (
    AWAY,
    DRAW,
    HOME,
    AliasDictionary,
    DevigMethod,
    OddsFormat,
    RawFixture,
    implied_probability,
    same_side,
)

SOURCE_ID_PATTERN = r"^[a-z][a-z0-9_]{2,47}$"
_SPORT_KEY = re.compile(r"^[a-z0-9_]{2,64}$")
_ENV_NAME = r"^[A-Z][A-Z0-9_]{2,127}$"
ENTRY_KEY, ENTRY_VALUE = "@key", "@value"


def default_secret_env(source_id: str) -> str:
    """Where a config provider's key lives in the environment unless its spec names a variable."""
    return f"OMNI_{source_id.upper()}_API_KEY"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthSpec(_Strict):
    type: Literal["none", "query", "header", "bearer"] = "none"
    param: str | None = Field(default=None, max_length=64)  # query parameter or header name
    secret_env: str | None = Field(default=None, pattern=_ENV_NAME)  # env var holding the key

    @model_validator(mode="after")
    def _param_required(self) -> Self:
        if self.type in ("query", "header") and not self.param:
            raise ValueError(f"auth.type '{self.type}' needs auth.param")
        return self


class RequestSpec(_Strict):
    path: str = Field(min_length=1, max_length=512)  # may contain {sport}
    params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        if not value.startswith("/") or "://" in value:
            raise ValueError("path must be relative to base_url and start with '/'")
        return value


class RateLimitSpec(_Strict):
    requests_per_minute: float = Field(default=30.0, gt=0, le=6000)
    burst: int = Field(default=5, ge=1, le=1000)


class QuotaSpec(_Strict):
    remaining_header: str | None = Field(default=None, max_length=64)
    used_header: str | None = Field(default=None, max_length=64)
    limit: float | None = Field(default=None, gt=0)  # plan size, when the provider only reports "remaining"


class PriceSpec(_Strict):
    path: str = Field(min_length=1)  # relative to the outcome node, or "@value" for member values
    format: OddsFormat = "decimal"


class SelectionSpec(_Strict):
    home: list[str] = Field(default_factory=lambda: ["1", "home", "w1", "p1", "{home}"])
    draw: list[str] = Field(default_factory=lambda: ["x", "draw", "tie", "d"])
    away: list[str] = Field(default_factory=lambda: ["2", "away", "w2", "p2", "{away}"])


class MappingSpec(_Strict):
    events: str
    event_id: str
    home: str
    away: str
    commence_time: str
    commence_format: TimestampFormat = "iso8601"
    books: str | None = None
    book_name: str | None = None
    markets: str
    market_key: str | None = None
    market_values: list[str] = Field(default_factory=list)
    outcomes: str
    outcome_name: str
    price: PriceSpec
    suspended: str | None = None
    selections: SelectionSpec = Field(default_factory=SelectionSpec)

    @model_validator(mode="after")
    def _paths_compile(self) -> Self:
        for name in ("events", "event_id", "home", "away", "commence_time", "books", "book_name", "markets", "market_key", "outcomes", "suspended"):
            value = getattr(self, name)
            if value is not None:
                try:
                    compile_path(value)
                except JsonPathError as exc:
                    raise ValueError(f"mapping.{name}: {exc}") from None
        if self.outcome_name != ENTRY_KEY:
            try:
                compile_path(self.outcome_name)
            except JsonPathError as exc:
                raise ValueError(f"mapping.outcome_name: {exc}") from None
        if self.price.path != ENTRY_VALUE:
            try:
                compile_path(self.price.path)
            except JsonPathError as exc:
                raise ValueError(f"mapping.price.path: {exc}") from None
        if self.price.path == ENTRY_VALUE and self.outcome_name != ENTRY_KEY:
            raise ValueError("price.path '@value' only makes sense with outcome_name '@key'")
        return self


class ProviderSpec(_Strict):
    display_name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=280)
    docs_url: HttpUrl | None = None
    base_url: HttpUrl
    auth: AuthSpec = Field(default_factory=AuthSpec)
    requests: list[RequestSpec] = Field(min_length=1, max_length=10)
    coverage: dict[str, str] = Field(min_length=1, max_length=200)  # canonical sport key -> provider-native id
    rate_limit: RateLimitSpec = Field(default_factory=RateLimitSpec)
    quota: QuotaSpec = Field(default_factory=QuotaSpec)
    cost: Literal["free", "metered"] = "metered"
    priority: int = Field(default=50, ge=0, le=1000)  # lower = preferred within a market group
    interval_seconds: float = Field(default=60.0, ge=10, le=86_400)
    devig: DevigMethod = "shin"
    mapping: MappingSpec

    @field_validator("coverage")
    @classmethod
    def _canonical_keys(cls, value: dict[str, str]) -> dict[str, str]:
        bad = [key for key in value if not _SPORT_KEY.match(key)]
        if bad:
            raise ValueError(f"coverage keys must be canonical sport keys like 'soccer_epl': {bad[:3]}")
        return value

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("base_url must use https")
        return value

    def secret_env(self, source_id: str) -> str:
        return self.auth.secret_env or default_secret_env(source_id)


# ---------------------------------------------------------------- fetching
class UniversalDataIngestor(BaseDataIngestor):
    """One instance per configured provider. Behaves exactly like a hand-written adapter."""

    is_config: ClassVar[bool] = True

    def __init__(self, source_id: str, spec: ProviderSpec, http: httpx.AsyncClient, settings: Settings, **kwargs: Any) -> None:
        self.source_id = source_id  # type: ignore[misc]
        self.display_name = spec.display_name  # type: ignore[misc]
        self.description = spec.description  # type: ignore[misc]
        self.requires_api_key = spec.auth.type != "none"  # type: ignore[misc]
        self.docs_url = str(spec.docs_url) if spec.docs_url else None  # type: ignore[misc]
        self.requests_per_minute = spec.rate_limit.requests_per_minute  # type: ignore[misc]
        self.burst = spec.rate_limit.burst  # type: ignore[misc]
        self.quota_remaining_header = spec.quota.remaining_header  # type: ignore[misc]
        self.quota_used_header = spec.quota.used_header  # type: ignore[misc]
        self.quota_limit = spec.quota.limit  # type: ignore[misc]
        self.spec = spec
        super().__init__(http, settings, **kwargs)

    @classmethod
    def interval_seconds(cls, settings: Settings) -> float:  # noqa: ARG003 - config providers carry their own
        return 60.0

    async def _target(self, path: str) -> str:
        """SSRF guard: admins type these URLs, so private/loopback targets are refused (DNS off-loop)."""
        url = f"{str(self.spec.base_url).rstrip('/')}{path}"
        schemes = frozenset({"https", "http"} if self._settings.omni_allow_insecure_http else {"https"})
        try:
            await asyncio.to_thread(
                assert_public_target, url, allowed_schemes=schemes, allow_private=self._settings.omni_allow_private_networks
            )
        except UnsafeTargetError as exc:
            raise IngestionError(f"{self.display_name}: blocked unsafe target ({exc})") from None
        return url

    def _credentials(self, params: dict[str, str], headers: dict[str, str]) -> None:
        auth = self.spec.auth
        if auth.type == "none" or not self._api_key:
            return
        if auth.type == "query":
            params[auth.param or "apiKey"] = self._api_key
        elif auth.type == "header":
            headers[auth.param or "X-API-Key"] = self._api_key
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"

    async def fetch(self, scope: Sequence[str] | None = None) -> IngestionBatch:
        """``scope``: provider-native ids from ``coverage`` values; None = all of them."""
        native_to_canonical = {native: canonical for canonical, native in self.spec.coverage.items()}
        natives = [n for n in (scope if scope is not None else native_to_canonical) if n in native_to_canonical]
        if not natives:
            raise IngestionError(f"{self.display_name}: nothing in scope")
        payloads: list[SourcePayload] = []
        for native in natives:
            for request in self.spec.requests:
                params = {k: v.replace("{sport}", native) for k, v in request.params.items()}
                headers = {k: v.replace("{sport}", native) for k, v in request.headers.items()}
                self._credentials(params, headers)
                url = await self._target(request.path.replace("{sport}", quote(native, safe="/")))
                data = await self._get_json(url, params=params, headers=headers)
                payloads.append(SourcePayload(key=native_to_canonical[native], data=data))
        return self._batch(payloads, {"scope": natives})


# ---------------------------------------------------------------- mapping
def _price_entries(market: Any, mapping: MappingSpec) -> list[tuple[str, Any]]:
    """(outcome name, raw price) pairs from one market node."""
    if mapping.outcome_name == ENTRY_KEY:
        pairs: list[tuple[str, Any]] = []
        for container in select(market, mapping.outcomes):
            if isinstance(container, Mapping):
                for name, value in container.items():
                    price = value if mapping.price.path == ENTRY_VALUE else first(value, mapping.price.path)
                    pairs.append((str(name), price))
        return pairs
    return [
        (str(first(node, mapping.outcome_name, "")), first(node, mapping.price.path))
        for node in select(market, mapping.outcomes)
    ]


class SpecMapper:
    """Turns one provider response into ``RawFixture``s (``None`` marks an event that failed to parse)."""

    def __init__(self, spec: ProviderSpec, aliases: AliasDictionary) -> None:
        self._spec = spec
        self._aliases = aliases
        selections = spec.mapping.selections
        self._names = {
            HOME: [s.casefold() for s in selections.home],
            DRAW: [s.casefold() for s in selections.draw],
            AWAY: [s.casefold() for s in selections.away],
        }
        self._wanted_markets = {m.casefold() for m in spec.mapping.market_values}

    def __call__(self, payload: SourcePayload) -> list[RawFixture | None]:
        return [self._fixture(payload.key, event) for event in select(payload.data, self._spec.mapping.events)]

    def _label(self, sport: str, name: str, home: str, away: str) -> str | None:
        folded = name.strip().casefold()
        for label, options in self._names.items():
            if folded in options:
                return label
        for label, team, placeholder in ((HOME, home, "{home}"), (AWAY, away, "{away}")):
            if placeholder in self._names[label] and same_side(self._aliases, sport, name, team):
                return label
        return None

    def _market(self, book: Any) -> Any | None:
        mapping = self._spec.mapping
        for market in select(book, mapping.markets):
            if mapping.market_key is None or not self._wanted_markets:
                return market
            key = first(market, mapping.market_key)
            if key is not None and str(key).casefold() in self._wanted_markets:
                return market
        return None

    def _fixture(self, sport: str, event: Any) -> RawFixture | None:
        mapping = self._spec.mapping
        try:
            event_id = first(event, mapping.event_id)
            home, away = first(event, mapping.home), first(event, mapping.away)
            if event_id is None or not home or not away:
                return None
            kickoff = parse_timestamp(first(event, mapping.commence_time), mapping.commence_format)
            books: list[dict[str, float]] = []
            names: list[str] = []
            for book in select(event, mapping.books) if mapping.books else [event]:
                market = self._market(book)
                if market is None:
                    continue
                priced: dict[str, float] = {}
                for name, raw_price in _price_entries(market, mapping):
                    label = self._label(sport, name, str(home), str(away))
                    if label is None or label in priced:
                        priced = {}  # an unknown or duplicated selection makes the book unusable
                        break
                    implied = implied_probability(raw_price, mapping.price.format)
                    priced[label] = float(raw_price) if mapping.price.format == "decimal" else 1.0 / implied
                if len(priced) >= 2:
                    books.append(priced)
                    name = first(book, mapping.book_name) if mapping.book_name else None
                    names.append(str(name).strip()[:64] if name is not None else "")
            suspended = bool(first(event, mapping.suspended)) if mapping.suspended else False
            return RawFixture(
                sport=sport,
                event_id=str(event_id),
                home=str(home),
                away=str(away),
                kickoff=kickoff,
                books=tuple(books),
                suspended=suspended,
                bookmakers=tuple(names),
            )
        except (TypeError, ValueError, KeyError, AttributeError, IndexError):
            return None
