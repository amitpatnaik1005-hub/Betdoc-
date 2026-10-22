"""VIDUR's intelligence board (Group 108, point b.14b): what the Wire knows, one row per item, four columns.

1. **The item**: a stored article's headline and the first two sentences of its own summary, or a measured record
   (a fixture's recorded absences, a forecast that moves the friction factor, a steam move in the odds ticks).
2. **Source and freshness**: the source and its tier, and when it was published or measured:

   * ``TIER_1_OFFICIAL``: a governing body, league or club (``WIRE_OFFICIAL_SOURCE_HOSTS``);
   * ``TIER_2_PRESS``: the press at or over ``WIRE_PRESS_MIN_CREDIBILITY`` (the Wire's own credibility table);
   * ``TIER_3_WIRE``: aggregators, tabloids and unknown hosts;
   * ``MEASURED``: not prose: an injury list, a forecast, the odds ticks, an operator's entry.

3. **Match and market impact**: only what was measured. The fixture's de-vigged consensus when the story landed
   against the consensus now (recorded for CRITICAL and HIGH stories), a catalyst's recorded move, the lineup model's
   delta from rated absences, the forecast's friction factor, a steam move's implied-probability change; and the
   fixture's best leg priced above fair at a retail book now (Ashoka), with its change since the last chain run.
4. **The action**: open the fixture in the Oracle, or run the neural chain on it (with your slip, to audit it).

Categories come from the events the sentiment engine read (``app.domain.the_wire.sentiment_engine``): absences,
doubts and returns are LINEUP_INJURY; weather is WEATHER_PITCH; rotation, sackings and stranded teams are
TACTICAL_ROTATION; a story the market followed (a catalyst) is ODDS_STEAM. A story with none of these is GENERAL.
Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from urllib.parse import urlparse

from app.domain.the_wire.sentiment_engine import Event


class Category(StrEnum):
    LINEUP_INJURY = "LINEUP_INJURY"
    WEATHER_PITCH = "WEATHER_PITCH"
    ODDS_STEAM = "ODDS_STEAM"
    TACTICAL_ROTATION = "TACTICAL_ROTATION"
    GENERAL = "GENERAL"


class Tier(StrEnum):
    TIER_1_OFFICIAL = "TIER_1_OFFICIAL"
    TIER_2_PRESS = "TIER_2_PRESS"
    TIER_3_WIRE = "TIER_3_WIRE"
    MEASURED = "MEASURED"


class Kind(StrEnum):
    NEWS = "NEWS"
    ABSENCES = "ABSENCES"
    WEATHER = "WEATHER"
    STEAM = "STEAM"


IMPACT_RANK: dict[str, int] = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_EVENT_CATEGORY: tuple[tuple[Category, frozenset[str]], ...] = (
    (Category.LINEUP_INJURY, frozenset({Event.ABSENCE, Event.DOUBT, Event.RETURN})),
    (Category.WEATHER_PITCH, frozenset({Event.WEATHER})),
    (Category.TACTICAL_ROTATION, frozenset({Event.ROTATION, Event.SACKING, Event.STRANDED})),
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'‘“])")
_TAGS = re.compile(r"<[^>]+>")


def category_of(events: Iterable[str], *, catalyst: bool) -> Category:
    """A story the market followed is ODDS_STEAM; otherwise the first category its events name."""
    if catalyst:
        return Category.ODDS_STEAM
    found = set(events)
    return next((c for c, wanted in _EVENT_CATEGORY if found & wanted), Category.GENERAL)


def _host(url: str | None) -> str:
    if not url or "://" not in url:
        return ""
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def tier_of(url: str | None, credibility: float, *, official_hosts: Sequence[str], press_min: float) -> Tier:
    host = _host(url)
    if host and any(host == h or host.endswith("." + h) for h in official_hosts):
        return Tier.TIER_1_OFFICIAL
    return Tier.TIER_2_PRESS if credibility >= press_min else Tier.TIER_3_WIRE


def two_sentences(text: str | None, limit: int = 320) -> str:
    """The first two sentences of the source's own summary (markup stripped), cut at ``limit`` characters."""
    clean = " ".join(_TAGS.sub(" ", text or "").split())
    if not clean:
        return ""
    head = " ".join(_SENTENCE.split(clean)[:2])
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"


def sport_label(family: str | None, sports: Mapping[str, Sequence[str]]) -> str | None:
    """The board's sport (CRICKET, FOOTBALL ...) for a feed family ("soccer"), or None when the board has no filter for it."""
    if not family:
        return None
    return next((label for label, families in sports.items() if family in families), None)


def probability_move(before: Mapping[str, float] | None, now: Mapping[str, float] | None, side: str | None) -> dict[str, float | str] | None:
    """The named side's consensus when the story landed against now (percentage points), or None if either is missing."""
    if not before or not now:
        return None
    selection = side if side in before and side in now else None
    if selection is None:
        return None
    b, n = float(before[selection]), float(now[selection])
    return {"selection": selection, "before": round(b, 4), "now": round(n, 4), "delta_pts": round(100.0 * (n - b), 2)}


def edge_shift(now_ev: float | None, before: Mapping[str, object] | None, leg_id: str | None) -> float | None:
    """The change in EV on the same leg since the last chain recorded it (percentage points), else None."""
    if now_ev is None or not before or before.get("leg_id") != leg_id or not isinstance(before.get("ev"), int | float):
        return None
    return round(100.0 * (now_ev - float(before["ev"])), 2)  # type: ignore[arg-type]


def weather_is_news(factor: float | None, dew: bool | None, min_effect: float) -> bool:
    return bool(dew) or (factor is not None and abs(factor - 1.0) >= min_effect)


def order_key(item: Mapping[str, object]) -> tuple[int, int, float]:
    """Items on a tracked fixture first, then by impact, then newest first."""
    impact = item.get("impact_level")
    stamp = item.get("_epoch")
    return (0 if item.get("fixture") else 1, IMPACT_RANK.get(impact if isinstance(impact, str) else "", 2), -(stamp if isinstance(stamp, int | float) else 0.0))


def matches(item: Mapping[str, object], *, sport: str | None, category: str | None, query: str | None, linked_only: bool) -> bool:
    if linked_only and not item.get("fixture"):
        return False
    if sport and sport != "ALL" and item.get("sport") != sport:
        return False
    if category and category != "ALL" and item.get("category") != category:
        return False
    if query:
        q = query.casefold().strip()
        fixture = item.get("fixture")
        names = f"{fixture.get('home', '')} {fixture.get('away', '')}" if isinstance(fixture, Mapping) else ""
        source = item.get("source")
        hay = " ".join(str(x) for x in (item.get("headline", ""), item.get("summary", ""), names, source.get("name", "") if isinstance(source, Mapping) else ""))
        if q and q not in hay.casefold():
            return False
    return True
