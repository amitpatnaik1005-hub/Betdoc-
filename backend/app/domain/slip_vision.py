"""Reading a bookmaker's bet slip (Group 126): pure functions from OCR boxes or pasted text to legs Ashoka can price.
Developed for Amit Ashok Kumar Patnaik.

* ``rows``: OCR boxes into lines as a person reads them. Boxes that share a row (a selection on the left, its price on
  the right) are joined left to right; rows run top to bottom.
* ``parse``: lines into legs. A fixture line ("Arsenal - Chelsea", "Real Madrid v Girona", "A vs B") opens a leg; the
  lines under it are its market and selection; a price closes it. The summary (total odds, stake, potential return, the
  booking code, the bet type, the book) is read from labelled lines only: an unlabelled capitalised word is never taken
  for a booking code. Hyphenated names (Auger-Aliassime) are not separators; "Both teams to score - Yes" is a market,
  not a fixture; a line number (Over 2.5, Handicap (-1)) is never taken for a price.
* ``interpret``: the bookmaker's words (1X2, W1, X, W2, Winner, To win, Total Over (2.5), TO(2.5), BTTS Yes, Double
  chance 1X, Draw no bet, Handicap (-0.5), AH1) as Ashoka's market and selection on the board's own fixture. A market
  Ashoka does not price (player props, corners, cards, halves, team totals, European handicaps) is None: not covered.
* ``team_similarity``: the Wire's name match, plus the abbreviations slips use (Man City, Man Utd, R. Madrid, Wolves):
  every word of the shorter name must open a different word of the longer one, so "Man City" is Manchester City and
  never Manchester United.

Nothing is invented: a line that cannot be read stays unread and is reported.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.oracle.markets import MarketKind, MarketRef
from app.domain.the_wire.espn_parse import name_similarity, normalize

MIN_ODDS, MAX_ODDS = Decimal("1.01"), Decimal("1000")
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", " ": " ", "×": "x", "＠": "@"})
_LEAD = re.compile(r"^(?:#?\d{1,2}[.)]\s+|[•·*●]\s*)")
_DATETIME = re.compile(r"\s*\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?,?\s+\d{1,2}:\d{2}(?:\s*(?:am|pm))?\s*$", re.IGNORECASE)
_TIME = re.compile(r"\s*(?:\b\d{1,2}:\d{2}\b(?:\s*(?:am|pm))?|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\btoday\b|\btomorrow\b)\s*$", re.IGNORECASE)  # never d.dd: that is a price
_FIXTURE = re.compile(r"^(?P<home>.+?)\s+(?:vs?\.?|-)\s+(?P<away>.+)$", re.IGNORECASE)
_AT = re.compile(r"(?:^|\s)@\s*(\d{1,4}[.,]\d{1,3})\s*$")
_ONLY = re.compile(r"^@?\s*(\d{1,4}[.,]\d{1,3})$")
_TRAIL2 = re.compile(r"\s(\d{1,4}[.,]\d{2,3})\s*$")
_TRAIL1 = re.compile(r"\s(\d{1,4}[.,]\d)\s*$")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_LINE_WORDS = re.compile(r"(?:over|under|total|handicap|\bo|\bu|\bto|\btu|[+-])\s*\(?\s*$", re.IGNORECASE)
_MONEY = r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:₹|inr|rs\.?)?"
_TOTAL_ODDS = re.compile(r"^(?:total|overall|combined|accumulator)\s+(?:odds|coefficient|coef\.?)\s*[:=x@]?\s*(\d{1,6}[.,]\d{1,3})\b", re.IGNORECASE)
_STAKE = re.compile(rf"^(?:total\s+)?(?:stake|bet amount|amount)\s*[:=]?\s*{_MONEY}$", re.IGNORECASE)
_RETURN = re.compile(rf"^(?:potential|possible|max(?:imum)?|estimated|est\.?|to)\s+(?:return|win|winnings|payout|payment)s?\s*[:=]?\s*{_MONEY}$", re.IGNORECASE)
_BOOKING = re.compile(r"\b(?:booking|coupon|share|load)\s*code\b\s*[:#=]?\s*([A-Za-z0-9-]{4,16})\b|^code\s*[:=]\s*([A-Za-z0-9-]{4,16})$", re.IGNORECASE)
_REFERENCE = re.compile(r"\b(?:bet|ticket|slip|coupon)\s*(?:id|no\.?|number|#)\s*[:#=]?\s*[A-Za-z0-9-]{3,}", re.IGNORECASE)  # the placed bet's id: not a code to load
_KIND = re.compile(r"^(single|double|treble|trixie|patent|yankee|canadian|heinz|super heinz|goliath|accumulator|express|multiple|multi|parlay|combo|system)"
                   r"(?:\s*\(\d{1,2}\))?(?:\s+\d+/\d+)?$", re.IGNORECASE)
_KINDS = {"single": "SINGLE", "double": "DOUBLE", "treble": "TREBLE", "trixie": "TRIXIE", "patent": "PATENT", "yankee": "YANKEE", "canadian": "CANADIAN",
          "heinz": "HEINZ", "super heinz": "SUPER_HEINZ", "goliath": "GOLIATH", "accumulator": "ACCUMULATOR", "express": "ACCUMULATOR", "multiple": "ACCUMULATOR",
          "multi": "ACCUMULATOR", "parlay": "ACCUMULATOR", "combo": "ACCUMULATOR", "system": "SYSTEM"}
_NOISE = re.compile(r"^(?:bet\s?slip|my bets|place (?:a )?bet|accept (?:all )?(?:odds )?changes?|balance\b.*|clear(?: all)?|remove|share|cash ?out\b.*|live|"
                    r"login|log in|deposit|bonus.*|odds changed.*|keep|save|print|continue|done|close|back|menu|settings|bet placed.*|bet accepted.*)$",
                    re.IGNORECASE)
_MARKET_WORDS = ("total", "both teams", "btts", "handicap", "winner", "double chance", "draw no bet", "1x2", "result", "to win", "over", "under", "odds",
                 "stake", "return", "booking", "code", "goals", "points", "corners", "cards", "half", "set ", "map ", "period", "quarter", "innings", "runs")
_SELECTION_WORDS = {"yes", "no", "over", "under", "draw", "1", "x", "2", "1x", "x2", "12", "w1", "w2"}
_BOOKS = (("parimatch", "parimatch"), ("1xbet", "1xbet"), ("1x bet", "1xbet"), ("stake.com", "stake"))
_ALIASES = {"utd": "united", "man": "manchester", "spurs": "tottenham hotspur", "wolves": "wolverhampton wanderers", "psg": "paris saint germain",
            "inter": "internazionale", "atl": "atletico", "atl.": "atletico", "st": "saint", "st.": "saint", "dep": "deportivo", "sp": "sporting"}


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


# ================================================================ OCR boxes into reading lines
@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    confidence: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "confidence": round(self.confidence, 3)}


def rows(boxes: Iterable[tuple[Sequence[Sequence[float]], str, float]]) -> list[OcrLine]:
    """``(polygon, text, score)`` boxes into lines: a box joins a row when it overlaps the row's band by half its height."""
    tokens = []
    for poly, text, score in boxes:
        xs, ys = [float(p[0]) for p in poly], [float(p[1]) for p in poly]
        if text and text.strip():
            tokens.append((min(ys), max(ys), min(xs), max(xs), text.strip(), float(score)))
    tokens.sort(key=lambda t: ((t[0] + t[1]) / 2, t[2]))
    bands: list[list[tuple[float, float, float, float, str, float]]] = []
    for token in tokens:
        top, bottom = token[0], token[1]
        for band in bands:
            b_top, b_bottom = min(t[0] for t in band), max(t[1] for t in band)
            overlap = min(bottom, b_bottom) - max(top, b_top)
            if overlap >= 0.5 * min(bottom - top, b_bottom - b_top):
                band.append(token)
                break
        else:
            bands.append([token])
    out = []
    for band in sorted(bands, key=lambda b: min(t[0] for t in b)):
        band.sort(key=lambda t: t[2])
        parts, prev_right = [], None
        for top, bottom, left, right, text, _ in band:
            gap = 0.0 if prev_right is None else left - prev_right
            parts.append(("  " if gap > (bottom - top) else " ") + text if parts else text)
            prev_right = right
        out.append(OcrLine("".join(parts), min(t[5] for t in band)))
    return out


