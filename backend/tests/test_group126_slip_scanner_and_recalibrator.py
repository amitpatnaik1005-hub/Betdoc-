"""Group 126: the slip scanner, a bookmaker's slip read from a screenshot, a camera photo or text, vetted by the
Consultant's own engines and re-calibrated. Developed for Amit Ashok Kumar Patnaik.

Pure: the parser on the layouts slips come in (1xBet's line-by-line express, Parimatch's one-line legs, typed text),
what it will not call a price or a booking code, the team names slips abbreviate, the markets it maps and the ones it
will not guess, the structure it vets, the image checks and the OCR bands. RapidOCR on rendered slips: a long phone
capture read in bands without a line split or repeated, and a dark theme. End to end on SQLite and PostgreSQL with the
live market in Redis: a scanned slip gets the very Slip Score ``find_cure`` gives the same legs at the same book, the
Doctor's faults by leg number, the quoted cure applied and nothing recorded; every leg that cannot be vetted is said
and left out; an empty board and an unreadable slip are never scored; the image endpoints read an uploaded screenshot;
refusals; the frontend wiring.
"""

from __future__ import annotations

import base64
import io
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import slip_scanner as scanner_api
from app.core.config import Settings, get_settings
from app.domain import slip_vision as sv
from app.models import User
from app.models.digital_twin import TwinVettingAudit
from app.schemas.slip_scanner import ScanSlipRequest
from app.services import manual_parlay
from app.services.vision import slip_scanner_service as scanner
from tests.test_backtest_and_parlay import app_for, make_user, redis, sessions, settings  # noqa: F401 - fixtures
from tests.test_group104_bet_builder_and_slip_score import _market

S = get_settings()
D = Decimal
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
CREDIT = "Amit Ashok Kumar Patnaik"
SEVERITY = ["CRITICAL", "WARNING", "MILD"]


def _legs(text: str) -> list[tuple[str | None, str, str, str | None]]:
    return [(leg.fixture, leg.market_text, leg.selection_text, None if leg.odds is None else str(leg.odds)) for leg in sv.parse(sv.text_lines(text)).legs]


# ================================================================ the parser (pure)
ONEXBET = """1xBet
Bet slip
Express (3)
Arsenal - Chelsea
24.10.26, 20:00
1X2
W1
2.45
Real Madrid — Girona
Total
TO(2.5)
1.62
Man City - Wolves
Handicap
Man City (-1.5)
2.10
Overall odds: 8.33
Stake 1,000
Possible win 8,334.90
Coupon code: 5X7A9B
Bet ID 88219417"""


def test_a_1xbet_express_is_read_line_by_line() -> None:
    slip = sv.parse(sv.text_lines(ONEXBET))
    assert _legs(ONEXBET) == [("Arsenal vs Chelsea", "1X2", "W1", "2.45"), ("Real Madrid vs Girona", "Total", "TO(2.5)", "1.62"),
                              ("Man City vs Wolves", "Handicap", "Man City (-1.5)", "2.10")]  # the kickoff line is not a price
    assert (slip.book, slip.kind, slip.stake, slip.total_odds, slip.potential_return) == ("1xbet", "ACCUMULATOR", D("1000"), D("8.33"), D("8334.90"))
    assert slip.product_odds == D("8.33") and slip.odds_check == "MATCH"
    assert slip.booking_code == "5X7A9B" and slip.unread == []  # "Bet ID 88219417" is the book's reference, not a code to load


