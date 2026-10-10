"""An execution venue as the sniper uses it: a frozen copy of its ``ExecutionVenue`` row; and, for the
Smart Order Router (Group 71), each venue's stake rules and the circuit-breaker policy it trades under."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.models.execution import ExecutionVenue

if TYPE_CHECKING:
    from app.core.config import Settings

SANDBOX_HOST = "sandbox.invalid"  # RFC 2606: can never resolve, so a sandbox venue can only run in-process


@dataclass(frozen=True, slots=True)
class VenueConfig:
    id: str
    display_name: str
    adapter: str
    base_url: str
    auth_type: str
    place_path: str
    status_path: str
    bets_per_second: Decimal
    burst: int
    token_path: str | None = None
    refresh_path: str | None = None
    events_path: str | None = None
    routes: tuple[str, ...] = ()
    selection_codes: dict[str, str] = field(default_factory=dict)
    encrypted_credentials: str | None = None
    is_sandbox: bool = False
    is_enabled: bool = True
    commission_rate: Decimal | None = None  # None: the bookmaker's default (settings)
    currency: str | None = None  # None: the bookmaker's default (settings), else INR
    session_scope: str | None = None  # a Vault account (Group 70): its own login session, apart from the venue's

    @classmethod
    def from_row(cls, row: ExecutionVenue) -> VenueConfig:
        return cls(
            id=row.id,
            display_name=row.display_name,
            adapter=row.adapter,
            base_url=row.base_url,
            auth_type=row.auth_type,
            place_path=row.place_path,
            status_path=row.status_path,
            bets_per_second=Decimal(row.bets_per_second),
            burst=int(row.burst),
            token_path=row.token_path,
            refresh_path=row.refresh_path,
            events_path=row.events_path,
            routes=tuple(row.routes or ()),
            selection_codes=dict(row.selection_codes or {}),
            encrypted_credentials=row.encrypted_credentials,
            is_sandbox=bool(row.is_sandbox),
            is_enabled=bool(row.is_enabled),
            commission_rate=None if row.commission_rate is None else Decimal(row.commission_rate),
            currency=row.currency,
        )

    @property
    def session_key(self) -> str:
        """What the venue's login session is cached under: one per account when a Vault account carries the order."""
        return self.id if self.session_scope is None else f"{self.id}@{self.session_scope}"

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def handles(self, bookmaker_id: str) -> bool:
        return bookmaker_id == self.id or bookmaker_id in self.routes or (self.is_sandbox and "*" in self.routes)

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()


@dataclass(frozen=True, slots=True)
class VenueStakeRules:
    """The smallest and largest single stake a venue takes (in the order's currency). The floor is never
    under ``ROUTER_MIN_SLICE_STAKE``; the ceiling is the book's own rule as the user configured it
    (``ROUTER_VENUE_STAKE_LIMITS``), None when unknown: nothing is assumed about a book's limits."""

    venue_id: str
    min_stake: Decimal
    max_stake: Decimal | None

    @classmethod
    def for_venue(cls, venue_id: str, settings: Settings) -> VenueStakeRules:
        raw = settings.ROUTER_VENUE_STAKE_LIMITS.get(venue_id) or {}
        floor = max(Decimal(str(raw.get("min", 0))), Decimal(settings.ROUTER_MIN_SLICE_STAKE))
        ceiling = raw.get("max")
        return cls(venue_id, floor, None if ceiling in (None, "") else Decimal(str(ceiling)))

    def ceiling(self, capacity: Decimal) -> Decimal:
        """What one slice may carry here: the account's capacity, under the venue's own maximum."""
        return capacity if self.max_stake is None else min(capacity, self.max_stake)


@dataclass(frozen=True, slots=True)
class CircuitPolicy:
    """``failures`` consecutive rejects or timeouts at one venue within ``window`` pause it for ``pause``."""

    failures: int
    window: timedelta
    pause: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> CircuitPolicy:
        return cls(
            int(settings.ROUTER_BREAKER_FAILURES),
            timedelta(seconds=float(settings.ROUTER_BREAKER_WINDOW_SECONDS)),
            timedelta(seconds=float(settings.ROUTER_BREAKER_PAUSE_SECONDS)),
        )