def text_lines(text: str) -> list[OcrLine]:
    return [OcrLine(line) for line in text.splitlines() if line.strip()]


# ================================================================ the slip
@dataclass(slots=True)
class ParsedLeg:
    number: int
    home: str | None = None
    away: str | None = None
    texts: list[str] = field(default_factory=list)
    odds: Decimal | None = None
    confidence: float = 1.0
    lines: list[str] = field(default_factory=list)

    @property
    def fixture(self) -> str | None:
        return None if self.home is None or self.away is None else f"{self.home} vs {self.away}"

    @property
    def market_text(self) -> str:
        return split_market(self.texts)[0]

    @property
    def selection_text(self) -> str:
        return split_market(self.texts)[1]

    def as_dict(self) -> dict[str, Any]:
        return {"number": self.number, "fixture": self.fixture, "home": self.home, "away": self.away, "market_text": self.market_text,
                "selection_text": self.selection_text, "odds": None if self.odds is None else str(self.odds), "confidence": round(self.confidence, 3),
                "lines": self.lines}


@dataclass(slots=True)
class ParsedSlip:
    legs: list[ParsedLeg]
    book: str | None = None
    kind: str | None = None
    stake: Decimal | None = None
    total_odds: Decimal | None = None
    potential_return: Decimal | None = None
    booking_code: str | None = None
    unread: list[str] = field(default_factory=list)  # lines that were neither summary, noise nor part of a leg

    @property
    def product_odds(self) -> Decimal | None:
        prices = [leg.odds for leg in self.legs]
        if not prices or any(p is None for p in prices):
            return None
        return math.prod(prices, start=Decimal(1)).quantize(Decimal("0.01"))  # type: ignore[arg-type]

    @property
    def odds_check(self) -> str | None:
        """MATCH when the legs' prices multiply to the printed total (within 1.5%: books round each step), else MISMATCH:
        a leg was missed or misread. None when there is nothing to compare."""
        product = self.product_odds
        if product is None or self.total_odds is None or self.total_odds <= 0:
            return None
        return "MATCH" if abs(product - self.total_odds) / self.total_odds <= Decimal("0.015") else "MISMATCH"

    def as_dict(self) -> dict[str, Any]:
        return {"legs": [leg.as_dict() for leg in self.legs], "book": self.book, "kind": self.kind,
                "stake": None if self.stake is None else str(self.stake), "total_odds": None if self.total_odds is None else str(self.total_odds),
                "product_odds": None if self.product_odds is None else str(self.product_odds), "odds_check": self.odds_check,
                "potential_return": None if self.potential_return is None else str(self.potential_return), "booking_code": self.booking_code,
                "unread": self.unread}


