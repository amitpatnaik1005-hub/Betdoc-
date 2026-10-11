"""
Universal Multi-Sport Adapter Registry (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

One instance per sport, dispatched by its canonical key. ``sport_for_odds_key`` maps a feed's sport key
(The Odds API's ``americanfootball_nfl``, ``icehockey_nhl``, ``mma_mixed_martial_arts`` ...) to the adapter
that prices it, so the odds poller, the ledger and the twin all land on the same model.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.domain.sports.american_football_adapter import AmericanFootballSportAdapter
from app.domain.sports.base_sport_adapter import BaseSportAdapter
from app.domain.sports.baseball_adapter import BaseballSportAdapter
from app.domain.sports.basketball_adapter import BasketballSportAdapter
from app.domain.sports.combat_adapter import CombatSportAdapter
from app.domain.sports.cricket_adapter import CricketSportAdapter
from app.domain.sports.errors import SportNotFoundError
from app.domain.sports.esports_adapter import EsportsSportAdapter
from app.domain.sports.ice_hockey_adapter import IceHockeySportAdapter
from app.domain.sports.soccer_adapter import SoccerSportAdapter
from app.domain.sports.tennis_adapter import TennisSportAdapter

# Feed sport-key prefixes (the part before the first underscore, or the whole key) -> adapter sport key
FAMILY_TO_SPORT: dict[str, str] = {
    "soccer": "soccer",
    "football": "soccer",
    "cricket": "cricket",
    "basketball": "basketball",
    "tennis": "tennis",
    "baseball": "baseball",
    "americanfootball": "american_football",
    "american_football": "american_football",
    "icehockey": "ice_hockey",
    "ice_hockey": "ice_hockey",
    "hockey": "ice_hockey",
    "mma": "combat",
    "boxing": "combat",
    "combat": "combat",
    "esports": "esports",
}

SPORT_KEYS: tuple[str, ...] = (
    "soccer", "cricket", "basketball", "tennis", "baseball", "american_football", "ice_hockey", "combat", "esports",
)


def sport_for_odds_key(odds_sport_key: str | None) -> str | None:
    """``"americanfootball_nfl"`` -> ``"american_football"``; an unknown family -> None."""
    key = (odds_sport_key or "").strip().lower()
    if not key:
        return None
    if key in FAMILY_TO_SPORT:
        return FAMILY_TO_SPORT[key]
    for sport in SPORT_KEYS:
        if key == sport:
            return sport
    return FAMILY_TO_SPORT.get(key.split("_", 1)[0])


class SportAdapterRegistry:
    """
    Central factory and lookup registry for all 9 quantitative sport adapters.
    Provides singleton instances and uniform dispatch.
    """

    _adapters: ClassVar[dict[str, BaseSportAdapter]] = {}

    @classmethod
    def initialize(cls) -> None:
        """Registers all 9 production sport domain adapters."""
        adapters: list[BaseSportAdapter] = [
            SoccerSportAdapter(),
            CricketSportAdapter(),
            BasketballSportAdapter(),
            TennisSportAdapter(),
            BaseballSportAdapter(),
            AmericanFootballSportAdapter(),
            IceHockeySportAdapter(),
            CombatSportAdapter(),
            EsportsSportAdapter(),
        ]
        cls._adapters = {adapter.sport_key: adapter for adapter in adapters}

    @classmethod
    def get_adapter(cls, sport_key: str) -> BaseSportAdapter:
        """The adapter for ``sport_key`` (a canonical key or a feed key such as ``basketball_nba``).
        An unknown sport raises ``SportNotFoundError``: a football model never prices another sport."""
        if not cls._adapters:
            cls.initialize()
        key = (sport_key or "").strip().lower()
        adapter = cls._adapters.get(key)
        if adapter is None:
            mapped = sport_for_odds_key(key)
            adapter = cls._adapters.get(mapped) if mapped else None
        if adapter is None:
            raise SportNotFoundError(sport_key)
        return adapter

    @classmethod
    def all_adapters(cls) -> list[BaseSportAdapter]:
        if not cls._adapters:
            cls.initialize()
        return list(cls._adapters.values())

    @classmethod
    def list_supported_sports(cls) -> list[dict[str, Any]]:
        """Returns metadata for all 9 registered sport adapters."""
        return [
            {
                "sport_key": a.sport_key,
                "display_name": a.display_name,
                "standard_markets": a.standard_markets,
                "supported_markets": a.supported_markets,
                "bet_types": sorted(a.BET_TYPES),
                "model_family": a.model_family,
                "kelly_multiplier": a.kelly_multiplier,
                "odds_api_family": a.odds_api_family,
                "has_draw": a.has_draw,
            }
            for a in cls.all_adapters()
        ]