def test_one_line_legs_typed_slips_and_a_price_above_its_market() -> None:
    parimatch = """PARIMATCH
Accumulator
1. Brighton vs Wolves  Match Result: Brighton  @2.58
2. Everton v Fulham  Both Teams To Score: Yes  @1.72
3. Carlos Alcaraz - Jannik Sinner Winner: Alcaraz @2.05
Total odds 9.10
Stake ₹500
Potential return ₹4,550.00
Booking code: PM7Q2X"""
    slip = sv.parse(sv.text_lines(parimatch))
    assert _legs(parimatch) == [("Brighton vs Wolves", "Match Result", "Brighton", "2.58"), ("Everton vs Fulham", "Both Teams To Score", "Yes", "1.72"),
                                ("Carlos Alcaraz vs Jannik Sinner", "Winner", "Alcaraz", "2.05")]  # the away name runs into the market's own words
    assert (slip.book, slip.kind, slip.stake, slip.total_odds, slip.odds_check, slip.booking_code) == ("parimatch", "ACCUMULATOR", D("500"), D("9.10"), "MATCH", "PM7Q2X")
    typed = "Arsenal v Chelsea - Draw No Bet: Chelsea 2.62\nLiverpool vs Spurs, BTTS No @ 2.30\nReal Madrid - Girona - 1X2: Real Madrid 1.45\nSingle"
    assert _legs(typed) == [("Arsenal vs Chelsea", "Draw No Bet", "Chelsea", "2.62"), ("Liverpool vs Spurs", "", "BTTS No", "2.30"),
                            ("Real Madrid vs Girona", "1X2", "Real Madrid", "1.45")]
    assert sv.parse(sv.text_lines(typed)).kind == "SINGLE"
    above = "Arsenal - Chelsea  2.45\n1X2: Arsenal\nEverton - Fulham\nOver 2.5  1.90"
    assert _legs(above) == [("Arsenal vs Chelsea", "1X2", "Arsenal", "2.45"), ("Everton vs Fulham", "", "Over 2.5", "1.90")]
    gapped = sv.rows([([[48, 40], [400, 40], [400, 80], [48, 80]], "Brighton vs Wolves", 0.99), ([[480, 42], [800, 42], [800, 82], [480, 82]], "1X2: Brighton", 0.98),
                      ([[960, 41], [1030, 41], [1030, 81], [960, 81]], "2.58", 0.97)])
    assert [line.text for line in gapped] == ["Brighton vs Wolves  1X2: Brighton  2.58"] and gapped[0].confidence == pytest.approx(0.97)  # one row, its column gaps kept
    assert _legs(gapped[0].text) == [("Brighton vs Wolves", "1X2", "Brighton", "2.58")]


def test_what_the_parser_will_not_call_a_price_or_a_code() -> None:
    assert sv.split_odds("Total Over 2.5")[0] is None and sv.split_odds("Handicap Man City (-1.5)")[0] is None  # a market's line, not a price
    assert sv.split_odds("1X2: Arsenal @ 2.45") == (D("2.45"), "1X2: Arsenal") and sv.split_odds("@1.01")[0] == D("1.01")
    assert sv.split_odds("1.00")[0] is None  # under the 1.01 floor: not a price
    assert sv.parse(sv.text_lines("Booking code: ABCD")).booking_code is None  # four letters: a word, not a code
    assert sv.parse(sv.text_lines("Share code: 7KQ2ZP")).booking_code == "7KQ2ZP"
    assert sv.parse(sv.text_lines("1xBet Bet slip #882194\nTicket No. 55120")).booking_code is None
    mismatch = sv.parse(sv.text_lines("Arsenal - Chelsea\n1X2: Arsenal 2.45\nEverton - Fulham\n1X2: Everton 1.62\nTotal odds 4.20"))
    assert mismatch.product_odds == D("3.97") and mismatch.odds_check == "MISMATCH"
    assert sv.parse(sv.text_lines("Place bet\nAccept odds changes\nBalance 12,000\n20:00\nToday")).legs == []  # the slip's chrome is not a leg
    header = sv.parse(sv.text_lines("1xBet Bet slip\nArsenal - Chelsea\n1X2: Arsenal 2.45"))
    assert header.book == "1xbet" and header.unread == []


def test_the_team_names_slips_abbreviate() -> None:
    same = [("Man City", "Manchester City"), ("Man Utd", "Manchester United"), ("Spurs", "Tottenham Hotspur"), ("Wolves", "Wolverhampton Wanderers"),
            ("R. Madrid", "Real Madrid"), ("Brighton", "Brighton and Hove Albion"), ("Arsenal", "Arsenal")]
    for short, full in same:
        assert sv.team_similarity(short, full) >= S.SCANNER_NAME_CUTOFF, (short, full)
    for a, b in [("Man City", "Manchester United"), ("Chelsea", "Arsenal"), ("Real Madrid", "Real Sociedad"), ("Manchester City", "Man Utd")]:
        assert sv.team_similarity(a, b) < S.SCANNER_NAME_CUTOFF, (a, b)


