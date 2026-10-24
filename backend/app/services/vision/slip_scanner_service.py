"""The slip scanner (Group 126): a bookmaker's bet slip, as a screenshot, a camera photo or pasted text, read, matched to
the live board, vetted and re-calibrated. Developed for Amit Ashok Kumar Patnaik.

BetDoc is Krishna; the user, Arjuna, places every slip. The scanner reads what the user shows it and judges it with the
same engines as the BetDoc Consultant; it never places, loads or changes anything at a bookmaker, and a booking code it
reads is only shown (a code cannot be expanded into legs without the bookmaker's site, which BetDoc does not scrape).

1. **Read.** An image is checked (PNG, JPEG, WEBP, BMP or GIF; at most ``SCANNER_MAX_IMAGE_BYTES`` and
   ``SCANNER_MAX_PIXELS``), turned upright from its EXIF orientation and scaled (wider than ``SCANNER_OCR_MAX_SIDE``
   down, narrower than ``SCANNER_OCR_MIN_WIDTH`` up, at most 2x), then read by RapidOCR on this machine (ONNX Runtime:
   the screenshot never leaves BetDoc and is not stored). A tall phone screenshot is read in overlapping bands, so a long
   slip keeps its print size. Words under ``SCANNER_OCR_MIN_CONFIDENCE`` are shown but not parsed. Text is taken as it is.
2. **Parse** (``app.domain.slip_vision``): legs, prices, the printed total, stake, potential return and booking code.
3. **Match.** Each leg's fixture to a fixture on the fortress's own board (the pre-match board plus fixtures that started
   within ``vetting.LIVE_LOOKBACK``), by both team names, either order, with the abbreviations slips use; and its market
   to Ashoka's (1X2, totals, BTTS, double chance, draw no bet, Asian handicap). A matched leg carries its consensus
   probability, the EV of the slip's own price, and the book's price now.
4. **Vet.** The matched legs go through the Consultant's own engines, nothing recorded: the 15-pillar fortress and the
   Slip Score (``manual_parlay.find_cure``), the Doctor's pathologies (``diagnose``), the prescription and the verdict
   (``verdict``). A scan and a consultation of the same legs at the same book cannot disagree.
5. **Re-calibrate.** When a cure scores higher, it is applied and sought again, at most ``SCANNER_CURE_ROUNDS`` times:
   each replacement is a leg the book quotes at positive EV on a fixture the slip does not touch. Legs that could not be
   vetted are left out of the re-calibrated slip, and said so.

Either slip opens in the Consultant (``consultant_legs``), which seals it; an approved one becomes the official Bet-Slip.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import math
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain import slip_vision as sv
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, market_selection_label
from app.domain.consultant import diagnostics as dx
from app.domain.oracle.parlay_engine import SYSTEMS, LegCandidate
from app.schemas.slip_scanner import DEVELOPER_CREDIT, LegStatus, SlipVerdict
from app.services import ashoka_market
from app.services import consultant as consultant_service
from app.services import manual_parlay as workbench
from app.services.twin import vetting

logger = logging.getLogger("betdoc.slip_scanner")

FORMATS = frozenset({"PNG", "JPEG", "MPO", "WEBP", "BMP", "GIF"})
SKIN_OF = {"parimatch": "parimatch", "1xbet": "one_xbet", "stake": "stake"}
SYSTEM_LEGS = {kind.value: legs for kind, (legs, _) in SYSTEMS.items()}
_names = [kind.value.replace("_", " ").title() for kind in SYSTEMS]
SYSTEM_NAMES = ", ".join(_names[:-1]) + " and " + _names[-1]
BAND_OVERLAP = 200  # px two OCR bands of a tall screenshot share: a line shorter than this is whole in the band that keeps it
OCR_FLOOR = 0.3  # RapidOCR drops words under this; words under SCANNER_OCR_MIN_CONFIDENCE are shown, not parsed
LOW_CONFIDENCE = 0.8  # a leg read with less is flagged for the user to check
PRICE_MOVE = 0.02  # a book price now this far from the slip's (relative) is flagged
SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "MILD": 2}
CENT = Decimal("0.01")
Token = tuple[list[list[float]], str, float]


def odds(value: float | Decimal) -> Decimal:
    """A price as odds are printed: at least two decimals, no trailing zeros past them (2.40, 1.833, 6.419)."""
    d = Decimal(str(value)).normalize()
    return d.quantize(CENT) if int(d.as_tuple().exponent) > -2 else d


def leg_text(leg: sv.ParsedLeg) -> str:
    """The market and selection as the slip shows them: "Corners: Over 9.5", "BTTS No"."""
    if leg.market_text and leg.selection_text:
        return f"{leg.market_text}: {leg.selection_text}"
    return leg.market_text or leg.selection_text


class ScanRefusal(Exception):
    def __init__(self, reason: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.reason, self.message, self.status_code = reason, message, status_code


# ================================================================ 1. reading the image
def prepare_image(raw: bytes, settings: Settings) -> Image.Image:
    """The upload as an upright RGB image sized for OCR, or ScanRefusal (empty, too large, not an image, damaged)."""
    if not raw:
        raise ScanRefusal("EMPTY_IMAGE", "The image is empty.", 422)
    if len(raw) > settings.SCANNER_MAX_IMAGE_BYTES:
        raise ScanRefusal("IMAGE_TOO_LARGE", f"The image is {len(raw) / 1e6:.1f} MB; the scanner reads up to {settings.SCANNER_MAX_IMAGE_BYTES / 1e6:.0f} MB.", 413)
    try:
        img = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ScanRefusal("NOT_AN_IMAGE", "This file is not an image the scanner reads (PNG, JPEG, WEBP). For an iPhone HEIC photo, take a screenshot "
                                          "instead, or set Camera > Formats to Most Compatible.", 415) from exc
    if (img.format or "") not in FORMATS:
        raise ScanRefusal("FORMAT_NOT_READ", f"{img.format or 'This'} images are not read: send a PNG, JPEG or WEBP screenshot.", 415)
    width, height = img.size
    if width * height > settings.SCANNER_MAX_PIXELS:
        raise ScanRefusal("IMAGE_TOO_LARGE", f"The image is {width}x{height} pixels; the scanner reads up to {settings.SCANNER_MAX_PIXELS:,} pixels.", 413)
    try:
        img = (ImageOps.exif_transpose(img) or img).convert("RGB")
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ScanRefusal("IMAGE_DAMAGED", "The image could not be decoded (damaged or cut short): take the screenshot again.", 422) from exc
    w, h = img.size
    scale = min(1.0, settings.SCANNER_OCR_MAX_SIDE / w)
    if w * scale < settings.SCANNER_OCR_MIN_WIDTH:
        scale = min(2.0, settings.SCANNER_OCR_MIN_WIDTH / w)
    scale = min(scale, math.sqrt(settings.SCANNER_MAX_PIXELS / (w * h)))
    if abs(scale - 1.0) > 0.01:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    return img


def decode_base64(data: str, settings: Settings) -> bytes:
    payload = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    payload = "".join(payload.split())
    if len(payload) > settings.SCANNER_MAX_IMAGE_BYTES * 4 // 3 + 16:
        raise ScanRefusal("IMAGE_TOO_LARGE", f"The image is larger than {settings.SCANNER_MAX_IMAGE_BYTES / 1e6:.0f} MB.", 413)
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ScanRefusal("NOT_BASE64", "image_base64 is not valid base64.", 422) from exc


def bands(height: int, side: int) -> list[tuple[int, int]]:
    """(top, bottom) of the OCR bands of an image this tall: at most ``side`` high, each sharing BAND_OVERLAP with the next."""
    if height <= side:
        return [(0, height)]
    out, top = [], 0
    while True:
        bottom = min(top + side, height)
        out.append((top, bottom))
        if bottom >= height:
            return out
        top += side - BAND_OVERLAP


_engine: tuple[Any, threading.BoundedSemaphore] | None = None
_engine_lock = threading.Lock()


def _ocr_engine(settings: Settings) -> tuple[Any, threading.BoundedSemaphore]:
    """RapidOCR and its concurrency slots, built once per process. The ONNX models ship inside the package (nothing is
    downloaded); its own 2000 px cap is lifted to the band size, which ``prepare_image`` and ``bands`` keep to."""
    global _engine  # noqa: PLW0603 - one engine per process
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR  # noqa: PLC0415 - ONNX Runtime loads on the first scan, not at import

            ocr = RapidOCR(params={"Global.max_side_len": max(2000, settings.SCANNER_OCR_MAX_SIDE), "Global.log_level": "warning",
                                   "Global.text_score": min(OCR_FLOOR, settings.SCANNER_OCR_MIN_CONFIDENCE)})
            _engine = (ocr, threading.BoundedSemaphore(settings.SCANNER_OCR_CONCURRENCY))
    return _engine


def ocr_tokens(raw: bytes, settings: Settings) -> list[Token]:
    """Every word RapidOCR finds, as ``(polygon, text, score)`` in the prepared image's pixels (blocking: run it in a thread)."""
    import numpy as np  # noqa: PLC0415

    img = prepare_image(raw, settings)
    pixels = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])  # RGB -> BGR, as RapidOCR expects
    spans = bands(img.height, settings.SCANNER_OCR_MAX_SIDE)
    engine, slots = _ocr_engine(settings)
    tokens: list[Token] = []
    with slots:
        for i, (top, bottom) in enumerate(spans):
            out = engine(np.ascontiguousarray(pixels[top:bottom]))
            boxes = getattr(out, "boxes", None)
            if boxes is None or len(boxes) == 0:
                continue
            low = top + BAND_OVERLAP / 2 if i > 0 else -math.inf  # the bands' kept zones tile the image exactly
            high = bottom - BAND_OVERLAP / 2 if i < len(spans) - 1 else math.inf
            for box, text, score in zip(boxes.tolist() if hasattr(boxes, "tolist") else boxes, out.txts, out.scores, strict=True):
                poly = [[float(x), float(y) + top] for x, y in box]
                centre = sum(y for _, y in poly) / len(poly)
                if low <= centre < high:
                    tokens.append((poly, str(text), float(score)))
    return tokens