def clean(line: str) -> str:
    """The line with dashes unified, its lead marker and trailing date dropped; a column gap (two spaces or more, a tab)
    is kept as two spaces, the cue that splits a one-line leg."""
    parts = (" ".join(part.split()) for part in re.split(r"\s{2,}|\t", line.translate(_DASHES).strip()))
    text = "  ".join(part for part in parts if part)
    return _DATETIME.sub("", _LEAD.sub("", text)).strip()


def split_odds(line: str) -> tuple[Decimal | None, str]:
    """(the price on the line, the rest of the line). A number that belongs to the market (Over 2.5, (-1.5)) is not a price."""
    for pattern in (_ONLY, _AT):
        if m := pattern.search(line):
            odds = _decimal(m.group(1))
            if odds is not None and MIN_ODDS <= odds <= MAX_ODDS:
                return odds, line[: m.start()].strip()
            return None, line
    numbers = _NUMBER.findall(line)
    for pattern, needs_two in ((_TRAIL2, False), (_TRAIL1, True)):
        m = pattern.search(line)
        if m is None or (needs_two and len(numbers) < 2):
            continue
        head = line[: m.start()]
        if head.rstrip().endswith("(") or (len(numbers) < 2 and _LINE_WORDS.search(head)):
            continue  # "Total Over (2.50": the line of the market
        odds = _decimal(m.group(1))
        if odds is not None and MIN_ODDS <= odds <= MAX_ODDS:
            return odds, head.strip()
    return None, line


def fixture_of(line: str) -> tuple[str, str] | None:
    """(home, away) when the line names a fixture, else None."""
    text = _TIME.sub("", line).strip()
    m = _FIXTURE.match(text)
    if m is None:
        return None
    home, away = m.group("home").strip(" :,"), m.group("away").strip(" :,")
    low_home, low_away = home.casefold(), away.casefold()
    if any(w in low_home for w in _MARKET_WORDS) or low_away in _SELECTION_WORDS or low_home in _SELECTION_WORDS:
        return None
    if not (re.search(r"[A-Za-z]", home) and re.search(r"[A-Za-z]", away)) or not (2 <= len(home) <= 60 and 2 <= len(away) <= 60):
        return None
    if re.fullmatch(r"[\d\s.,:()+-]+", home) or re.fullmatch(r"[\d\s.,:()+-]+", away):
        return None
    return home, away