def test_the_markets_it_maps_and_the_ones_it_will_not_guess() -> None:
    def read(market: str, selection: str, *, swapped: bool = False, home: str = "Arsenal", away: str = "Chelsea") -> tuple[str, str] | None:
        found = sv.interpret(market, selection, home, away, swapped=swapped)
        return None if found is None else (found.market, found.selection)

    assert read("1X2", "W1") == ("Match Odds", "HOME") and read("Match Result", "Chelsea") == ("Match Odds", "AWAY") and read("", "Draw") == ("Match Odds", "DRAW")
    assert read("1X2", "2", swapped=True) == ("Match Odds", "HOME")  # "Chelsea - Arsenal": the slip's own second team is the board's home
    assert read("Double Chance", "1X", swapped=True) == ("Double Chance", "X2") and read("Draw No Bet", "Chelsea") == ("Draw No Bet", "AWAY")
    assert read("Total", "Over 2.5") == ("Totals 2.5", "OVER") and read("Total", "TU(3.5)") == ("Totals 3.5", "UNDER") and read("", "BTTS No") == ("BTTS", "NO")
    assert read("Asian Handicap", "Chelsea (+0.5)") == ("Asian Handicap -0.5", "AWAY")  # the market is keyed by the home line
    assert read("Handicap", "2 (-1.5)", swapped=True) == ("Asian Handicap -1.5", "HOME")
    assert read("", "Brighton to win", home="Brighton and Hove Albion", away="Wolverhampton Wanderers") == ("Match Odds", "HOME")
    for market, selection in [("Corners", "Over 9.5"), ("1st Half 1X2", "Arsenal"), ("Arsenal Total", "Over 1.5"), ("Handicap", "1 (0:1)"), ("Correct Score", "2-1"),
                              ("Winner", "Alcaraz"), ("", "")]:
        assert read(market, selection) is None, (market, selection)  # part of a match, a team total, a European handicap, a name not in the fixture


def test_the_structure_the_fortress_vets() -> None:
    assert scanner.structure("ACCUMULATOR", 4, 4, True) == (None, True, None) and scanner.structure(None, 1, 1, True) == (None, True, None)
    assert scanner.structure("TRIXIE", 3, 3, True) == ("TRIXIE", False, None)
    kind, straight, note = scanner.structure("YANKEE", 4, 3, False)
    assert kind is None and not straight and "3 of its 4 legs" in (note or "")
    assert "Trixie" in (scanner.structure("SYSTEM", 3, 3, True)[2] or "") and "Goliath" in scanner.SYSTEM_NAMES
    assert "3 singles" in (scanner.structure("SINGLE", 3, 3, True)[2] or "")


def test_a_fixture_is_found_in_either_order_at_its_nearest_kickoff() -> None:
    now = datetime.now(UTC)
    fixtures = {"late": scanner.BoardFixture("late", "Arsenal", "Chelsea", now + timedelta(hours=50)), "soon": scanner.BoardFixture("soon", "Arsenal", "Chelsea", now + timedelta(hours=2)),
                "city": scanner.BoardFixture("city", "Manchester City", "Manchester United", now + timedelta(hours=5))}
    assert [(fx.fixture_id, swapped) for fx, swapped in [scanner.find_fixture("Chelsea", "Arsenal", fixtures, 0.85)]] == [("soon", True)]  # type: ignore[misc]
    assert scanner.find_fixture("Arsenal", "Chelsea", fixtures, 0.85)[1] is False  # type: ignore[index]
    assert scanner.find_fixture("Man City", "Man Utd", fixtures, 0.85)[0].fixture_id == "city"  # type: ignore[index]
    assert scanner.find_fixture("Man City", "Wolves", fixtures, 0.85) is None  # one name matching is not a fixture