# ================================================================ 3. matching to the board
@dataclass(slots=True)
class Resolved:
    parsed: sv.ParsedLeg
    status: LegStatus
    note: str
    candidate: LegCandidate | None = None
    fixture: str | None = None
    ev_at_slip: float | None = None
    book_price: Decimal | None = None
    ev_at_book: float | None = None

    @property
    def leg_id(self) -> str | None:
        return None if self.candidate is None else self.candidate.leg_id


@dataclass(slots=True)
class BoardFixture:
    fixture_id: str
    home: str
    away: str
    kickoff: datetime | None
    legs: dict[str, LegCandidate] = field(default_factory=dict)


async def board(redis: Redis | None, settings: Settings, now: datetime) -> tuple[list[LegCandidate], str | None]:
    """The fortress's own pool (``vetting`` reads fixtures that started within LIVE_LOOKBACK too), or why there is none."""
    if redis is None:
        return [], "The live board (Redis) is not reachable: no leg can be matched or vetted."
    try:
        candidates, _ = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK)
    except (RedisError, OSError, TimeoutError):
        return [], "The live board (Redis) did not answer: no leg can be matched or vetted."
    if not candidates:
        return [], "The live board has no priced fixture now (the odds feeds are off or quiet): no leg can be vetted until they price again."
    return candidates, None


