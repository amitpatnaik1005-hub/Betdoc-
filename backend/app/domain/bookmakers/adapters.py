"""Bookmaker adapter plugins resolved dynamically through ``bookmaker_registry``."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from numbers import Real
from typing import TYPE_CHECKING, ClassVar, Final

from app.core.registry import PluginRegistry

if TYPE_CHECKING:
    from app.models.bookmakers import BookmakerConfigModel

GENERIC_ADAPTER_KEY: Final[str] = "generic"


def format_odds(odds: float) -> str:
    """Render decimal odds with 2 or 3 decimals (2.1 -> "2.10", 1.952 -> "1.952")."""
    if isinstance(odds, bool) or not isinstance(odds, Real):
        raise ValueError(f"odds must be a real number, got {type(odds).__name__}.")
    value = float(odds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"odds must be a finite, positive number, got {value!r}.")
    rendered = f"{value:.3f}"
    return rendered[:-1] if rendered.endswith("0") else rendered


def _clean_text(field: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value.strip()


class BookmakerAdapter(ABC):
    """Base class for every bookmaker integration."""

    display_name: ClassVar[str | None] = None

    def __init__(self, bookmaker_name: str, config: BookmakerConfigModel | None = None) -> None:
        self.bookmaker_name = _clean_text("bookmaker_name", bookmaker_name)
        self.config = config

    @property
    def label(self) -> str:
        return self.display_name or self.bookmaker_name

    def generate_placement_instruction(
        self,
        sport: str,
        league: str,
        match: str,
        market: str,
        selection: str,
        odds: float,
    ) -> str:
        return self._render(
            sport=_clean_text("sport", sport),
            league=_clean_text("league", league),
            match=_clean_text("match", match),
            market=_clean_text("market", market),
            selection=_clean_text("selection", selection),
            odds=format_odds(odds),
        )

    @abstractmethod
    def _render(
        self, *, sport: str, league: str, match: str, market: str, selection: str, odds: str
    ) -> str:
        """Produce the bookmaker-specific instruction from validated inputs."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(bookmaker_name={self.bookmaker_name!r})"


bookmaker_registry: Final[PluginRegistry[BookmakerAdapter]] = PluginRegistry[BookmakerAdapter](
    "bookmakers"
)


@bookmaker_registry.register("Stake")
class StakeAdapter(BookmakerAdapter):
    display_name = "Stake"

    def _render(
        self, *, sport: str, league: str, match: str, market: str, selection: str, odds: str
    ) -> str:
        return (
            f"On {self.label}: Open Sports → {sport} → {league} → {match} → {market} "
            f"→ Select {selection} ({odds})"
        )


@bookmaker_registry.register("Parimatch")
class ParimatchAdapter(BookmakerAdapter):
    display_name = "Parimatch"

    def _render(
        self, *, sport: str, league: str, match: str, market: str, selection: str, odds: str
    ) -> str:
        return (
            f"On {self.label}: Go to {sport} → {league} → {match} → {market} "
            f"→ Add {selection} ({odds}) to Bet Slip"
        )


@bookmaker_registry.register("1xBet", aliases=("OneXBet", "1x Bet"))
class OneXBetAdapter(BookmakerAdapter):
    display_name = "1xBet"

    def _render(
        self, *, sport: str, league: str, match: str, market: str, selection: str, odds: str
    ) -> str:
        return (
            f"On {self.label}: Sports → {sport} → {league} → {match} → {market} "
            f"→ Click {selection} ({odds})"
        )


@bookmaker_registry.register("Pinnacle")
class PinnacleAdapter(BookmakerAdapter):
    display_name = "Pinnacle"

    def _render(
        self, *, sport: str, league: str, match: str, market: str, selection: str, odds: str
    ) -> str:
        return (
            f"On {self.label}: Select {sport} → {league} → {match} → {market} "
            f"→ Select {selection} ({odds})"
        )


@bookmaker_registry.register("Betfair", aliases=("Betfair Exchange",))
class BetfairAdapter(BookmakerAdapter):
    display_name = "Betfair"

    def _render(
        self, *, sport: str, league: str, match: str, market: str, selection: str, odds: str
    ) -> str:
        return (
            f"On {self.label}: Exchange → {sport} → {league} → {match} → {market} "
            f"→ Back {selection} ({odds})"
        )


@bookmaker_registry.register(GENERIC_ADAPTER_KEY, aliases=("default",), fallback=True)
class GenericAdapter(BookmakerAdapter):
    """Fallback used for any bookmaker without a dedicated adapter."""

    def _render(
        self, *, sport: str, league: str, match: str, market: str, selection: str, odds: str
    ) -> str:
        return (
            f"On {self.bookmaker_name}: Navigate to {sport} > {match} "
            f"and select {selection} at odds {odds}"
        )


def create_adapter(
    bookmaker_name: str, config: BookmakerConfigModel | None = None
) -> BookmakerAdapter:
    """Return a fresh adapter for ``bookmaker_name``, falling back to ``GenericAdapter``."""
    return bookmaker_registry.create(bookmaker_name, bookmaker_name, config)