# ================================================================ the image checks and the OCR bands (pure)
def _png(img: Image.Image, fmt: str = "PNG", **kw: object) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def test_the_image_checks_turn_scale_and_refuse() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # stored on its side: the camera says rotate 90 degrees
    turned = scanner.prepare_image(_png(Image.new("RGB", (300, 100), "white"), "JPEG", exif=exif.tobytes()), S)
    assert turned.size == (200, 600)  # upright (100x300), then small print doubled
    assert scanner.prepare_image(_png(Image.new("RGB", (3000, 1000), "white")), S).size == (S.SCANNER_OCR_MAX_SIDE, round(1000 * S.SCANNER_OCR_MAX_SIDE / 3000))
    assert scanner.prepare_image(_png(Image.new("RGB", (1080, 2400), "white")), S).size == (1080, 2400)  # a phone screenshot is read as it is
    assert scanner.prepare_image(_png(Image.new("P", (1200, 900))), S).mode == "RGB"
    refusals = {
        "EMPTY_IMAGE": (b"", S), "NOT_AN_IMAGE": (b"\x00\x00\x00\x18ftypheic" + bytes(64), S),
        "FORMAT_NOT_READ": (_png(Image.new("RGB", (64, 64)), "TIFF"), S), "IMAGE_DAMAGED": (_png(Image.new("RGB", (1200, 900), "white"))[:900], S),
        "IMAGE_TOO_LARGE": (_png(Image.new("RGB", (200, 100))), S.model_copy(update={"SCANNER_MAX_PIXELS": 10_000})),
    }
    for reason, (raw, settings_) in refusals.items():
        with pytest.raises(scanner.ScanRefusal) as caught:
            scanner.prepare_image(raw, settings_)
        assert caught.value.reason == reason, (reason, caught.value.reason)
    with pytest.raises(scanner.ScanRefusal) as caught:
        scanner.prepare_image(bytes(2_000), S.model_copy(update={"SCANNER_MAX_IMAGE_BYTES": 1_000}))
    assert caught.value.status_code == 413
    raw = _png(Image.new("RGB", (10, 10)))
    assert scanner.decode_base64("data:image/png;base64," + base64.b64encode(raw).decode(), S) == raw
    assert scanner.decode_base64(base64.encodebytes(raw).decode(), S) == raw  # line-wrapped base64
    with pytest.raises(scanner.ScanRefusal, match="not valid base64"):
        scanner.decode_base64("not base64!", S)
    with pytest.raises(ValidationError):
        ScanSlipRequest(image_base64="eA==", slip_text="Arsenal - Chelsea 2.45")  # one or the other
    with pytest.raises(ValidationError):
        ScanSlipRequest()


def test_the_bands_tile_a_tall_capture_exactly() -> None:
    side = S.SCANNER_OCR_MAX_SIDE
    for height in (500, side, side + 1, 4304, 9000, 40_000):
        spans = scanner.bands(height, side)
        assert spans[0][0] == 0 and spans[-1][1] == height and all(bottom - top <= side for top, bottom in spans)
        assert all(b[0] == a[1] - scanner.BAND_OVERLAP for a, b in zip(spans, spans[1:], strict=False))
        kept = [(top + scanner.BAND_OVERLAP / 2 if i else -math.inf, bottom - scanner.BAND_OVERLAP / 2 if i < len(spans) - 1 else math.inf)
                for i, (top, bottom) in enumerate(spans)]
        assert all(a[1] == b[0] for a, b in zip(kept, kept[1:], strict=False))  # every point of the image is kept by exactly one band


