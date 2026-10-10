"""The Vault's runtime fleet configuration, as every process sees it (Group 70).

Settings come from the environment once per process; what the Vault imports or the Control Panel
toggles must reach a running worker without a restart. This module holds that overlay: the sports
activated from the user's file, the markets each sport's Odds API call requests, the currency each
bookmaker's accounts hold, the quiet hours and whether execution routes through the Vault's accounts.

It holds state only, with no I/O: ``app.services.vault.fleet_config`` loads it from the database
(the source of truth) through a versioned Redis mirror and ``install``s it, at most every few seconds.
``Settings.odds_sport_keys`` and ``bookmaker_terms`` read it, so the fleet's coverage and the
currency routing pick an import up on their next tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

H2H = "h2h"
FULL_MARKETS = "h2h,totals,spreads"  # three credits per region and call, against one for h2h
MARKET_PROFILES = {"h2h": H2H, "full": FULL_MARKETS}


@dataclass(frozen=True, slots=True)
class FleetOverlay:
    version: int = 0
    sports: tuple[str, ...] = ()  # activated from the Vault, on top of ODDS_SPORT_KEYS
    markets_by_sport: dict[str, str] = field(default_factory=dict)  # sport key (or "*") -> "h2h" | "h2h,totals,spreads"
    currencies: dict[str, str] = field(default_factory=dict)  # bookmaker id -> its accounts' currency
    quiet_start: time | None = None  # inside the quiet hours every sport falls back to h2h
    quiet_end: time | None = None
    timezone: str = "Asia/Kolkata"
    account_routing: bool = False  # execution picks one of the Vault's accounts per order

    def in_quiet_hours(self, now: datetime) -> bool:
        if self.quiet_start is None or self.quiet_end is None or self.quiet_start == self.quiet_end:
            return False
        try:
            local = now.astimezone(ZoneInfo(self.timezone)).time()
        except (KeyError, ValueError):
            local = now.time()
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= local < self.quiet_end
        return local >= self.quiet_start or local < self.quiet_end  # across midnight

    def markets_for(self, sport: str, now: datetime) -> str | None:
        """None: the overlay has no say for this sport (the environment decides)."""
        if self.in_quiet_hours(now):
            return H2H
        return self.markets_by_sport.get(sport) or self.markets_by_sport.get("*")


_current = FleetOverlay()


def current() -> FleetOverlay:
    return _current


def install(overlay: FleetOverlay) -> None:
    global _current
    _current = overlay


def reset() -> None:
    """Back to empty (tests)."""
    install(FleetOverlay())