_MARKET_START = re.compile(
    r"\b(?:match result|match winner|full[- ]time result|result|1x2|winner|to win|both teams to score|btts|totals?|over|under|asian handicap|handicap|"
    r"double chance|draw no bet|dnb|money ?line|correct score|corners?|cards?|(?:1st|2nd|first|second) half)\b", re.IGNORECASE)


def fixture_split(text: str) -> tuple[str, str, str] | None:
    """(home, away, the rest of the line) when the line opens with a fixture. A whole leg on one line is split at a
    column gap ("Arsenal v Chelsea  1X2: Arsenal"), a separator after a v/vs fixture ("Arsenal v Chelsea - Draw No Bet:
    Chelsea", "Liverpool vs Spurs, BTTS No"), or where the away name runs into the market's own words ("Brighton vs
    Wolves Match Result: Brighton")."""
    segments = [s.strip() for s in re.split(r"\s{2,}|\s+[|\u00b7\u2022]\s+|,\s+", text) if s.strip()]
    if len(segments) > 1 and (teams := fixture_of(segments[0])) is not None:
        return teams[0], teams[1], " ".join(" ".join(segments[1:]).split())
    flat = " ".join(text.split())
    if (m := re.match(r"^(?P<fx>.+?\s+vs?\.?\s+.+?)\s+-\s+(?P<rest>.+)$", flat, re.IGNORECASE)) and (teams := fixture_of(m["fx"])) is not None:
        return teams[0], teams[1], m["rest"]
    teams = fixture_of(flat)
    if teams is None:
        return None
    home, away = teams
    cut = _MARKET_START.search(away)
    if cut is not None and cut.start() > 0 and (name := away[: cut.start()].strip(" :,-")):
        return home, name, away[cut.start():].strip()
    return home, away, ""


def split_market(texts: Sequence[str]) -> tuple[str, str]:
    """(market, selection) from a leg's lines: "1X2: Arsenal", or "Winner" then "Alcaraz", or one line for both."""
    parts = [t.strip() for t in texts if t.strip()]
    if not parts:
        return "", ""
    first = parts[0]
    if ":" in first:
        market, _, rest = first.partition(":")
        return market.strip(), " ".join([rest.strip(), *parts[1:]]).strip()
    if len(parts) >= 2:
        return first, " ".join(parts[1:])
    return "", first


def parse(lines: Sequence[OcrLine]) -> ParsedSlip:
    slip = ParsedSlip(legs=[])
    current: ParsedLeg | None = None

    def close() -> None:
        nonlocal current
        if current is not None:
            if current.odds is None and current.home is None:
                slip.unread.extend(current.lines)  # neither a fixture nor a price: a heading or a misread line, not a leg
            else:
                slip.legs.append(current)
        current = None

    def leg() -> ParsedLeg:
        nonlocal current
        if current is None:
            current = ParsedLeg(number=len(slip.legs) + 1)
        return current

    for raw in lines:
        gapped = clean(raw.text)
        line = " ".join(gapped.split())  # the summary lines and the noise read single-spaced; a leg's line keeps its gaps
        if not line:
            continue
        low = line.casefold()
        for word, book in _BOOKS:
            if word in low:
                slip.book = slip.book or book
        if m := _BOOKING.search(line):
            code = (m.group(1) or m.group(2)).upper()
            if re.search(r"\d", code) or len(code) >= 6:
                slip.booking_code = slip.booking_code or code
            continue
        if _REFERENCE.search(line):
            continue
        if m := _TOTAL_ODDS.match(line):
            slip.total_odds = _decimal(m.group(1))
            continue
        if m := _STAKE.match(line):
            slip.stake = _decimal(m.group(1).replace(",", ""))
            continue
        if m := _RETURN.match(line):
            slip.potential_return = _decimal(m.group(1).replace(",", ""))
            continue
        if m := _KIND.match(line):
            slip.kind = slip.kind or _KINDS[m.group(1).casefold()]
            continue
        bare = " ".join(re.sub("|".join(re.escape(word) for word, _ in _BOOKS), " ", low).split())  # "1xBet Bet slip" is the header
        if not bare or _NOISE.match(line) or _NOISE.match(bare) or _TIME.fullmatch(line):
            continue
        odds, rest = split_odds(gapped)
        opened = fixture_split(rest) if rest else None
        rest = " ".join(rest.split())
        if opened is not None:
            close()
            current = ParsedLeg(number=len(slip.legs) + 1, home=opened[0], away=opened[1], confidence=raw.confidence, lines=[line])
            if opened[2]:
                current.texts.append(opened[2])
            if odds is not None:
                current.odds = odds
                if opened[2]:
                    close()  # the whole leg on one line; a price on a bare fixture line waits for its market below
            continue
        if not rest and odds is None:
            continue
        if odds is not None and current is not None and current.odds is not None:
            close()  # a second price: the next leg (its fixture was not read)
        target = leg()
        target.lines.append(line)
        target.confidence = min(target.confidence, raw.confidence)
        if rest:
            target.texts.append(rest)
        if odds is not None:
            target.odds = odds
            close()
    close()
    for i, item in enumerate(slip.legs, 1):
        item.number = i
    return slip


