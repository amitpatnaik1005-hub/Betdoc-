"""The omni-provider catalogue the multi-sport engine draws on (Group 79). Developed for Amit Ashok Kumar Patnaik.

Categories follow the fleet's confidential provider register:

* A: execution books (Parimatch, the primary capital account; 1xBet and Stake for multiples and props), sharp
  benchmarks and exchanges (Pinnacle, Betfair, The Odds API consensus), high-frequency sports feeds,
  prediction markets, and the authorised headless harvesters;
* B: weather and geocoding (Open-Meteo); C: FX for multi-currency sizing; D: travel logistics; E: news, social
  and OSINT.

A provider's key is resolved in this order: an active ``vault_provider_credentials`` row (decrypted with the
master vault key, bound to the row), then the Settings field named here, then the ``OMNI_<ID>_API_KEY``
environment variable. Nothing here holds a secret; a base URL is listed only where it is the provider's public
API root, otherwise it comes from the vault row.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_id: str
    name: str
    category: str  # A | B | C | D | E
    role: str  # execution | sharp | exchange | aggregator | feed | prediction_market | harvester | weather | fx | travel | news
    vault_id: str | None = None  # VaultProviderCredential.provider_id; bookmakers live in vault_bookmaker_accounts
    key_setting: str | None = None  # Settings attribute holding the key
    url_setting: str | None = None  # Settings attribute holding the base / target URL
    base_url: str | None = None  # public API root when not configurable
    bookmaker: str | None = None  # canonical venue key for execution books
    sports: tuple[str, ...] = ()  # empty: every sport

    @property
    def env_key(self) -> str:
        return f"OMNI_{self.provider_id.upper()}_API_KEY"


PROVIDERS: tuple[ProviderSpec, ...] = (
    # ---- A: execution books
    ProviderSpec("parimatch", "Parimatch", "A", "execution", key_setting="PARIMATCH_FEED_TOKEN", url_setting="PARIMATCH_TARGET_URL", bookmaker="parimatch"),
    ProviderSpec("one_xbet", "1xBet", "A", "execution", url_setting="ONE_XBET_TARGET_URL", bookmaker="1xbet"),
    ProviderSpec("stake", "Stake", "A", "execution", url_setting="STAKE_TARGET_URL", bookmaker="stake"),
    # ---- A: sharp benchmarks, exchange, aggregator
    ProviderSpec("pinnacle", "Pinnacle", "A", "sharp", vault_id="pinnacle_api", key_setting="PINNACLE_API_KEY", url_setting="PINNACLE_API_BASE_URL",
                 bookmaker="pinnacle"),
    ProviderSpec("betfair", "Betfair Exchange", "A", "exchange", key_setting="BETFAIR_APP_KEY", url_setting="BETFAIR_IDENTITY_URL", bookmaker="betfair"),
    ProviderSpec("the_odds_api", "The Odds API", "A", "aggregator", vault_id="odds_api", key_setting="ODDS_API_KEY", url_setting="ODDS_API_BASE_URL"),
    # ---- A: high-frequency sports feeds and micro-data
    ProviderSpec("oddspapi", "Oddspapi.io", "A", "feed", key_setting="ODDSPAPI_API_KEY"),
    ProviderSpec("sharpapi", "SharpAPI", "A", "feed", vault_id="sharpapi", key_setting="SHARPAPI_API_KEY"),
    ProviderSpec("sportsdata_io", "SportsData.io", "A", "feed", key_setting="SPORTSDATA_IO_KEY", base_url="https://api.sportsdata.io"),
    ProviderSpec("the_rundown", "The Rundown", "A", "feed", key_setting="THE_RUNDOWN_API_KEY"),
    ProviderSpec("sports_api", "API-Sports", "A", "feed", vault_id="api_football"),
    ProviderSpec("all_sports_api", "All Sports API", "A", "feed"),
    ProviderSpec("sportmonks", "Sportmonks", "A", "feed", vault_id="sportmonks", key_setting="SPORTMONKS_API_KEY", base_url="https://api.sportmonks.com/v3"),
    ProviderSpec("soccer_api", "SoccerAPI", "A", "feed", sports=("soccer",)),
    ProviderSpec("livescore_api", "LiveScore API", "A", "feed"),
    ProviderSpec("rapidapi", "RapidAPI Sports Suite", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY"),
    ProviderSpec("sofascore", "SofaScore (RapidAPI)", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY"),
    ProviderSpec("bet365data", "Bet365 data (RapidAPI)", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY"),
    ProviderSpec("cricbuzz", "Cricbuzz (RapidAPI)", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY", sports=("cricket",)),
    ProviderSpec("cricket_live_line", "Cricket Live Line (RapidAPI)", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY", sports=("cricket",)),
    ProviderSpec("cricapi", "CricAPI", "A", "feed", vault_id="cricapi", sports=("cricket",)),
    ProviderSpec("flashlive", "Flashlive Sports (RapidAPI)", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY"),
    ProviderSpec("sports_highlights", "Sports Highlights API (RapidAPI)", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY"),
    ProviderSpec("football_prediction", "Football Prediction API (RapidAPI)", "A", "feed", vault_id="rapidapi", key_setting="RAPIDAPI_KEY", sports=("soccer",)),
    ProviderSpec("espn", "ESPN public site API", "A", "feed", url_setting="WIRE_ESPN_BASE_URL"),
    # ---- A: prediction markets (wisdom of crowds)
    ProviderSpec("polymarket", "Polymarket", "A", "prediction_market", url_setting="POLYMARKET_GAMMA_BASE_URL"),
    ProviderSpec("kalshi", "Kalshi", "A", "prediction_market", url_setting="KALSHI_BASE_URL"),
    ProviderSpec("manifold", "Manifold Markets", "A", "prediction_market", url_setting="MANIFOLD_BASE_URL"),
    ProviderSpec("limitless", "Limitless", "A", "prediction_market"),
    ProviderSpec("predict_fun", "Predict.fun", "A", "prediction_market"),
    ProviderSpec("myriad", "Myriad Markets", "A", "prediction_market"),
    # ---- A: authorised headless harvesters (only where the site's terms allow it)
    ProviderSpec("playwright", "Playwright dynamic scraper", "A", "harvester"),
    ProviderSpec("hyperscraper", "Hyperscraper", "A", "harvester"),
    ProviderSpec("scrapingbee", "ScrapingBee", "A", "harvester"),
    ProviderSpec("ai_web_scraper", "AI Web Scraper", "A", "harvester"),
    ProviderSpec("zenith", "Zenith", "A", "harvester"),
    ProviderSpec("dripcrawler", "DripCrawler", "A", "harvester"),
    # ---- B: weather, geocoding
    ProviderSpec("open_meteo", "Open-Meteo forecast", "B", "weather", url_setting="WIRE_OPEN_METEO_FORECAST_URL"),
    ProviderSpec("open_meteo_geocoding", "Open-Meteo geocoding", "B", "weather", url_setting="WIRE_OPEN_METEO_GEOCODING_URL"),
    # ---- C: FX and crypto
    ProviderSpec("fx_rates", "FX rates (INR, USD, EUR, GBP, USDT)", "C", "fx"),
    # ---- D: travel logistics
    ProviderSpec("flight_status", "Flight status & transit", "D", "travel"),
    # ---- E: news, social, OSINT
    ProviderSpec("newsapi", "NewsAPI", "E", "news", key_setting="NEWSAPI_ORG_API_KEY", url_setting="WIRE_NEWSAPI_URL"),
    ProviderSpec("social_sentiment", "Social sentiment (X, Reddit)", "E", "news"),
    ProviderSpec("osint_injuries", "OSINT early injury intelligence", "E", "news"),
)

PROVIDERS_BY_ID: dict[str, ProviderSpec] = {p.provider_id: p for p in PROVIDERS}
PROVIDER_ALIASES: dict[str, str] = {"odds_api": "the_odds_api", "oddsapi": "the_odds_api", "1xbet": "one_xbet", "onexbet": "one_xbet",
                                    "pinnacle_api": "pinnacle", "sportsdataio": "sportsdata_io", "rundown": "the_rundown"}


def provider(provider_id: str) -> ProviderSpec | None:
    key = provider_id.strip().casefold()
    return PROVIDERS_BY_ID.get(PROVIDER_ALIASES.get(key, key))
