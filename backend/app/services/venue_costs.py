"""What each bookmaker costs: commission on net winnings and the currency its account settles in.

A venue row that executes for the bookmaker (its own id, or listed in its routes) sets both; a
bookmaker without one falls back to ``EXCHANGE_COMMISSION_RATES`` / ``BOOKMAKER_CURRENCIES``, and
then to the book's own currency: a regional key's suffix (``_uk`` GBP, ``_nl`` EUR, ``_au`` AUD...),
the UK-licensed books and exchanges in GBP, prediction markets in USD. Only an unknown, unsuffixed
book is assumed to hold rupees. A foreign currency without a live rate is refused (``fx_rates``),
so a wrong default can only hide a price, never mis-price one. The sandbox catch-all never
overrides: it stands in for the real bookmaker, so it must price like it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings
from app.domain.math.arbitrage_calc import HOME_CURRENCY, MAX_COMMISSION

ZERO = Decimal(0)

_REGION_CURRENCY = {
    "uk": "GBP", "ie": "EUR", "eu": "EUR", "nl": "EUR", "fr": "EUR", "it": "EUR", "es": "EUR", "de": "EUR", "be": "EUR", "at": "EUR",
    "se": "SEK", "dk": "DKK", "no": "NOK", "au": "AUD", "us": "USD", "us2": "USD", "ca": "CAD",
}
_KNOWN_CURRENCY = {
    # prediction markets settle in dollars (USDC)
    "polymarket": "USD", "kalshi": "USD",
    # exchanges and UK-licensed books without a regional suffix
    "betfair": "GBP", "matchbook": "GBP", "smarkets": "GBP", "betdaq": "GBP", "williamhill": "GBP", "paddypower": "GBP",
    "skybet": "GBP", "coral": "GBP", "ladbrokes": "GBP", "betfred": "GBP", "betvictor": "GBP", "boylesports": "GBP",
    "virginbet": "GBP", "grosvenor": "GBP", "livescorebet": "GBP", "sport888": "GBP", "casumo": "GBP", "leovegas": "GBP", "betway": "GBP",
}


def default_currency(bookmaker_id: str) -> str:
    known = _KNOWN_CURRENCY.get(bookmaker_id)
    if known:
        return known
    _, _, suffix = bookmaker_id.rpartition("_")
    return _REGION_CURRENCY.get(suffix, HOME_CURRENCY) if suffix != bookmaker_id else HOME_CURRENCY


@dataclass(frozen=True, slots=True)
class BookmakerTerms:
    commission: Decimal  # fraction of net winnings, e.g. 0.05
    currency: str


def _owns(venue: VenueConfig | None, bookmaker_id: str) -> bool:
    return venue is not None and not venue.is_sandbox and (venue.id == bookmaker_id or bookmaker_id in venue.routes)


def bookmaker_terms(bookmaker_id: str, settings: Settings, venue: VenueConfig | None = None) -> BookmakerTerms:
    own = venue if _owns(venue, bookmaker_id) else None
    if own is not None and own.commission_rate is not None:
        commission = Decimal(own.commission_rate)
    else:
        commission = Decimal(str(settings.EXCHANGE_COMMISSION_RATES.get(bookmaker_id, 0)))
    if not ZERO <= commission < MAX_COMMISSION:
        commission = MAX_COMMISSION - Decimal("0.0001")  # a misconfigured rate prices the book as worthless, never as free
    currency = (own.currency if own is not None and own.currency else settings.BOOKMAKER_CURRENCIES.get(bookmaker_id) or default_currency(bookmaker_id)).upper()
    return BookmakerTerms(commission, currency)


class TermsTable:
    """Terms for every bookmaker, resolved once against the current venues."""

    def __init__(self, settings: Settings, venues: Iterable[VenueConfig] = ()) -> None:
        self.settings = settings
        self.venues = tuple(venues)
        self._cache: dict[str, BookmakerTerms] = {}

    def __call__(self, bookmaker_id: str) -> BookmakerTerms:
        terms = self._cache.get(bookmaker_id)
        if terms is None:
            owner = next((v for v in self.venues if _owns(v, bookmaker_id)), None)
            terms = self._cache[bookmaker_id] = bookmaker_terms(bookmaker_id, self.settings, owner)
        return terms