# ================================================================ team names
def _expand(name: str) -> list[str]:
    words = []
    for word in normalize(name).split():
        words.extend(_ALIASES.get(word, word).split())
    return words


def team_similarity(a: str, b: str) -> float:
    """The Wire's match, else the abbreviation match (0.9): each word of the shorter name opens a different word of the
    longer one (an initial matches a word's first letter), and at least one word is whole or four letters long."""
    exact = name_similarity(a, b)
    if exact >= 0.85:
        return exact
    x, y = _expand(a), _expand(b)
    if not x or not y:
        return exact
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    free = list(long_)
    strong = False
    for word in short:
        hit = next((w for w in free if w.startswith(word) and (len(word) >= 3 or (len(word) == 1 and len(short) > 1))), None)
        if hit is None:
            return exact
        free.remove(hit)
        strong = strong or word == hit or len(word) >= 4
    return 0.9 if strong else exact


# ================================================================ the bookmaker's words as Ashoka's market
@dataclass(frozen=True, slots=True)
class Interpretation:
    ref: MarketRef
    selection: str

    @property
    def market(self) -> str:
        return self.ref.key


_PART_OF_MATCH = re.compile(r"\b(?:1st|2nd|first|second|half|halves|ht|period|quarter|set|map|inning|innings|over\s+\d+\s+runs|corners?|cards?|bookings?|"
                            r"scorer|goalscorer|player|assists?|rebounds?|wickets?|sixes|fours|boundar(?:y|ies)|aces?|correct score|exact|odd/even|"
                            r"team total|individual total|clean sheet|win to nil|both halves|minute|race to)\b|\b\d+\s*-\s*\d+\b", re.IGNORECASE)
_TOTALS_RE = [
    re.compile(r"\b(?:total\s+)?(over|under)\s*\(?\s*(\d+(?:[.,]\d+)?)\s*\)?", re.IGNORECASE),
    re.compile(r"\bt\s?(o|u)\s*\(\s*(\d+(?:[.,]\d+)?)\s*\)", re.IGNORECASE),
    re.compile(r"\btotal\s*\(?\s*(\d+(?:[.,]\d+)?)\s*\)?\s*(over|under)\b", re.IGNORECASE),
]
_HANDICAP_WORD = re.compile(r"\b(?:asian\s+)?handicap\b|\bhcp\b|\bspread\b|\bah\s*[12]?\b|\bh[12]\b", re.IGNORECASE)
_HANDICAP_CODE = re.compile(r"\b(?:ah|h|handicap|hcp)\s*([12])\b", re.IGNORECASE)
_HANDICAP_LINE = re.compile(r"\(\s*([+-]?\d+(?:[.,]\d+)?)\s*\)|(?:^|\s)([+-]\d+(?:[.,]\d+)?)(?=\s|$)")


def _side(text: str, home: str, away: str, cutoff: float, *, swapped: bool) -> str | None:
    """HOME / AWAY / DRAW (the board's sides) for a selection written as a code or a team name. Codes (1, W1, 2, W2) are
    the slip's own order: on a slip that lists the teams the other way round, 1 is the board's away side."""
    token = re.sub(r"\s+", " ", text.strip().casefold()).strip(" .:")
    token = re.sub(r"\b(?:to win|win|wins|winner|match winner|moneyline|full time result|ft|1x2|result)\b", "", token).strip(" :-")
    if token in ("x", "draw", "tie", "the draw"):
        return "DRAW"
    if token in ("1", "w1", "p1", "home"):
        return "AWAY" if swapped else "HOME"
    if token in ("2", "w2", "p2", "away"):
        return "HOME" if swapped else "AWAY"
    if not token:
        return None
    h, a = team_similarity(token, home), team_similarity(token, away)
    if h >= cutoff and h > a:
        return "HOME"
    if a >= cutoff and a > h:
        return "AWAY"
    return None


