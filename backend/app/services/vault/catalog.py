"""Names the importer understands: bookmakers, data providers, credential fields and sports (Group 70).

Every lookup normalises first (case, punctuation, markdown, ``_``/``-`` as spaces), then matches an alias
exactly, then fuzzily (``difflib`` ratio >= 0.84 on the whole name, so "Pari match" and "1x-Bet" land
but "Bet" alone does not). Unknown bookmakers are kept under their own slug with the generic adapter.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, GENERIC_ADAPTER_KEY, create_adapter
from app.services.venue_costs import default_currency

FUZZY_CUTOFF = 0.84

EntityKind = Literal["bookmaker", "provider", "sports"]


# A list number in front of a label: "**1.) Odds API Key", "12) Exa API Key", "\*3. Sportradar" (never "1xBet" or "2FA")
_ENUMERATION = re.compile(r"^[\s*_`>\\]*\d{1,3}\s*[.)]+\s*")


def normalise(text: str) -> str:
    """``"**1xBet — Account #2:**"`` -> ``"1xbet account 2"``; ``"**1.) Odds API Key"`` -> ``"odds api key"``."""
    text = _ENUMERATION.sub("", text.replace("&#x20;", " ").replace("&nbsp;", " ").casefold())
    text = re.sub(r"[`*_~>#\[\]()\"'“”‘’]", " ", text)
    text = re.sub(r"[-–—/\\|.,:;=+!?@]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


BOOKMAKER_ALIASES: dict[str, tuple[str, ...]] = {
    "parimatch": ("parimatch", "pari match", "parimatch in", "parimatch com", "pm"),
    "1xbet": ("1xbet", "1x bet", "onexbet", "one x bet", "1xbet com", "1x"),
    "stake": ("stake", "stake com", "stake us", "stake casino", "stake sportsbook"),
    "pinnacle": ("pinnacle", "pinnacle sports", "pinny", "ps3838", "pinnacle com", "pinnaclesports"),
    "betfair": ("betfair", "betfair exchange", "betfair com", "bf exchange"),
    "bet365": ("bet365", "bet 365"),
    "dafabet": ("dafabet",),
    "betway": ("betway",),
    "10cric": ("10cric", "10 cric", "tencric"),
    "22bet": ("22bet", "22 bet"),
    "melbet": ("melbet",),
    "mostbet": ("mostbet",),
    "4rabet": ("4rabet", "4ra bet"),
    "betwinner": ("betwinner", "bet winner"),
    "fun88": ("fun88", "fun 88"),
    "bcgame": ("bc game", "bcgame", "bc game com"),
    "smarkets": ("smarkets",),
    "matchbook": ("matchbook",),
    "betdaq": ("betdaq",),
}
# Brief: rupees for the Indian books, USDT for Stake, sterling for Betfair; any other book its usual one.
DEFAULT_CURRENCY: dict[str, str] = {"parimatch": "INR", "1xbet": "INR", "stake": "USDT", "betfair": "GBP"}

PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "odds_api": ("the odds api", "odds api", "oddsapi", "theoddsapi", "the odds api com", "odds api v4"),
    "pinnacle_api": ("pinnacle api", "pinn api", "pinnacle odds api", "pinnacle data api"),
    "sharpapi": ("sharpapi", "sharp api", "sharp odds api"),
    "api_football": ("api football", "apifootball", "api sports", "api football com"),
    "sportmonks": ("sportmonks", "sport monks"),
    "football_data": ("football data", "football data org", "footballdata"),
    "betsapi": ("betsapi", "bets api"),
    "opticodds": ("opticodds", "optic odds"),
    "oddsjam": ("oddsjam", "odds jam"),
    "sportradar": ("sportradar", "sport radar"),
    "rapidapi": ("rapidapi", "rapid api"),
    "cricapi": ("cricapi", "cric api", "cricketdata", "cricket data"),
}
PROVIDER_NAMES: dict[str, str] = {
    "odds_api": "The Odds API", "pinnacle_api": "Pinnacle API", "sharpapi": "SharpAPI", "api_football": "API-Football",
    "sportmonks": "Sportmonks", "football_data": "football-data.org", "betsapi": "BetsAPI", "opticodds": "OpticOdds",
    "oddsjam": "OddsJam", "sportradar": "Sportradar", "rapidapi": "RapidAPI", "cricapi": "CricAPI",
}
# Provider -> the Fleet Command source that runs on its key (config-driven sources link by their own id)
PROVIDER_FLEET_SOURCE: dict[str, str] = {"odds_api": "odds_api"}

SPORTS_SECTION_ALIASES = ("sports", "sport keys", "active sports", "leagues", "sports and leagues", "sport", "multi sport", "multi sport keys", "competitions")

FieldName = Literal[
    "username", "password", "api_key", "token", "secret", "totp_seed", "notes", "currency", "url", "label", "balance", "stake_cap", "sports",
]
SECRET_FIELDS: tuple[FieldName, ...] = ("username", "password", "api_key", "token", "secret", "totp_seed", "notes", "url")

FIELD_ALIASES: dict[FieldName, tuple[str, ...]] = {
    "username": (
        "user", "username", "user name", "login", "login id", "login name", "user id", "userid", "account id", "account no",
        "account number", "email", "e mail", "mail", "email id", "email address", "phone", "mobile", "phone number", "mobile number",
        "id", "client id", "customer id", "login email",
    ),
    "password": ("password", "pass", "pwd", "passwd", "passcode", "login password", "pw", "account password"),
    "api_key": ("api key", "apikey", "key", "app key", "application key", "x api key", "api access key", "access key", "license key"),
    "token": ("token", "bearer", "bearer token", "access token", "auth token", "session token", "api token", "jwt", "authorization"),
    "secret": ("secret", "api secret", "app secret", "client secret", "secret key", "private key", "shared secret"),
    "totp_seed": (
        "2fa", "2fa secret", "2fa seed", "2fa key", "2fa code", "two factor", "two factor secret", "totp", "totp secret", "totp seed",
        "otp", "otp secret", "otp seed", "authenticator", "authenticator key", "authenticator secret", "google authenticator", "mfa",
        "mfa secret", "seed",
    ),
    "notes": ("notes", "note", "remarks", "remark", "comment", "comments", "info", "details", "memo"),
    "currency": ("currency", "ccy", "curr", "account currency", "wallet currency"),
    "url": (
        "url", "link", "site", "website", "web", "target url", "target", "base url", "endpoint", "api url", "login url", "domain",
        "mirror", "homepage", "address", "api endpoint", "api base", "base",
    ),
    "label": ("label", "name", "account name", "nickname", "alias", "title", "account label"),
    "balance": ("balance", "bankroll", "funds", "current balance", "wallet balance"),
    "stake_cap": ("max stake", "stake cap", "stake limit", "max bet", "bet limit", "max per bet", "limit"),
    "sports": ("sports", "sport keys", "sport", "leagues", "active sports", "sport key", "competitions"),
}
_FIELD_LOOKUP: dict[str, FieldName] = {alias: name for name, aliases in FIELD_ALIASES.items() for alias in aliases}
_BOOK_LOOKUP: dict[str, str] = {alias: book for book, aliases in BOOKMAKER_ALIASES.items() for alias in aliases}
_PROVIDER_LOOKUP: dict[str, str] = {alias: provider for provider, aliases in PROVIDER_ALIASES.items() for alias in aliases}

SPORT_PREFIXES = (
    "soccer", "cricket", "tennis", "basketball", "americanfootball", "icehockey", "baseball", "mma", "boxing",
    "rugbyleague", "rugbyunion", "aussierules", "golf", "lacrosse", "handball", "politics", "volleyball", "esports",
)
SPORT_KEY_RE = re.compile(rf"\b(?:{'|'.join(SPORT_PREFIXES)})_[a-z0-9_]+\b")
SPORT_ALIASES: dict[str, str] = {
    "epl": "soccer_epl", "premier league": "soccer_epl", "english premier league": "soccer_epl",
    "efl championship": "soccer_efl_champ", "championship": "soccer_efl_champ",
    "la liga": "soccer_spain_la_liga", "laliga": "soccer_spain_la_liga", "serie a": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga", "ligue 1": "soccer_france_ligue_one", "ligue one": "soccer_france_ligue_one",
    "eredivisie": "soccer_netherlands_eredivisie", "primeira liga": "soccer_portugal_primeira_liga",
    "champions league": "soccer_uefa_champs_league", "ucl": "soccer_uefa_champs_league", "uefa champions league": "soccer_uefa_champs_league",
    "europa league": "soccer_uefa_europa_league", "uel": "soccer_uefa_europa_league", "mls": "soccer_usa_mls",
    "ipl": "cricket_ipl", "indian premier league": "cricket_ipl", "big bash": "cricket_big_bash", "bbl": "cricket_big_bash",
    "psl": "cricket_psl", "test cricket": "cricket_test_match", "test match": "cricket_test_match", "odi": "cricket_odi",
    "t20i": "cricket_international_t20", "international t20": "cricket_international_t20",
    "nba": "basketball_nba", "euroleague": "basketball_euroleague", "nfl": "americanfootball_nfl", "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb", "ufc": "mma_mixed_martial_arts", "mma": "mma_mixed_martial_arts",
}
# The Odds API names tennis by tournament (tennis_atp_wimbledon, tennis_wta_us_open...): a bare tour matches no feed
AMBIGUOUS_SPORTS: dict[str, str] = {
    "tennis_atp": "The Odds API lists ATP tennis per tournament (tennis_atp_wimbledon, tennis_atp_us_open...)",
    "tennis_wta": "The Odds API lists WTA tennis per tournament (tennis_wta_wimbledon, tennis_wta_us_open...)",
    "atp": "The Odds API lists ATP tennis per tournament (tennis_atp_wimbledon, tennis_atp_us_open...)",
    "wta": "The Odds API lists WTA tennis per tournament (tennis_wta_wimbledon, tennis_wta_us_open...)",
}


@dataclass(frozen=True, slots=True)
class Entity:
    kind: EntityKind
    key: str  # bookmaker id, provider id, or "sports"


def _fuzzy(name: str, lookup: dict[str, str]) -> str | None:
    if not name:
        return None
    exact = lookup.get(name)
    if exact is not None:
        return exact
    match = difflib.get_close_matches(name, [a for a in lookup if len(a) >= 4], n=1, cutoff=FUZZY_CUTOFF)
    return lookup[match[0]] if match else None


def match_field(label: str, *, fuzzy: bool = True) -> FieldName | None:
    """A credential field from its label: ``"API Key #2"`` -> ``api_key``."""
    name = re.sub(r"\s*\d+$", "", normalise(label)).strip()
    name = re.sub(r"^(?:your|my|the|primary|secondary|main|backup|alt|alternate)\s+", "", name)
    if not name:
        return None
    if name in _FIELD_LOOKUP:
        return _FIELD_LOOKUP[name]
    if not fuzzy:
        return None
    match = difflib.get_close_matches(name, [a for a in _FIELD_LOOKUP if len(a) >= 5], n=1, cutoff=0.88)
    return _FIELD_LOOKUP[match[0]] if match else None


def is_bare_name(text: str) -> bool:
    """Is the heading just a bookmaker's name (with "account(s)" / "login")? Else it labels an account."""
    name = re.sub(r"\s*(?:accounts?|acc|acct|logins?|credentials|creds|details)$", "", normalise(text)).strip()
    return name in _BOOK_LOOKUP


