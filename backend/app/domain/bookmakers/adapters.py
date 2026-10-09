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


# =============================================================================================
# Group 69: canonical bookmakers and each bookmaker's market naming (Ashoka's slips)
# =============================================================================================
# Feeds name books by their own keys (The Odds API: "onexbet", "betfair_ex_uk"). Ashoka speaks of five
# books, in this priority. Odds for any of them come only from an authorised feed or from the price the
# user types in: nothing here fetches a bookmaker's site.
ASHOKA_BOOKMAKERS: Final[dict[str, str]] = {
    "parimatch": "Parimatch",
    "1xbet": "1xBet",
    "stake": "Stake",
    "pinnacle": "Pinnacle",
    "betfair": "Betfair",
}
FEED_BOOKMAKER_ALIASES: Final[dict[str, str]] = {
    "parimatch": "parimatch",
    "pari_match": "parimatch",
    "onexbet": "1xbet",
    "1xbet": "1xbet",
    "one_x_bet": "1xbet",
    "stake": "stake",
    "stake_com": "stake",
    "pinnacle": "pinnacle",
    "betfair": "betfair",
    "betfair_ex_uk": "betfair",
    "betfair_ex_eu": "betfair",
    "betfair_ex_au": "betfair",
}
EXCHANGE_BOOKMAKERS: Final[frozenset[str]] = frozenset({"betfair"})  # back singles only: the exchange takes no multiples


def canonical_bookmaker(raw: str | None) -> str | None:
    """A feed's bookmaker key as one of Ashoka's five (``"onexbet"`` -> ``"1xbet"``), or None."""
    if not raw:
        return None
    key = str(raw).strip().casefold().replace(" ", "_").replace("-", "_")
    return FEED_BOOKMAKER_ALIASES.get(key) or (key if key in ASHOKA_BOOKMAKERS else None)


def fixture_name(home: str, away: str) -> str:
    """``"Arsenal vs Chelsea"``: the name both priority books search by."""
    return f"{_clean_text('home', home)} vs {_clean_text('away', away)}"


def _team(selection: str, home: str, away: str) -> str:
    return {"HOME": home, "AWAY": away}.get(selection, selection.title())


def market_selection_label(market_key: str, selection: str, home: str, away: str) -> str:
    """The market and selection as the slip shows them: ``"1X2: Arsenal"``, ``"Totals: Over 2.5"``,
    ``"Both Teams to Score: YES"``, ``"Asian Handicap: Arsenal (-0.5)"``."""
    from app.domain.oracle.markets import MarketKind, parse_market  # noqa: PLC0415 - the oracle imports this module

    ref = parse_market(market_key)
    if ref is None:
        return f"{market_key}: {selection}"
    if ref.kind is MarketKind.MATCH_ODDS:
        return f"1X2: {'Draw' if selection == 'DRAW' else _team(selection, home, away)}"
    if ref.kind is MarketKind.TOTALS:
        return f"Totals: {selection.title()} {ref.line:g}"
    if ref.kind is MarketKind.BTTS:
        return f"Both Teams to Score: {selection.upper()}"
    if ref.kind is MarketKind.ASIAN_HANDICAP:
        line = ref.line if selection == "HOME" else -float(ref.line)  # type: ignore[arg-type]
        return f"Asian Handicap: {_team(selection, home, away)} ({line:+g})"
    if ref.kind is MarketKind.DOUBLE_CHANCE:
        pair = {"1X": f"{home} or Draw", "12": f"{home} or {away}", "X2": f"Draw or {away}"}[selection]
        return f"Double Chance: {pair}"
    return f"Draw No Bet: {_team(selection, home, away)}"


def _search_codes(book: str, market_key: str, selection: str) -> str:
    """The short code each book's own slip and search use (1xBet: W1/X/W2, Parimatch: 1/X/2)."""
    from app.domain.oracle.markets import MarketKind, parse_market  # noqa: PLC0415

    ref = parse_market(market_key)
    if ref is None:
        return selection
    onex = book == "1xbet"
    if ref.kind is MarketKind.MATCH_ODDS:
        return ({"HOME": "W1", "DRAW": "X", "AWAY": "W2"} if onex else {"HOME": "1", "DRAW": "X", "AWAY": "2"})[selection]
    if ref.kind is MarketKind.TOTALS:
        return f"Total {selection.title()} ({ref.line:g})"
    if ref.kind is MarketKind.BTTS:
        return f"Both Teams To Score - {selection.title()}" if onex else f"Both teams to score: {selection.title()}"
    if ref.kind is MarketKind.ASIAN_HANDICAP:
        side, line = ("1", ref.line) if selection == "HOME" else ("2", -float(ref.line))  # type: ignore[arg-type]
        return f"{'Asian Handicap' if onex else 'Handicap'} {side} ({line:+g})"
    if ref.kind is MarketKind.DOUBLE_CHANCE:
        return selection
    return f"{'W1' if onex else '1'} (DNB)" if selection == "HOME" else f"{'W2' if onex else '2'} (DNB)"


def slip_line(book: str, market_key: str, selection: str, home: str, away: str) -> dict[str, str]:
    """Everything one leg needs on a bookmaker's view of a slip."""
    return {
        "fixture": fixture_name(home, away),
        "market": market_selection_label(market_key, selection, home, away),
        "search_code": _search_codes(book, market_key, selection),
        "bookmaker": ASHOKA_BOOKMAKERS.get(book, book),
    }