def _ref(kind: MarketKind, line: float | None = None) -> MarketRef | None:
    try:
        return MarketRef(kind, line)
    except ValueError:
        return None


def interpret(market_text: str, selection_text: str, home: str, away: str, *, swapped: bool = False, cutoff: float = 0.85) -> Interpretation | None:
    """The leg as Ashoka's market on the board's fixture (``home`` and ``away`` are the board's names), or None."""
    market, selection = market_text.strip(), selection_text.strip()
    both = f"{market} {selection}".strip()
    low = both.casefold()
    if not both or _PART_OF_MATCH.search(low) or re.search(r"\(\s*\d+\s*:\s*\d+\s*\)", low):
        return None  # a part of the match, a player or a European (x:y) handicap: not a market Ashoka prices
    if "both teams" in low or "btts" in low:
        answer = "YES" if re.search(r"\byes\b", low) else "NO" if re.search(r"\bno\b", low) else None
        return Interpretation(MarketRef(MarketKind.BTTS), answer) if answer else None
    if "double chance" in low or re.fullmatch(r"(?:dc\s*)?(1x|x2|12)", low):
        code = re.search(r"\b(1x|x2|12)\b", low)
        if code is None:
            return None
        flip = {"1x": "X2", "x2": "1X", "12": "12"}
        value = code.group(1).upper()
        return Interpretation(MarketRef(MarketKind.DOUBLE_CHANCE), flip[code.group(1)] if swapped else value)
    if "draw no bet" in low or re.search(r"\bdnb\b", low):
        side = _side(re.sub(r"draw no bet|\bdnb\b", "", selection or market, flags=re.IGNORECASE), home, away, cutoff, swapped=swapped)
        return Interpretation(MarketRef(MarketKind.DRAW_NO_BET), side) if side in ("HOME", "AWAY") else None
    if _HANDICAP_WORD.search(low):
        number = _HANDICAP_LINE.search(both)
        line = None if number is None else _decimal(number.group(1) or number.group(2))
        code = _HANDICAP_CODE.search(both)
        if code is not None:
            side = "HOME" if (code.group(1) == "1") != swapped else "AWAY"
        else:
            words = _HANDICAP_WORD.sub(" ", both if number is None else both[: number.start()] + " " + both[number.end():])
            side = _side(re.sub(r"[()+\-\d.,:]", " ", words), home, away, cutoff, swapped=swapped)
        if side not in ("HOME", "AWAY") or line is None:
            return None
        home_line = float(line) if side == "HOME" else -float(line)  # a slip shows the selected side's line; Ashoka keys the home line
        ref = _ref(MarketKind.ASIAN_HANDICAP, home_line)
        return Interpretation(ref, side) if ref is not None else None
    for i, pattern in enumerate(_TOTALS_RE):
        if m := pattern.search(both):
            word, number = (m.group(2), m.group(1)) if i == 2 else (m.group(1), m.group(2))
            head = both[: m.start()].strip(" :-")
            head = re.sub(r"\b(?:totals?|goals|match|points|runs|games)\b", "", head, flags=re.IGNORECASE).strip(" :-")
            if head and (team_similarity(head, home) >= cutoff or team_similarity(head, away) >= cutoff):
                return None  # a team's own total
            line = _decimal(number)
            ref = _ref(MarketKind.TOTALS, float(line)) if line is not None else None
            side = "OVER" if word.casefold() in ("over", "o") else "UNDER"
            return Interpretation(ref, side) if ref is not None else None
    result_words = ("1x2", "match result", "result", "winner", "match winner", "moneyline", "to win", "full time result", "match odds", "money line", "ft")
    if not market or any(w in market.casefold() for w in result_words) or re.search(r"\bto win\b", low):
        side = _side(selection or market, home, away, cutoff, swapped=swapped)
        if side is not None:
            return Interpretation(MarketRef(MarketKind.MATCH_ODDS), side)
    return None


__all__ = ["Interpretation", "OcrLine", "ParsedLeg", "ParsedSlip", "clean", "fixture_of", "interpret", "parse", "rows", "split_market", "split_odds",
           "team_similarity", "text_lines"]