def match_entity(text: str) -> Entity | None:
    """A heading or label as a bookmaker, a provider or the sports section. Provider aliases are tried
    first so "Pinnacle API" is the data feed while "Pinnacle" alone is the bookmaker."""
    name = normalise(text)
    name = re.sub(r"\s*(?:account|accounts|acc|acct|login|logins|credentials|creds|details|keys?|id)\s*\d*$", "", name).strip()
    name = re.sub(r"\s*#?\d+$", "", name).strip()
    if not name:
        return None
    if name in SPORTS_SECTION_ALIASES:
        return Entity("sports", "sports")
    provider = _fuzzy(name, _PROVIDER_LOOKUP)
    if provider is not None:
        return Entity("provider", provider)
    book = _fuzzy(name, _BOOK_LOOKUP)
    if book is not None:
        return Entity("bookmaker", book)
    return None


def split_entity_label(label: str) -> tuple[Entity, FieldName | None] | None:
    """``"Odds API Key 2"`` -> (odds_api, api_key); ``"PINNACLE_USERNAME"`` -> (pinnacle, username);
    ``"Parimatch"`` -> (parimatch, None). The longest entity alias that starts the label wins."""
    words = normalise(label).split()
    for cut in range(len(words), 0, -1):
        head, tail = " ".join(words[:cut]), " ".join(words[cut:])
        entity = (
            Entity("provider", _PROVIDER_LOOKUP[head]) if head in _PROVIDER_LOOKUP
            else Entity("bookmaker", _BOOK_LOOKUP[head]) if head in _BOOK_LOOKUP
            else None
        )
        if entity is None:
            continue
        if not tail:
            return entity, None
        field = match_field(tail)
        if field is not None:
            return entity, field
        if entity.kind == "bookmaker" and tail in ("api", "api key"):  # "Betfair API": the app key
            return entity, "api_key"
    return None