def board_fixtures(candidates: Sequence[LegCandidate]) -> dict[str, BoardFixture]:
    out: dict[str, BoardFixture] = {}
    for leg in candidates:
        out.setdefault(leg.fixture_id, BoardFixture(leg.fixture_id, leg.home, leg.away, leg.kickoff)).legs[leg.leg_id] = leg
    return out


def find_fixture(home: str, away: str, fixtures: dict[str, BoardFixture], cutoff: float) -> tuple[BoardFixture, bool] | None:
    """The board fixture both names match, and whether the slip lists its teams the other way round; the nearest kickoff
    when two would (a club's next match)."""
    found: list[tuple[float, float, BoardFixture, bool]] = []
    for fx in fixtures.values():
        straight = min(sv.team_similarity(home, fx.home), sv.team_similarity(away, fx.away))
        crossed = min(sv.team_similarity(home, fx.away), sv.team_similarity(away, fx.home))
        score, swapped = (straight, False) if straight >= crossed else (crossed, True)
        if score >= cutoff:
            found.append((score, fx.kickoff.timestamp() if fx.kickoff else math.inf, fx, swapped))
    if not found:
        return None
    found.sort(key=lambda t: (-t[0], t[1]))
    return found[0][2], found[0][3]


def resolve(parsed: sv.ParsedSlip, candidates: Sequence[LegCandidate], book: str | None, settings: Settings, now: datetime) -> list[Resolved]:
    fixtures = board_fixtures(candidates)
    hours = int(ashoka_market.HORIZON.total_seconds() // 3600)
    out: list[Resolved] = []
    for leg in parsed.legs:
        if leg.odds is None:
            out.append(Resolved(leg, LegStatus.NO_ODDS, "Its price was not read: check the line, or type the slip."))
            continue
        if leg.home is None or leg.away is None:
            out.append(Resolved(leg, LegStatus.FIXTURE_NOT_READ, "Its fixture (Team A vs Team B) was not read, so it cannot be matched to the board."))
            continue
        found = find_fixture(leg.home, leg.away, fixtures, settings.SCANNER_NAME_CUTOFF)
        if found is None:
            out.append(Resolved(leg, LegStatus.NOT_ON_BOARD, f"{leg.fixture} is not on the live board: it is over, more than {hours} hours away, "
                                                            "or no feed prices it now."))
            continue
        fx, swapped = found
        name = f"{fx.home} vs {fx.away}"
        meaning = sv.interpret(leg.market_text, leg.selection_text, fx.home, fx.away, swapped=swapped, cutoff=settings.SCANNER_NAME_CUTOFF)
        if meaning is None:
            read = leg_text(leg) or "This market"
            out.append(Resolved(leg, LegStatus.MARKET_NOT_COVERED, f"“{read}” is not a market Ashoka prices (1X2, totals, both teams to score, double "
                                                                   "chance, draw no bet, Asian handicap), so it cannot be vetted.", fixture=name))
            continue
        candidate = fx.legs.get(f"{fx.fixture_id}|{meaning.market}|{meaning.selection}")
        if candidate is None or ashoka_market.fair_value_inputs(candidate) is None:
            out.append(Resolved(leg, LegStatus.MARKET_NOT_PRICED, f"{meaning.market} / {meaning.selection} on {name} is not on the board now.", fixture=name))
            continue
        try:
            ev_slip = candidate.ev(float(leg.odds))
        except ValueError:
            ev_slip = None
        quote = None if book is None else candidate.quotes.get(book)
        if quote is not None and quote.age(now) > settings.ASHOKA_MAX_QUOTE_AGE_SECONDS:
            quote = None
        try:
            ev_book = None if quote is None else candidate.ev(quote.net_odds)
        except ValueError:
            ev_book = None
        out.append(Resolved(leg, LegStatus.MATCHED, "On the live board: priced and vetted.", candidate, name, ev_slip,
                            None if quote is None else odds(quote.odds), ev_book))
    return out


def structure(parsed_kind: str | None, read: int, vetted: int, whole: bool) -> tuple[str | None, bool, str | None]:
    """(the kind the fortress vets, whether the slip is one straight multiple, a note when what is vetted is not the slip as printed)."""
    if parsed_kind in SYSTEM_LEGS:
        name = parsed_kind.replace("_", " ").title()
        if whole and vetted == SYSTEM_LEGS[parsed_kind]:
            return parsed_kind, False, None
        return None, False, (f"The slip is a {name} ({SYSTEM_LEGS[parsed_kind]} legs) and {vetted} of its {read} legs could be vetted: they are vetted "
                             "as one straight multiple, not as the system.")
    if parsed_kind == "SYSTEM":
        return None, False, f"A system bet is vetted as the straight multiple of its legs: the fortress vets the {SYSTEM_NAMES} as systems."
    if parsed_kind == "SINGLE" and read > 1:
        return None, False, (f"The slip holds {read} singles: they are vetted together as one multiple, the Consultant's unit. Open each in the "
                             "Consultant for its own verdict.")
    return None, True, None


# ================================================================ 4-5. vetting and re-calibration (the Consultant's engines, nothing recorded)
@dataclass(slots=True)
class Vetting:
    cure: workbench.Cure
    pathologies: list[dx.Pathology]
    prescription: dict[str, Any] | None
    status: dx.VerdictStatus
    title: str
    summary: str


async def vet(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, leg_ids: list[str], kind: str | None,
              book: str | None, now: datetime) -> Vetting:
    """``consultant.consult`` up to its verdict, with nothing sealed or recorded."""
    cure = await workbench.find_cure(sessions, redis, settings, user_id, leg_ids=leg_ids, kind=kind, book=book, bankroll=None, now=now)
    run = cure.current
    audit = run.audit
    margins = {row["leg_id"]: row.get("account_margin") for row in run.margins["legs"]}
    cap = dx.LawyerPolicy.from_settings(settings).cap_for(audit.bookmaker)
    payout = None if audit.total_odds is None else (audit.stake_inr * audit.total_odds).quantize(CENT)
    pathologies, unexamined = dx.diagnose(audit.pillars, (audit.slip or {}).get("legs", []), audit.bookmaker, margins,
                                          heavy_juice_min=settings.MANUAL_PARLAY_HEAVY_JUICE_MIN, payout=payout, max_payout=cap)
    prescription, cure_view = consultant_service._prescription(cure, pathologies, cap)  # noqa: SLF001 - the Consultant's own prescription
    status, title, summary = dx.verdict(pathologies, is_vetted=audit.is_vetted, score=run.rating.score, tier=run.rating.tier,
                                        gold_floor=float(settings.MANUAL_PARLAY_TIERS["GOLD"]), joint_ev=audit.joint_ev, stake=audit.stake_inr, cure=cure_view,
                                        unexamined=len(unexamined))
    return Vetting(cure, pathologies, prescription, status, title, summary)


async def recalibrate(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, first: workbench.Cure,
                      kind: str | None, book: str | None, now: datetime) -> tuple[list[dict[str, Any]], workbench.Trial | None]:
    """The Consultant's cure, applied while one scores higher, at most SCANNER_CURE_ROUNDS times: (the rounds, the final slip's trial)."""
    rounds: list[dict[str, Any]] = []
    cure, final = first, None
    while cure.leg_ids is not None and cure.best is not None and cure.replaced is not None and len(rounds) < settings.SCANNER_CURE_ROUNDS:
        replacement = cure.replacement or {}
        rounds.append({"replaced": cure.replaced, "replacement": {k: replacement.get(k) for k in ("leg_id", "label", "fixture", "ev", "fair_probability")},
                       "before": cure.before, "after": {"score": cure.best.rating.score, "tier": cure.best.rating.tier}, "leg_ids": list(cure.leg_ids)})
        final = cure.best
        if len(rounds) >= settings.SCANNER_CURE_ROUNDS:
            break
        try:
            cure = await workbench.find_cure(sessions, redis, settings, user_id, leg_ids=cure.leg_ids, kind=kind, book=book, bankroll=None, now=now)
        except (workbench.WorkbenchRefusal, vetting.TwinRefusal, ValueError, RedisError, OSError, TimeoutError):
            break
    return rounds, final


# ================================================================ the scan
def _pct(value: float | None, digits: int = 2) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value * 100.0, digits)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(CENT))


