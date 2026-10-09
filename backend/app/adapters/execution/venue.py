"""An execution venue as the sniper uses it: a frozen copy of its ``ExecutionVenue`` row."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlsplit

from app.models.execution import ExecutionVenue

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

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def handles(self, bookmaker_id: str) -> bool:
        return bookmaker_id == self.id or bookmaker_id in self.routes or (self.is_sandbox and "*" in self.routes)

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()