_TARGET_LABEL = re.compile(r"^(?:target|bookmaker|book|site)\s+(?:url|link|site|address)\s+(?P<name>.+)$|^(?P<pre>.+?)\s+target\s+(?:url|link)$")
# " API Key" after a space, else a bare " Key": "SerpAPI Key" -> "SerpAPI", "Sports-API Key" -> "Sports-API"
_GENERIC_KEY_SUFFIX = re.compile(r"\s+(?:api[\s_-]*key|api[\s_-]*token|access[\s_-]*key|key)\s*$", re.I)


def target_url_name(label: str) -> str | None:
    """``"**5.) Target URL (Parimatch)"`` -> ``"parimatch"``: what a target-URL label points at (normalised)."""
    match = _TARGET_LABEL.match(normalise(label))
    if match is None:
        return None
    return (match.group("name") or match.group("pre") or "").strip() or None


def display_label(label: str) -> str:
    """A label as the user wrote it, minus markdown, escapes and list numbering: ``"**8.) Oddspapi.io API Key"``."""
    text = _ENUMERATION.sub("", label.replace("&#x20;", " ").replace("&nbsp;", " "))
    text = re.sub(r"[`*_~>#\[\]\"“”‘’\\]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" :-")


@dataclass(frozen=True, slots=True)
class GenericProvider:
    provider_id: str  # a known provider's id, or a slug of the name ("newsapi_org")
    name: str  # as written: "NewsAPI.org"
    known: bool


def generic_provider(label: str) -> GenericProvider | None:
    """``"<Name> API Key"`` for any name: a known provider when the name is one (fuzzily), else a generic
    provider under a slug of the name, so no key in the file is ever dropped for want of an alias.
    ``None`` when the label has no name before "API key", or the name is a credential field itself."""
    written = display_label(label)
    if not _GENERIC_KEY_SUFFIX.search(written):
        return None
    name = _GENERIC_KEY_SUFFIX.sub("", written).strip(" ()-_.")
    plain = normalise(name)
    if not plain or plain in ("api", "apis", "the api", "my api") or not re.search(r"[a-z]", plain) or len(name) > 64 or plain in _FIELD_LOOKUP or match_field(name) is not None:
        return None
    entity = match_entity(name)
    if entity is not None and entity.kind == "provider":
        return GenericProvider(entity.key, provider_display(entity.key), True)
    if entity is not None:  # "Stake API Key": a bookmaker's own key, not a data provider
        return None
    provider_id = re.sub(r"[^a-z0-9]+", "_", plain).strip("_")[:64]
    return GenericProvider(provider_id, name[:128], False) if provider_id else None


def is_known_provider(provider_id: str) -> bool:
    return provider_id in PROVIDER_ALIASES


def sport_keys_in(text: str, *, friendly: bool) -> tuple[list[str], list[str]]:
    """(sport keys, ambiguity notes) in a line. ``friendly``: also "EPL", "IPL"... (only inside a sports section)."""
    lowered = text.casefold()
    keys = list(dict.fromkeys(SPORT_KEY_RE.findall(lowered)))
    notes: list[str] = []
    for key in list(keys):
        if key in AMBIGUOUS_SPORTS:
            keys.remove(key)
            notes.append(f"{key}: {AMBIGUOUS_SPORTS[key]}")
    if friendly:
        for raw in re.split(r"[,;|•·\n/]+|\s+-\s+|\band\b|\s+&\s+", re.sub(r"\b[a-z]+(?:_[a-z0-9]+)+\b", " ", lowered)):
            part = re.sub(r"^(?:the|all)\s+", "", normalise(raw))
            if part in SPORT_ALIASES and SPORT_ALIASES[part] not in keys:
                keys.append(SPORT_ALIASES[part])
            elif part in AMBIGUOUS_SPORTS:
                notes.append(f"{part.upper()}: {AMBIGUOUS_SPORTS[part]}")
    return keys, notes


def bookmaker_display(book: str) -> str:
    if book in ASHOKA_BOOKMAKERS:
        return ASHOKA_BOOKMAKERS[book]
    for alias in BOOKMAKER_ALIASES.get(book, ()):
        return alias.title().replace(" ", "")
    return book


def provider_display(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider.replace("_", " ").title())


def adapter_key(book: str) -> str:
    """The ``app.domain.bookmakers.adapters`` class that renders this book's placement instructions."""
    adapter = create_adapter(bookmaker_display(book))
    name = type(adapter).__name__
    return GENERIC_ADAPTER_KEY if name == "GenericAdapter" else name


def currency_for(book: str) -> str:
    return DEFAULT_CURRENCY.get(book) or default_currency(book)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalise(text))[:32] or "other"


def known_bookmakers() -> Iterable[str]:
    return BOOKMAKER_ALIASES.keys()