# ================================================================ RapidOCR on rendered slips
def render(rows: list[str], *, width: int = 1080, row_h: int = 96, size: int = 40, bg: object = "white", fg: object = (25, 25, 25), blank: int = 0) -> bytes:
    """A slip as a phone screenshot: each row's last double-space part right-aligned, like a price column."""
    img = Image.new("RGB", (width, 80 + row_h * (len(rows) + blank)), bg)  # type: ignore[arg-type]
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=size)
    y = 40
    for i, row in enumerate(rows):
        left, _, right = row.rpartition("  ") if "  " in row else (row, "", "")
        draw.text((48, y), left, fill=fg, font=font)  # type: ignore[arg-type]
        if right:
            draw.text((width - 48 - draw.textlength(right, font=font), y), right, fill=fg, font=font)  # type: ignore[arg-type]
        y += row_h * (1 + (blank if i == len(rows) // 2 else 0))
    return _png(img)


FOUR = ["Arsenal - Chelsea", "1X2: Arsenal  2.45", "Brighton vs Wolves", "Total: Over 2.5  1.90", "Everton v Fulham", "Both Teams To Score: Yes  1.72",
        "Man City - Man Utd", "Handicap: Man City (-1.5)  2.10"]
FOUR_LEGS = [("Arsenal vs Chelsea", "1X2", "Arsenal", "2.45"), ("Brighton vs Wolves", "Total", "Over 2.5", "1.90"), ("Everton vs Fulham", "Both Teams To Score", "Yes", "1.72"),
             ("Man City vs Man Utd", "Handicap", "Man City (-1.5)", "2.10")]


def test_rapidocr_reads_a_long_phone_capture_in_bands_and_a_dark_theme() -> None:
    tall = render(["1xBet  Bet slip", *FOUR, "Accumulator (4)", "Total odds  16.81", "Stake  500.00", "Booking code: 7KQ2ZP"], blank=30)
    assert len(scanner.bands(Image.open(io.BytesIO(tall)).height, S.SCANNER_OCR_MAX_SIDE)) > 1
    dark = render(["Parimatch", *FOUR, "Total odds  16.81"], bg=(18, 22, 30), fg=(235, 235, 235))
    for raw, book in ((tall, "1xbet"), (dark, "parimatch")):
        lines = sv.rows(scanner.ocr_tokens(raw, S))
        texts = [line.text for line in lines]
        assert len(texts) == len(set(texts)) and all(line.confidence >= 0.8 for line in lines)  # no line split across a band or read twice
        slip = sv.parse(lines)
        assert [(leg.fixture, leg.market_text, leg.selection_text, str(leg.odds)) for leg in slip.legs] == FOUR_LEGS
        assert slip.book == book and slip.total_odds == D("16.81") and slip.odds_check == "MATCH"
    assert sv.rows(scanner.ocr_tokens(_png(Image.new("RGB", (1080, 1400), "white")), S)) == []  # a blank image reads nothing


# ================================================================ end to end: the board in Redis, the Consultant's engines
def _app(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user: User) -> FastAPI:
    app = app_for(sessions, redis, settings, user)
    app.include_router(scanner_api.router, prefix="/api/v1")
    return app


async def _audits(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return int((await session.execute(select(func.count()).select_from(TwinVettingAudit))).scalar_one())


SLIP = ["1xBet", "Accumulator", "Arsenal - Chelsea", "1X2: Arsenal  2.45", "Everton v Fulham", "Match Winner: Everton  1.62", "Total odds  3.97", "Stake  1,000"]
SLIP_IDS = ["fx-fair|Match Odds|HOME", "fx-bad|Match Odds|HOME"]
STORE_KEYS = {"leg_id", "fixture", "label", "market", "prices", "fair_probability"}


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=120)


@pytest.mark.asyncio
async def test_a_scanned_slip_is_vetted_by_the_consultants_engines_and_recalibrated(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with _client(_app(sessions, redis, settings, user)) as client:
        await _market(redis, settings, client)
        response = await client.post("/api/v1/slip-scanner/scan-and-rate", json={"slip_text": "\n".join(SLIP)})
        assert response.status_code == 200 and response.headers["X-Engineered-For"] == CREDIT
        body = response.json()
    assert body["developer_credit"] == CREDIT and re.fullmatch(r"SCAN-[0-9A-F]{10}", body["scan_id"])
    assert (body["source"], body["bookmaker"], body["skin"], body["detected_kind"]) == ("text", "1xbet", "one_xbet", "ACCUMULATOR")  # the book the slip names
    legs = body["legs_scanned"]
    assert [leg["status"] for leg in legs] == ["MATCHED", "MATCHED"] and [leg["leg_id"] for leg in legs] == SLIP_IDS
    assert [leg["book_price_now"] for leg in legs] == ["2.45", "1.62"] and legs[1]["ev_at_slip_odds"] < 0 and legs[1]["fair_probability"] < 1 / 1.62
    assert legs[0]["label"] == "1X2: Arsenal" and legs[0]["fixture"] == "Arsenal vs Chelsea" and legs[0]["consensus_books"] >= 3
    assert (body["odds_check"], body["printed_total_odds"], body["total_scanned_odds"]) == ("MATCH", "3.97", "3.969")
    assert (body["stake_inr"], body["estimated_payout_inr"], body["coverage"]) == ("1000.00", "3969.00", {"read": 2, "vetted": 2})

    # the Consultant's engines: the very Slip Score find_cure gives these legs at this book
    cure = await manual_parlay.find_cure(sessions, redis, settings, user.id, leg_ids=SLIP_IDS, kind=None, book="1xbet", bankroll=None, now=datetime.now(UTC))
    assert body["vetting_score"] == pytest.approx(cure.current.rating.score, abs=0.01) and body["tier"] == cure.current.rating.tier
    assert body["rating"]["score"] == pytest.approx(body["vetting_score"]) and len(body["rating"]["breakdown"]) == 15
    assert body["joint_win_probability_pct"] == pytest.approx(cure.current.audit.joint_probability * 100, abs=0.01)
    assert body["verdict"] == "DECLINED" and body["verdict_status"] in ("APPROVED_WITH_CURE", "REJECTED")
    if body["verdict_status"] == "APPROVED_WITH_CURE":  # the cured slip is approved; the slip as printed is not
        assert body["verdict_badge"].startswith("DECLINED AS PRINTED · ")
    assert "Vetted at 1xBet's prices now." in body["verdict_explanation"]
    faults = body["faults_detected"]
    negative = next(f for f in faults if f["code"] == "NEGATIVE_EV")
    assert negative["source"] == "doctor" and negative["severity"] == "CRITICAL" and negative["legs"] == [2]  # leg ids mapped to the slip's own numbers
    assert not any(f["code"] == "NEGATIVE_EV_AT_SLIP_PRICE" for f in faults)  # the Doctor already said it, once
    assert [f["severity"] for f in faults] == sorted((f["severity"] for f in faults), key=SEVERITY.index)

    # the re-calibration: the Consultant's quoted cure, applied
    rounds = body["recalibration_rounds"]
    assert body["is_recalibrated"] and 1 <= len(rounds) <= settings.SCANNER_CURE_ROUNDS
    assert rounds[0]["replaced"]["leg_id"] == "fx-bad|Match Odds|HOME" and rounds[0]["replacement"]["leg_id"].startswith("fx-good|")
    assert rounds[0]["before"]["score"] == pytest.approx(body["vetting_score"]) and body["recalibrated_score"] > body["vetting_score"]
    assert cure.best is not None and rounds[0]["after"]["score"] == pytest.approx(cure.best.rating.score, abs=0.01)
    actions = {leg["action"]: leg for leg in body["recalibrated_legs"]}
    assert set(actions) == {"RETAINED", "SUBSTITUTED"} and actions["RETAINED"]["leg_id"] == SLIP_IDS[0]
    swapped = actions["SUBSTITUTED"]
    assert swapped["replaces"].startswith("Leg 2: 1X2: Everton") and "negative EV at this book" in swapped["rationale"] and swapped["price"] is not None
    assert [leg["leg_id"] for leg in body["recalibrated_consultant_legs"]] == [leg["leg_id"] for leg in body["recalibrated_legs"]]
    assert all({"1xbet", "parimatch"} <= set(leg["prices"]) for leg in body["recalibrated_consultant_legs"])  # every book's price: the skin can change
    assert D(body["recalibrated_total_odds"]) > 1 and body["recalibrated_ev_pct"] is not None and "from the live board lifts the Slip Score from" in body["recalibration_summary"]
    assert body["recalibrated_projected_payout_inr"] == str((D("1000") * D(body["recalibrated_total_odds"])).quantize(D("0.01")))
    store = body["consultant_legs"]
    assert [leg["leg_id"] for leg in store] == SLIP_IDS and all(set(leg) == STORE_KEYS for leg in store) and store[0]["prices"]["1xbet"] == 2.45
    assert await _audits(sessions) == 0  # trials only: nothing recorded, nothing sealed


PARTIAL = ["Parimatch", "Brighton vs Wolves", "Brighton to win @ 2.70", "Chelsea v Arsenal  1X2: 2  2.40", "Arsenal v Chelsea  Total: Over 2.5  1.90",
           "Everton v Fulham  Corners: Over 9.5  1.85", "Bayern - Dortmund", "1X2: Bayern", "Liverpool vs Spurs  1X2: Liverpool  1.70", "Over 1.5 goals  1.30"]


@pytest.mark.asyncio
async def test_every_leg_that_cannot_be_vetted_is_said_and_left_out(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with _client(_app(sessions, redis, settings, user)) as client:
        await _market(redis, settings, client)
        body = (await client.post("/api/v1/slip-scanner/scan-and-rate", json={"slip_text": "\n".join(PARTIAL), "stake_inr": 250})).json()
    legs = body["legs_scanned"]
    assert [leg["status"] for leg in legs] == ["MATCHED", "MATCHED", "MARKET_NOT_PRICED", "MARKET_NOT_COVERED", "NO_ODDS", "NOT_ON_BOARD", "FIXTURE_NOT_READ"]
    assert [leg["leg_id"] for leg in legs[:2]] == ["fx-good|Match Odds|HOME", "fx-fair|Match Odds|HOME"]  # "Chelsea v Arsenal: 2" is Arsenal, the board's home
    assert legs[1]["fixture"] == "Arsenal vs Chelsea" and legs[0]["book_price_now"] == "2.58" and body["bookmaker"] == "parimatch"
    assert body["coverage"] == {"read": 7, "vetted": 2} and body["stake_inr"] == "250.00"
    assert body["total_scanned_odds"] is None and body["estimated_payout_inr"] is None  # a leg without a price: no total is invented
    assert body["joint_win_probability_pct"] is None and body["overall_ev_pct"] is None  # the score covers two legs, not the slip
    assert body["verdict"] in ("PARTIAL", "DECLINED") and "Only 2 of 7 legs are on the board" in body["verdict_explanation"]
    by_code = {f["code"]: f for f in body["faults_detected"] if f["source"] == "scanner"}
    expected = {"MARKET_NOT_PRICED": [3], "MARKET_NOT_COVERED": [4], "NO_ODDS": [5], "NOT_ON_BOARD": [6], "FIXTURE_NOT_READ": [7], "PRICE_MOVED": [1]}
    assert {code: by_code[code]["legs"] for code in expected} == expected
    assert "2.70" in by_code["PRICE_MOVED"]["text"] and "Parimatch quotes 2.58 now" in by_code["PRICE_MOVED"]["text"]
    assert "Corners: Over 9.5" in by_code["MARKET_NOT_COVERED"]["text"] and "Totals 2.5 / OVER on Arsenal vs Chelsea" in by_code["MARKET_NOT_PRICED"]["text"]
    if body["is_recalibrated"]:
        dropped = [leg for leg in body["recalibrated_legs"] if leg["action"] == "DROPPED"]
        assert len(dropped) == 5 and all(leg["rationale"].startswith("Left out: ") for leg in dropped)
        assert body["recalibration_summary"].endswith("Legs that could not be vetted are left out.")
    assert [leg["leg_id"] for leg in body["consultant_legs"]] == ["fx-good|Match Odds|HOME", "fx-fair|Match Odds|HOME"]


@pytest.mark.asyncio
async def test_an_empty_board_and_an_unreadable_slip_are_never_scored(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with _client(_app(sessions, redis, settings, user)) as client:
        empty = (await client.post("/api/v1/slip-scanner/scan-and-rate", json={"slip_text": "\n".join(SLIP)})).json()  # nothing on the board
        assert (empty["verdict"], empty["verdict_badge"], empty["vetting_score"], empty["tier"]) == ("UNRATED", "NOT VETTED", None, None)
        assert [f["code"] for f in empty["faults_detected"]] == ["BOARD_UNAVAILABLE"] and "no priced fixture" in empty["faults_detected"][0]["text"]
        assert empty["consultant_legs"] == [] and not empty["is_recalibrated"] and empty["total_scanned_odds"] == "3.969"  # what the slip says is still read
        nothing = (await client.post("/api/v1/slip-scanner/scan-and-rate", json={"slip_text": "Good luck!\nPlace bet"})).json()
        assert (nothing["verdict"], nothing["legs_scanned"], nothing["coverage"]) == ("UNREADABLE", [], {"read": 0, "vetted": 0})
        assert (await client.post("/api/v1/slip-scanner/scan-and-rate", json={})).status_code == 422
        assert (await client.post("/api/v1/slip-scanner/scan-and-rate", json={"slip_text": "x", "bookmaker": "betway"})).status_code == 422
        bad = await client.post("/api/v1/slip-scanner/scan-and-rate", json={"image_base64": "%%%"})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "NOT_BASE64"
    short = settings.model_copy(update={"SCANNER_MAX_TEXT_CHARS": 200})
    async with _client(_app(sessions, redis, short, user)) as client:
        long = await client.post("/api/v1/slip-scanner/scan-and-rate", json={"slip_text": "Arsenal - Chelsea 2.45\n" * 20})
        assert long.status_code == 413 and long.json()["detail"]["code"] == "TEXT_TOO_LONG"
    async with _client(_app(sessions, None, settings, user)) as client:
        offline = (await client.post("/api/v1/slip-scanner/scan-and-rate", json={"slip_text": "\n".join(SLIP)})).json()
        assert offline["verdict"] == "UNRATED" and "not reachable" in offline["faults_detected"][0]["text"]


@pytest.mark.asyncio
async def test_the_image_endpoints_read_an_uploaded_screenshot(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    png = render(SLIP)
    async with _client(_app(sessions, redis, settings, user)) as client:
        await _market(redis, settings, client)
        uploaded = await client.post("/api/v1/slip-scanner/upload-and-rate", files={"file": ("slip.png", png, "image/png")}, data={"bookmaker": "auto"})
        assert uploaded.status_code == 200 and uploaded.headers["X-Engineered-For"] == CREDIT
        body = uploaded.json()
        assert body["source"] == "image" and [leg["leg_id"] for leg in body["legs_scanned"]] == SLIP_IDS and body["bookmaker"] == "1xbet"
        assert body["odds_check"] == "MATCH" and body["stake_inr"] == "1000.00" and all(line["parsed"] for line in body["ocr_lines"])
        assert {line["text"] for line in body["ocr_lines"]} >= {"Arsenal - Chelsea", "1X2: Arsenal  2.45", "Total odds  3.97"}
        at_parimatch = (await client.post("/api/v1/slip-scanner/scan-and-rate",
                                          json={"image_base64": "data:image/png;base64," + base64.b64encode(png).decode(), "bookmaker": "parimatch", "stake_inr": 400})).json()
        assert at_parimatch["bookmaker"] == "parimatch" and at_parimatch["skin"] == "parimatch" and at_parimatch["stake_inr"] == "400.00"
        assert [leg["book_price_now"] for leg in at_parimatch["legs_scanned"]] == ["2.40", "1.60"]  # Parimatch's own prices, not the slip's
        assert any(f["code"] == "PRICE_MOVED" and f["legs"] == [1] for f in at_parimatch["faults_detected"])  # 2.45 on the slip, 2.40 at Parimatch
        text = await client.post("/api/v1/slip-scanner/upload-and-rate", files={"file": ("slip.txt", b"Arsenal - Chelsea 2.45", "text/plain")})
        assert text.status_code == 415 and text.json()["detail"]["code"] == "NOT_AN_IMAGE"
    small = settings.model_copy(update={"SCANNER_MAX_IMAGE_BYTES": 1_000})
    async with _client(_app(sessions, redis, small, user)) as client:
        big = await client.post("/api/v1/slip-scanner/upload-and-rate", files={"file": ("slip.png", png, "image/png")})
        assert big.status_code == 413 and big.json()["detail"]["code"] == "IMAGE_TOO_LARGE"


def test_the_scanner_is_mounted_for_a_signed_in_user() -> None:
    router = (Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "router.py").read_text(encoding="utf-8")
    assert "api_router.include_router(slip_scanner.router, dependencies=_authenticated)" in router
    from app.main import app  # noqa: PLC0415

    paths = app.openapi()["paths"]
    assert {"/api/v1/slip-scanner/scan-and-rate", "/api/v1/slip-scanner/upload-and-rate"} <= set(paths)


def test_the_frontend_scans_from_a_laptop_and_a_phone_and_opens_the_consultant() -> None:
    modal = (FRONTEND / "components" / "slip_scanner" / "SlipScannerModal.tsx").read_text(encoding="utf-8")
    lib = (FRONTEND / "lib" / "slip_scanner.ts").read_text(encoding="utf-8")
    for cue in ('addEventListener("paste"', "clipboardData", "navigator.clipboard.read()", "window.isSecureContext", 'capture="environment"', 'accept="image/*"',
                "onDrop={onDrop}", "onDragEnter={onDrag}", '"(pointer: coarse)"', 'role="dialog"', 'aria-modal="true"', 'e.key === "Escape"', "<textarea"):
        assert cue in modal, cue  # paste, clipboard, camera, drag and drop, the text tab, an accessible dialog
    assert "SlipScoreMeter" in modal and "consultant_legs" in modal and "recalibrated_consultant_legs" in modal and 'navigate(where === "consultant"' in modal
    assert "Developed for Amit Ashok Kumar Patnaik" in modal and "never opened" in modal  # a booking code is shown, not loaded
    assert "apiClient.post<ScanResult>(\"/slip-scanner/upload-and-rate\", form, true, SCAN_TIMEOUT_MS)" in lib and "/slip-scanner/scan-and-rate" in lib
    for text in (modal, lib):
        assert "fetch(" not in text and "localStorage" not in text  # the shared client and its token, never a raw request
    assert "UPLOAD_MAX_WIDTH = 2048" in lib and 'imageOrientation: "from-image"' in lib
    bet_slip = (FRONTEND / "pages" / "TheBetSlip.tsx").read_text(encoding="utf-8")
    consultant = (FRONTEND / "pages" / "TheConsultant.tsx").read_text(encoding="utf-8")
    assert ">Scan Bet Slip</Button>" in bet_slip and "<SlipScannerModal onClose={() => setScanning(false)} />" in bet_slip
    assert ">Scan External Slip</Button>" in consultant and "<SlipScannerModal onClose={() => setScanning(false)} />" in consultant
    client = (FRONTEND / "api" / "client.ts").read_text(encoding="utf-8")
    assert "timeoutMs: number = REQUEST_TIMEOUT_MS" in client  # one slow call gets its own budget; every other keeps 15 s
