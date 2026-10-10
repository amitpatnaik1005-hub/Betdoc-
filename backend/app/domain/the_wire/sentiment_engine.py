"""Sports news, read for the market (Group 78): polarity, tactical impact, source credibility, the teams named.

Polarity, from the named side's point of view:

    S = clamp( sum polarity(w) x weight(w) / sqrt(N_words), -1, +1 )

Phrases are matched before single words and consume them ("ruled out", "torn ACL" count once each), and a
negator up to three words before a term flips it ("no injury concerns" reads positive).

Tactical impact:

* CRITICAL: an absence ("ruled out", "out for the season", "suspended") or a sacking, or a stranded team, when a
  fixture of a named side kicks off within ``WIRE_CRITICAL_WINDOW_HOURS``;
* HIGH: the same further out; a doubt ("doubtful", "fitness test"), rotation, or severe weather;
* MEDIUM: no such event but a strong polarity (``|S| > WIRE_SENTIMENT_MEDIUM``); otherwise LOW.

Credibility is the source's tier (``WIRE_SOURCE_CREDIBILITY`` by host): BBC and Reuters 1.0, Sky, ESPN and The
Athletic 0.9, the Guardian 0.85, the major continental dailies 0.75, tabloids and unknown aggregators 0.4.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlparse

from app.domain.the_wire.venue_geocoder import normalize


class Impact(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Event(StrEnum):
    ABSENCE = "ABSENCE"
    SACKING = "SACKING"
    STRANDED = "STRANDED"
    DOUBT = "DOUBT"
    ROTATION = "ROTATION"
    WEATHER = "WEATHER"
    RETURN = "RETURN"


PHRASES: dict[str, float] = {
    "ruled out": -0.95, "out for the season": -1.0, "torn acl": -1.0, "will miss": -0.8, "set to miss": -0.75, "red card": -0.6,
    "fitness test": -0.5, "race against time": -0.6, "game time decision": -0.4, "parted company": -0.8, "travel chaos": -0.7,
    "returns to training": 1.0, "back in training": 0.9, "passed fit": 1.0, "fit to play": 0.9, "available again": 0.8, "clean sheet": 0.6,
}
WORDS: dict[str, float] = {
    "fit": 0.8, "returns": 0.9, "return": 0.7, "starting": 0.7, "dominant": 0.8, "unbeaten": 0.85, "recovered": 0.85, "boost": 0.7,
    "available": 0.6, "cleared": 0.7, "signed": 0.6, "contract": 0.5, "promoted": 0.75, "masterclass": 0.95, "victory": 0.5, "win": 0.4, "wins": 0.4,
    "acl": -1.0, "injury": -0.85, "injured": -0.85, "hamstring": -0.8, "sacked": -0.9, "suspended": -0.85, "suspension": -0.8, "banned": -0.85,
    "crisis": -0.9, "doubt": -0.6, "doubtful": -0.7, "benched": -0.5, "unplayable": -0.9, "investigation": -0.7, "outbreak": -0.85,
    "sidelined": -0.9, "setback": -0.7, "knock": -0.5, "illness": -0.6, "defeat": -0.4, "stranded": -0.8, "postponed": -0.5, "waterlogged": -0.6,
}
NEGATORS = frozenset({"not", "no", "never", "without", "isnt", "isn't", "wont", "won't", "nor"})
EVENT_TERMS: dict[Event, tuple[str, ...]] = {
    Event.ABSENCE: ("ruled out", "out for the season", "torn acl", "will miss", "set to miss", "sidelined", "suspended", "banned", "red card"),
    Event.SACKING: ("sacked", "parted company", "fired", "dismissed"),
    Event.STRANDED: ("stranded", "travel chaos"),
    Event.DOUBT: ("doubtful", "doubt", "fitness test", "race against time", "hamstring", "knock", "game time decision", "questionable"),
    Event.ROTATION: ("rotation", "rotate", "rested", "rest players"),
    Event.WEATHER: ("heavy rain", "waterlogged", "postponed", "storm", "snow", "blizzard", "pitch inspection", "heatwave"),
    Event.RETURN: ("returns to training", "back in training", "passed fit", "fit to play", "available again", "recovered"),
}
_WORD = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True, slots=True)
class Mention:
    fixture_id: str
    team: str
    side: str  # HOME / AWAY
    kickoff: datetime | None


@dataclass(frozen=True, slots=True)
class Reading:
    sentiment: float
    impact: Impact
    events: tuple[Event, ...]
    mentions: tuple[Mention, ...]
    subject: Mention | None  # the side named first: the polarity is about it

    def as_dict(self) -> dict[str, object]:
        return {"sentiment": self.sentiment, "impact": self.impact.value, "events": [e.value for e in self.events],
                "mentions": [{"fixture_id": m.fixture_id, "team": m.team, "side": m.side} for m in self.mentions],
                "subject": None if self.subject is None else {"fixture_id": self.subject.fixture_id, "team": self.subject.team, "side": self.subject.side}}


_PHRASES = sorted(((p.split(), w) for p, w in PHRASES.items()), key=lambda pw: -len(pw[0]))  # longest first


def tokens(text: str) -> list[str]:
    """Lower-case words; "Saka's" -> "saka", "isn't" -> "isnt", "game-time" -> "game", "time"."""
    out = []
    for t in _WORD.findall(text.casefold().replace("-", " ")):
        t = (t[:-2] if t.endswith("'s") else t).replace("'", "")
        if t:
            out.append(t)
    return out


def polarity(text: str) -> float:
    words = tokens(text)
    if not words:
        return 0.0
    total, i = 0.0, 0
    while i < len(words):
        weight, width = None, 1
        for parts, w in _PHRASES:
            if words[i:i + len(parts)] == parts:
                weight, width = w, len(parts)
                break
        if weight is None:
            weight = WORDS.get(words[i])
        if weight is not None:
            if any(w in NEGATORS for w in words[max(0, i - 3):i]):
                weight = -weight
            total += weight
        i += width
    return max(-1.0, min(1.0, total / math.sqrt(len(words))))


def events(text: str) -> tuple[Event, ...]:
    padded = f" {' '.join(tokens(text))} "
    return tuple(e for e, terms in EVENT_TERMS.items() if any(f" {t} " in padded for t in terms))


def credibility(url: str | None, source: str | None, table: Mapping[str, float], default: float) -> float:
    """The source's tier, by the article's host (or the feed's), longest matching suffix first."""
    for candidate in (url, source):
        host = (urlparse(candidate).hostname or "") if candidate and "://" in candidate else ""
        host = host.casefold().removeprefix("www.")
        for suffix in sorted(table, key=len, reverse=True):
            if host == suffix or host.endswith("." + suffix):
                return float(table[suffix])
    return default


def mentions(text: str, fixtures: Sequence[tuple[str, str, str, datetime | None]]) -> tuple[Mention, ...]:
    """Fixtures ``(id, home, away, kickoff)`` whose sides the text names (whole words, club suffixes ignored), in order of appearance."""
    padded = f" {normalize(text)} "
    found: list[tuple[int, Mention]] = []
    for fixture_id, home, away, kickoff in fixtures:
        for side, team in (("HOME", home), ("AWAY", away)):
            names = {normalize(team)}
            short = " ".join(w for w in normalize(team).split() if w not in ("united", "city", "town", "hotspur", "albion", "wanderers"))
            if len(short) >= 4:
                names.add(short)
            at = min((padded.find(f" {n} ") for n in names if n and f" {n} " in padded), default=-1)
            if at >= 0:
                found.append((at, Mention(fixture_id, team, side, kickoff)))
    found.sort(key=lambda m: m[0])
    return tuple(m for _, m in found)


def classify(sentiment: float, found: tuple[Event, ...], named: tuple[Mention, ...], now: datetime, critical_window: timedelta, medium: float) -> Impact:
    near = any(m.kickoff is not None and timedelta(0) <= m.kickoff - now <= critical_window for m in named)
    severe = {Event.ABSENCE, Event.SACKING, Event.STRANDED} & set(found)
    if severe:
        return Impact.CRITICAL if near else Impact.HIGH
    if {Event.DOUBT, Event.ROTATION, Event.WEATHER} & set(found):
        return Impact.HIGH
    return Impact.MEDIUM if abs(sentiment) > medium else Impact.LOW


def read(title: str, summary: str, fixtures: Sequence[tuple[str, str, str, datetime | None]], now: datetime, *, critical_window: timedelta, medium: float) -> Reading:
    text = f"{title}. {summary}"
    score = round(polarity(text), 3)
    found = events(text)
    named = mentions(f"{title} {summary}", fixtures)
    subject = mentions(title, fixtures)
    return Reading(score, classify(score, found, named, now, critical_window, medium), found, named, subject[0] if subject else (named[0] if named else None))