def _book_name(book: str | None) -> str:
    return ASHOKA_BOOKMAKERS.get(book, book) if book else "the best retail price"


def _fault(source: str, code: str, severity: str, legs: Sequence[int], text: str) -> dict[str, Any]:
    return {"source": source, "code": code, "severity": severity, "legs": sorted(set(legs)), "text": text}


def _store_leg(candidate: LegCandidate | None, fallback: dict[str, Any], book: str | None, settings: Settings, now: datetime) -> dict[str, Any] | None:
    """A leg for the Bet-Slip store with every book's price on the board (the Consultant's skin can change), else the audit's own."""
    if candidate is None:
        return _consultant_leg(fallback)
    quote = candidate.best_quote(now, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, (book,) if book else workbench.ACCOUNT_BOOKS)
    return _consultant_leg(ashoka_market.leg_payload(candidate, quote, now))


def _consultant_leg(leg: dict[str, Any]) -> dict[str, Any] | None:
    """A leg payload as the Bet-Slip store holds it (None without a probability, which the Consultant needs)."""
    if not leg.get("leg_id") or leg.get("fair_probability") is None:
        return None
    return {"leg_id": leg["leg_id"], "fixture": str(leg.get("fixture") or ""), "label": str(leg.get("label") or leg["leg_id"]),
            "market": str(leg.get("market") or ""), "prices": {k: float(v) for k, v in (leg.get("prices") or {}).items() if v is not None},
            "fair_probability": float(leg["fair_probability"])}


