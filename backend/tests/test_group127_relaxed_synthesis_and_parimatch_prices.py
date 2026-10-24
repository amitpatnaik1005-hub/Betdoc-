"""Group 127: honest betting help when the board is short. Developed for Amit Ashok Kumar Patnaik.

The synthesizer's relaxed tier: when fewer fixtures than SYNTH_MIN_LEGS hold an anchor, legs down to the relaxed floors
are admitted at the book's own quote, never on a fixture an anchor holds, each marked, and the slip's expected loss said
in rupees; switched off (or with anchors enough) nothing is relaxed. Parimatch: the odds feed does not carry it, so its
prices are typed from Parimatch through its direct door and then priced on the board like any book's; no Parimatch price
is ever copied from another book or derived from the consensus. The frontend wiring.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from redis.asyncio import Redis

from app.api.v1 import parimatch_feed
from app.core.config import Settings, get_settings
from app.domain import street_smart as ss
from app.services import street_smart_synthesizer as synth
from app.services.aryabhata_pipeline import AryabhataConsumer
from tests.test_backtest_and_parlay import app_for, make_user, redis, sessions, settings  # noqa: F401 - fixtures
from tests.test_ultra_vetting import PINNACLE_NO_ARB, SOFT, seed

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
NOW = datetime.now(UTC).replace(microsecond=0)


def _leg(fixture: str, p: float, ev: float, odds: str = "1.70", *, sport: str = "Football", hours: float = 3) -> ss.PricedLeg:
    return ss.PricedLeg(leg_id=f"{fixture}|Match Odds|HOME", fixture_id=fixture, home=f"Home {fixture}", away=f"Away {fixture}", sport_key="soccer_epl",
                        sport=sport, league="EPL", kickoff=NOW + timedelta(hours=hours), market="Match Odds", selection="HOME", label=f"1X2: Home {fixture}",
                        book="1xbet", odds=Decimal(odds), probability=p, ev=ev, consensus_books=4, code="W1")


def _board(monkeypatch: pytest.MonkeyPatch, legs: list[ss.PricedLeg]) -> list[SimpleNamespace]:
    """Candidates whose price at the book is the given PricedLeg: the gate logic, without fitting a market."""
    table = {leg.leg_id: leg for leg in legs}
    monkeypatch.setattr(synth, "priced", lambda leg, book, settings, now: table.get(leg.leg_id))
    return [SimpleNamespace(leg_id=leg.leg_id, fixture_id=leg.fixture_id, kickoff=leg.kickoff) for leg in legs]


BOARD = [
    _leg("anchor", 0.66, 0.02, "1.55"),  # strict: 66%, a fair-or-better price
    _leg("near-fair", 0.60, -0.03, "1.62"),  # relaxed: an anchor's probability at a price 3% under fair
    _leg("coin", 0.50, -0.04, "1.92"),  # relaxed
    _leg("value-dog", 0.47, 0.01, "2.15"),  # relaxed: under the anchor floor, priced over fair
    _leg("too-long", 0.40, 0.00, "2.50"),  # under the relaxed probability floor
    _leg("juiced", 0.70, -0.08, "1.31"),  # under the relaxed EV floor
    _leg("late", 0.62, -0.01, "1.60", hours=80),  # beyond the horizon
]


def test_the_relaxed_tier_fills_a_short_board_within_its_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    second = dataclasses.replace(_leg("anchor", 0.80, -0.01), leg_id="anchor|Double Chance|1X")  # a likelier leg on the anchor's own fixture
    rows = _board(monkeypatch, [*BOARD, second])
    strict = synth.anchors(rows, "1xbet", s, NOW)
    assert [leg.fixture_id for leg in strict] == ["anchor"]
    relaxed = synth.relaxed_anchors(rows, strict, "1xbet", s, NOW)
    assert sorted(leg.fixture_id for leg in relaxed) == ["coin", "near-fair", "value-dog"]  # never on the anchor's fixture
    assert all(leg.probability >= s.SYNTH_RELAXED_MIN_PROBABILITY and leg.ev >= s.SYNTH_RELAXED_MIN_EV for leg in relaxed)
    assert synth.relaxed_anchors(rows, strict, "1xbet", s.model_copy(update={"SYNTH_RELAX_WHEN_SHORT": False}), NOW) == []
    four = [_leg(f"a{i}", 0.65, 0.01) for i in range(4)]
    rows = _board(monkeypatch, [*four, *BOARD[1:]])
    assert synth.relaxed_anchors(rows, synth.anchors(rows, "1xbet", s, NOW), "1xbet", s, NOW) == []  # anchors enough: nothing relaxed


@pytest.mark.asyncio
async def test_a_relaxed_slip_is_labelled_and_its_expected_loss_said(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    rows = _board(monkeypatch, BOARD)

    async def board(redis: object, settings: Settings, now: datetime) -> list[SimpleNamespace]:  # noqa: ARG001
        return rows

    monkeypatch.setattr(synth, "board", board)
    out = await synth.synthesize_for(None, s, NOW, stake=Decimal("300"), target_return=Decimal("3000"), book="1xbet")
    tiers = {leg["fixture_id"]: (leg["tier"], leg["below_fair"]) for leg in out["legs"]}
    assert tiers["anchor"] == ("ANCHOR", False) and tiers["near-fair"] == ("RELAXED", True) and tiers["value-dog"] == ("RELAXED", False)
    assert out["anchor_tier"] == "RELAXED" and set(out["relaxed_leg_ids"]) == {leg["leg_id"] for leg in out["legs"] if leg["tier"] == "RELAXED"}
    note = out["relaxation_note"]
    assert note and f"{out['joint_ev']:+.1%}" in note and "never promised" in note and "₹300.00 staked" in note
    expected_loss = Decimal("300") - Decimal(out["expected_return_inr"])
    assert (f"loses ₹{expected_loss:,.2f}" in note) if expected_loss > 0 else ("returns" in note)
    assert out["rules"]["relaxed_min_probability"] == s.SYNTH_RELAXED_MIN_PROBABILITY
    assert out["reached_target"] and Decimal(out["total_odds"]) == Decimal("10.3654") and out["leg_count"] == 4  # 1.55 x 1.62 x 1.92 x 2.15: the 10x reached
    assert out["joint_ev"] < 0 < out["joint_probability"]  # reached is not good value: the note says so


# ================================================================ Parimatch: typed from Parimatch, never derived
def test_no_parimatch_price_is_ever_derived() -> None:
    from app.services import ashoka_market, manual_parlay  # noqa: PLC0415

    for module in (ashoka_market, manual_parlay, synth):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "derive_retail_quote" not in source and "1.055" not in source, module.__name__
    assert not hasattr(ashoka_market, "derive_retail_quote")


@pytest.mark.asyncio
async def test_parimatch_prices_typed_from_its_site_reach_the_board(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    kickoff = datetime.now(UTC) + timedelta(hours=3)
    await seed(redis, settings, "fx-pm", "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft={k: v for k, v in SOFT.items() if k != "parimatch"}, kickoff=kickoff)
    app = app_for(sessions, redis, settings, user)
    app.include_router(parimatch_feed.router, prefix="/api/v1")
    consumer = AryabhataConsumer(redis, settings, name="test-127")  # the API process runs this consumer (app.main): frames -> books
    await consumer.ensure_group()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = (await client.get("/api/v1/manual-parlay/board")).json()
        card = next(c for c in before["fixtures"] if c["fixture_id"] == "fx-pm")
        assert all("parimatch" not in s["prices"] for m in card["markets"] for s in m["selections"])  # not in the feed: nothing filled in
        typed = await client.post("/api/v1/parimatch/odds", json={
            "events": [{"fixture_id": "fx-pm", "home": "Arsenal", "away": "Chelsea", "sport_key": "soccer_epl", "kickoff": kickoff.isoformat(),
                        "markets": [{"market": "Match Odds", "prices": {"HOME": "2.38", "DRAW": "3.20", "AWAY": "3.30"}}]}],
            "observed_at": datetime.now(UTC).isoformat(),
        })
        assert typed.status_code == 200 and typed.json()["frames"] == 1
        assert await consumer.step() >= 1
        after = (await client.get("/api/v1/manual-parlay/board")).json()
    market = next(m for m in next(c for c in after["fixtures"] if c["fixture_id"] == "fx-pm")["markets"] if m["market"] == "Match Odds")
    assert {s["selection"]: s["prices"].get("parimatch") for s in market["selections"]} == {"HOME": 2.38, "DRAW": 3.2, "AWAY": 3.3}
    assert market["margins"]["by_book"]["parimatch"] == pytest.approx(1 / 2.38 + 1 / 3.2 + 1 / 3.3 - 1, abs=1e-3)  # its margin, measured like any book's


def test_the_frontend_says_parimatch_is_not_fed_and_switches_sports() -> None:
    # Group 129: the operator asked for the typed-entry box to go; the board says once that the feeds do not carry Parimatch
    assert not (FRONTEND / "components" / "parlay" / "ParimatchPriceEntry.tsx").exists()
    note = (FRONTEND / "components" / "parlay" / "ParimatchFeedNote.tsx").read_text(encoding="utf-8")
    assert "not in the connected odds feeds" in note and not re.search(r"1\.055|derive_|prices\.1xbet", note)  # no other book's price stands in
    for page in ("components/oracle/board/MatchDetail.tsx", "components/parlay/ManualParlayWorkbench.tsx"):
        text = (FRONTEND / page).read_text(encoding="utf-8")
        assert "<ParimatchFeedNote />" in text and "ParimatchPriceEntry" not in text
    board = (FRONTEND / "components" / "vault" / "SportSwitchboard.tsx").read_text(encoding="utf-8")
    assert "switchSport(s.key, polling)" in board and "credits/day" in board and "always on" in board
    assert "<SportSwitchboard />" in (FRONTEND / "components" / "vault" / "VaultFleet.tsx").read_text(encoding="utf-8")
    synthesizer = (FRONTEND / "components" / "builder" / "StreetSmartSynthesizer.tsx").read_text(encoding="utf-8")
    assert "slip.relaxation_note" in synthesizer and 'leg.tier === "RELAXED"' in synthesizer and "below fair" in synthesizer