def _leg_view(r: Resolved) -> dict[str, Any]:
    c = r.candidate
    p = ashoka_market.fair_value_inputs(c)
    return {
        "number": r.parsed.number, "status": r.status.value, "note": r.note, "fixture_read": r.parsed.fixture, "market_read": r.parsed.market_text,
        "selection_read": r.parsed.selection_text, "odds_read": None if r.parsed.odds is None else str(r.parsed.odds), "confidence": round(r.parsed.confidence, 3),
        "leg_id": r.leg_id, "fixture": r.fixture, "sport_key": None if c is None else c.sport_key,
        "kickoff": None if c is None or c.kickoff is None else c.kickoff.isoformat(), "market": None if c is None else c.market.key,
        "selection": None if c is None else c.selection,
        "label": None if c is None else market_selection_label(c.market.key, c.selection, c.home, c.away),
        "fair_probability": None if p is None else round(p, 4), "consensus_books": None if c is None else c.consensus_books,
        "ev_at_slip_odds": None if r.ev_at_slip is None else round(r.ev_at_slip, 4),
        "book_price_now": None if r.book_price is None else str(r.book_price), "ev_at_book_price": None if r.ev_at_book is None else round(r.ev_at_book, 4),
    }


async def scan(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, *, image: bytes | None, text: str | None,
               book: str, stake: Decimal | None, now: datetime) -> dict[str, Any]:
    """Read, match, vet and re-calibrate one slip. ``book``: auto (the book the slip names, else the best retail price) or
    an account book; ``stake``: None for the stake the slip prints, if any. Nothing is stored."""
    scan_id = f"SCAN-{uuid.uuid4().hex[:10].upper()}"
    if image is not None:
        tokens = await asyncio.to_thread(ocr_tokens, image, settings)
        shown = sv.rows(tokens)
        usable = sv.rows(t for t in tokens if t[2] >= settings.SCANNER_OCR_MIN_CONFIDENCE)
        source = "image"
    elif text and text.strip():
        if len(text) > settings.SCANNER_MAX_TEXT_CHARS:
            raise ScanRefusal("TEXT_TOO_LONG", f"The text is longer than {settings.SCANNER_MAX_TEXT_CHARS:,} characters: paste the slip alone.", 413)
        shown = usable = sv.text_lines(text)
        source = "text"
    else:
        raise ScanRefusal("NOTHING_TO_READ", "Send a screenshot or paste the slip's text.", 422)

    parsed = sv.parse(usable)
    account = (None if book == "auto" else book) or (parsed.book if parsed.book in workbench.ACCOUNT_BOOKS else None)
    stake = stake if stake is not None else parsed.stake
    candidates, board_note = await board(redis, settings, now)
    resolved = resolve(parsed, candidates, account, settings, now)
    matched = [r for r in resolved if r.status is LegStatus.MATCHED]
    number_of: dict[str, int] = {}
    for r in matched:
        number_of.setdefault(r.leg_id, r.parsed.number)  # type: ignore[arg-type]
    leg_ids = list(number_of)
    read = len(resolved)
    whole = read > 0 and len(leg_ids) == read  # every leg read is a distinct leg on the board
    kind, straight, structure_note = structure(parsed.kind, read, len(leg_ids), whole)
    exact = whole and (straight or kind is not None)  # what the fortress vets is the slip as printed

    faults: list[dict[str, Any]] = []
    if board_note:
        faults.append(_fault("scanner", "BOARD_UNAVAILABLE", "WARNING", [], board_note))
    for r in resolved:
        if r.status is not LegStatus.MATCHED and not (board_note and r.status is LegStatus.NOT_ON_BOARD):
            faults.append(_fault("scanner", r.status.value, "WARNING", [r.parsed.number], f"Leg {r.parsed.number}: {r.note}"))
        if r.parsed.confidence < LOW_CONFIDENCE:
            faults.append(_fault("scanner", "LOW_OCR_CONFIDENCE", "MILD", [r.parsed.number],
                                 f"Leg {r.parsed.number} was read with {r.parsed.confidence:.0%} confidence: check it against the slip."))
        if r.book_price is not None and r.parsed.odds is not None and abs(float(r.book_price / r.parsed.odds) - 1.0) >= PRICE_MOVE:
            faults.append(_fault("scanner", "PRICE_MOVED", "MILD", [r.parsed.number],
                                 f"Leg {r.parsed.number}: the slip shows {r.parsed.odds}, {_book_name(account)} quotes {r.book_price} now "
                                 f"({float(r.book_price / r.parsed.odds) - 1.0:+.1%}). The vetting uses the price now."))
    duplicates = [r.parsed.number for r in matched if number_of.get(r.leg_id) != r.parsed.number]  # type: ignore[arg-type]
    if duplicates:
        faults.append(_fault("scanner", "DUPLICATE_LEG", "WARNING", duplicates, "The same selection is on the slip twice: it is vetted once."))
    if parsed.odds_check == "MISMATCH":
        faults.append(_fault("scanner", "TOTAL_ODDS_MISMATCH", "WARNING", [],
                             f"The legs' prices multiply to {parsed.product_odds} but the slip prints {parsed.total_odds}: a leg may be missing or misread."))
    if structure_note:
        faults.append(_fault("scanner", "STRUCTURE", "MILD", [], structure_note))

    result: Vetting | None = None
    rounds: list[dict[str, Any]] = []
    final: workbench.Trial | None = None
    if leg_ids and redis is not None:
        try:
            result = await vet(sessions, redis, settings, user_id, leg_ids, kind, account, now)
        except (workbench.WorkbenchRefusal, vetting.TwinRefusal, ValueError) as exc:
            faults.append(_fault("scanner", "NOT_VETTED", "WARNING", [], f"The fortress could not vet these legs: {getattr(exc, 'message', None) or exc}"))
        except (RedisError, OSError, TimeoutError):
            faults.append(_fault("scanner", "BOARD_UNAVAILABLE", "WARNING", [], "The live board (Redis) did not answer while vetting: nothing was scored."))
    if result is not None and redis is not None:
        flagged_negative: set[int] = set()
        for p in result.pathologies:
            d = p.as_dict()
            legs = [number_of[x] for x in d.get("legs", []) if x in number_of]
            if d["code"] == "NEGATIVE_EV":
                flagged_negative.update(legs)
            faults.append(_fault("doctor", d["code"], d["severity"], legs, f"{d['title']}: {d['diagnosis']}" if d.get("diagnosis") else d["title"]))
        for r in matched:
            if r.ev_at_slip is not None and r.ev_at_slip < 0 and r.parsed.number not in flagged_negative:
                faults.append(_fault("scanner", "NEGATIVE_EV_AT_SLIP_PRICE", "WARNING", [r.parsed.number],
                                     f"Leg {r.parsed.number} at the slip's {r.parsed.odds} is priced at a loss: EV {r.ev_at_slip:+.1%} against a fair "
                                     f"{ashoka_market.fair_value_inputs(r.candidate) or 0:.1%}."))
        rounds, final = await recalibrate(sessions, redis, settings, user_id, result.cure, kind, account, now)
    faults.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 3))

    # ---------------------------------------------------------------- the slip as printed
    prices = [r.parsed.odds for r in resolved]
    total = math.prod(prices, start=Decimal(1)).quantize(Decimal("0.001")) if straight and prices and None not in prices else None  # type: ignore[arg-type]
    payout = None if total is None or stake is None else (stake * total).quantize(CENT)
    run = None if result is None else result.cure.current
    joint = run.audit.joint_probability if run is not None and exact else None
    slip_evs = [r.ev_at_slip for r in matched]
    distinct = len({lid.split("|", 1)[0] for lid in leg_ids}) == len(leg_ids)
    overall_ev = math.prod(1.0 + e for e in slip_evs) - 1.0 if exact and straight and distinct and None not in slip_evs else None  # type: ignore[operator]

    # ---------------------------------------------------------------- the verdict
    if not parsed.legs:
        verdict, badge = SlipVerdict.UNREADABLE, "NO LEG READ"
        explanation = ("No leg could be read from this " + ("image" if source == "image" else "text") + ". Send a sharper screenshot of the whole slip, "
                       "straight on, or paste its text: each leg as its fixture (Team A vs Team B), its market and selection, and its price.")
    elif result is None:
        verdict, badge = SlipVerdict.UNRATED, "NOT VETTED"
        explanation = (f"{read} leg{'s' if read != 1 else ''} read, none of them vetted"
                       + (f": {board_note}" if board_note else ".") + " Nothing is scored without the market.")
    else:
        if result.status is not dx.VerdictStatus.APPROVED_GOLD:
            verdict = SlipVerdict.DECLINED  # as printed: an approval that needs the prescription is the cured slip's, not this one's
            badge = result.title if result.status is dx.VerdictStatus.REJECTED else f"DECLINED AS PRINTED · {result.title}"
        elif exact:
            verdict, badge = SlipVerdict.APPROVED, result.title
        else:
            verdict = SlipVerdict.PARTIAL
            badge = f"GOLD ON {len(leg_ids)} OF {read} LEGS" if not whole else "GOLD AS A STRAIGHT MULTIPLE"
        explanation = f"{result.summary} Vetted at {_book_name(account)}" + ("'s prices now." if account else " now.")
        if not whole:
            explanation += f" Only {len(leg_ids)} of {read} legs are on the board: the score covers those, not the whole slip."

    # ---------------------------------------------------------------- the re-calibrated slip
    recalibrated: list[dict[str, Any]] = []
    recal_consultant: list[dict[str, Any]] = []
    summary: str | None = None
    on_board = {leg.leg_id: leg for leg in candidates}
    if final is not None and run is not None:
        audit = final.audit
        scanned = {leg["leg_id"]: leg for leg in (run.audit.slip or {}).get("legs", [])}
        origin: dict[str, str] = {}  # a leg on the re-calibrated slip -> the scanned leg it stands in for
        reason: dict[str, str] = {}
        for rnd in rounds:
            new, old = rnd["replacement"]["leg_id"], rnd["replaced"]["leg_id"]
            first = origin.pop(old, old)
            origin[new] = first
            reason.setdefault(first, str(rnd["replaced"].get("reason") or ""))
        slip = audit.slip or {}
        for leg in slip.get("legs", []):
            leg_id = leg["leg_id"]
            was = origin.get(leg_id)
            price = (leg.get("prices") or {}).get(slip.get("book") or audit.bookmaker)
            row = {"action": "SUBSTITUTED" if was else "RETAINED", "leg_id": leg_id, "fixture": str(leg.get("fixture") or ""), "label": str(leg.get("label") or leg_id),
                   "price": None if price is None else str(odds(price)), "fair_probability": leg.get("fair_probability"), "ev": leg.get("ev"), "replaces": None,
                   "rationale": "Kept: vetted on the live board."}
            if was:
                old_leg = scanned.get(was, {})
                row["replaces"] = f"Leg {number_of.get(was, '?')}: {old_leg.get('label', was)} ({old_leg.get('fixture', '')})"
                row["rationale"] = (f"Replaces leg {number_of.get(was, '?')}, {reason.get(was) or 'the leg that cost the slip most'}: quoted at positive EV "
                                    "on a fixture the slip does not touch, and the fortress rates the slip higher with it.")
            recalibrated.append(row)
            if (store_leg := _store_leg(on_board.get(leg_id), leg, account, settings, now)) is not None:
                recal_consultant.append(store_leg)
        for r in resolved:
            if r.status is not LegStatus.MATCHED:
                recalibrated.append({"action": "DROPPED", "leg_id": None, "fixture": r.parsed.fixture or "fixture not read",
                                     "label": leg_text(r.parsed) or "not read",
                                     "price": None if r.parsed.odds is None else str(r.parsed.odds), "fair_probability": None, "ev": None, "replaces": None,
                                     "rationale": f"Left out: {r.note}"})
        ev_text = "" if audit.joint_ev is None else f"; joint EV {audit.joint_ev:+.1%}"
        many = len(rounds) != 1
        summary = (f"{len(rounds)} replacement{'s' if many else ''} from the live board {'lift' if many else 'lifts'} the Slip Score from {run.rating.score:.0f} "
                   f"({run.rating.tier}) to {final.rating.score:.0f} ({final.rating.tier}){ev_text} at {_book_name(audit.bookmaker)}."
                   + ("" if whole else " Legs that could not be vetted are left out."))
    elif result is not None:
        rx = result.prescription
        summary = (rx.get("message") if rx else "Nothing to re-calibrate: no leg shares a fixture, is priced at a loss or carries a heavy margin, "
                                                "and no pillar vetoes the slip.")

    consultant_legs: list[dict[str, Any]] = []
    for r in matched:
        if r.parsed.number != number_of.get(r.leg_id):  # type: ignore[arg-type]
            continue
        if (store_leg := _store_leg(r.candidate, {}, account, settings, now)) is not None:
            consultant_legs.append(store_leg)

    final_audit = None if final is None else final.audit
    logger.info("slip scan %s: %s, %d legs read, %d vetted, verdict %s", scan_id, source, read, len(leg_ids), verdict.value)
    return {
        "developer_credit": DEVELOPER_CREDIT, "scan_id": scan_id, "source": source, "bookmaker": account, "skin": SKIN_OF.get(account or "", "betdoc"),
        "ocr_lines": [{**line.as_dict(), "parsed": line.confidence >= settings.SCANNER_OCR_MIN_CONFIDENCE} for line in shown[:300]],
        "detected_booking_code": parsed.booking_code, "detected_kind": parsed.kind, "detected_stake_inr": _money(parsed.stake),
        "printed_total_odds": None if parsed.total_odds is None else str(odds(parsed.total_odds)),
        "printed_potential_return_inr": _money(parsed.potential_return), "odds_check": parsed.odds_check,
        "legs_scanned": [_leg_view(r) for r in resolved], "coverage": {"read": read, "vetted": len(leg_ids)},
        "total_scanned_odds": None if total is None else str(odds(total)), "stake_inr": _money(stake), "estimated_payout_inr": _money(payout),
        "joint_win_probability_pct": _pct(joint), "overall_ev_pct": _pct(overall_ev),
        "vetting_score": None if run is None else run.rating.score, "tier": None if run is None else run.rating.tier,
        "verdict": verdict.value, "verdict_status": None if result is None else result.status.value,
        "verdict_badge": badge, "verdict_explanation": explanation, "bookmaker_margin_pct": None if run is None else _pct(run.margins.get("parlay_overround")),
        "rating": None if run is None else run.rating.as_dict(), "faults_detected": faults,
        "is_recalibrated": final is not None, "recalibrated_legs": recalibrated, "recalibration_rounds": rounds,
        "recalibrated_score": None if final is None else final.rating.score, "recalibrated_tier": None if final is None else final.rating.tier,
        "recalibrated_total_odds": None if final_audit is None or final_audit.total_odds is None else str(odds(final_audit.total_odds)),
        "recalibrated_joint_win_prob_pct": None if final_audit is None else _pct(final_audit.joint_probability),
        "recalibrated_ev_pct": None if final_audit is None else _pct(final_audit.joint_ev),
        "recalibrated_projected_payout_inr": None if final_audit is None or final_audit.total_odds is None or stake is None or not straight
        else _money(stake * Decimal(str(final_audit.total_odds))),
        "recommended_kelly_stake_inr": None if final_audit is None or not final_audit.stake_inr else _money(final_audit.stake_inr),
        "recalibration_summary": summary, "consultant_legs": consultant_legs, "recalibrated_consultant_legs": recal_consultant,
    }


__all__ = ["BoardFixture", "Resolved", "ScanRefusal", "bands", "board", "board_fixtures", "decode_base64", "find_fixture", "ocr_tokens", "prepare_image",
           "recalibrate", "resolve", "scan", "structure", "vet"]
