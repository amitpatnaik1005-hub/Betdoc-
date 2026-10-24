"""Group 130: the manual bet builder's universal betslip. Developed for Amit Ashok Kumar Patnaik.

One slip the operator fills at 1xBet or at Parimatch: 1xBet's prices are the feeds' quotes; the feeds do not carry
Parimatch, so its figures are estimates by the operator's rule (98.5% of 1xBet's price; 95% of the fair price when
neither book quotes), each marked as an estimate and never handed to the engines as a quote. The slip takes the board's
latest prices on every refresh and recomputes on every keystroke. Inspecting at the Parimatch skin vets the same
selections at 1xBet's real prices and says so; the synthesizer says why it has no Parimatch price. Pillar 14 can hold
started legs to a tighter limit than pre-match ones. The slip scanner is untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.domain.oracle import fortress
from app.domain.oracle.fortress import FortressPolicy, Status
from app.domain.oracle.parlay_engine import Quote
from app.services import street_smart_synthesizer as synth
from tests.test_backtest_and_parlay import app_for, make_user, redis, sessions, settings  # noqa: F401 - fixtures
from tests.test_ultra_vetting import PINNACLE_NO_ARB, SOFT, seed

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
NOW = datetime.now(UTC).replace(microsecond=0)


# ================================================================ the universal slip's arithmetic (the real module, through node)
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_universal_slip_quotes_1xbet_and_estimates_parimatch() -> None:
    module = (FRONTEND / "lib" / "universal_slip.ts").as_uri()
    legs = [{"prices": {"1xbet": 2.0, "pinnacle": 2.05}, "fair_probability": 0.48}, {"prices": {"1xbet": 1.5}, "fair_probability": 0.64}]
    script = (f"import({json.dumps(module)}).then((m) => console.log(JSON.stringify({{"
              f"x: m.bookPrice({{prices: {{'1xbet': 2.0}}, fair_probability: 0.48}}, 'parimatch'),"
              f"pm: m.bookPrice({{prices: {{parimatch: 2.1}}, fair_probability: 0.46}}, '1xbet'),"
              f"fair: [m.bookPrice({{prices: {{pinnacle: 2.0}}, fair_probability: 0.5}}, '1xbet'), m.bookPrice({{prices: {{pinnacle: 2.0}}, fair_probability: 0.5}}, 'parimatch')],"
              f"none: m.bookPrice({{prices: {{}}, fair_probability: 0}}, 'parimatch'),"
              f"quoted: m.bookPrice({{prices: {{parimatch: 2.3, '1xbet': 2.4}}, fair_probability: 0.4}}, 'parimatch'),"
              f"slip: m.universalSlip({json.dumps(legs)}, 500), empty: m.universalSlip({json.dumps(legs)}, 0)}})))")
    run = subprocess.run(["node", "--experimental-strip-types", "--no-warnings", "-e", script], capture_output=True, text=True, timeout=60)
    if run.returncode != 0 and "experimental-strip-types" in run.stderr:
        pytest.skip("this node cannot run TypeScript")
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["x"] == {"price": 1.97, "source": "from_1xbet"}  # 2.00 x 0.985
    assert out["pm"] == {"price": 2.13, "source": "from_parimatch"}  # 2.10 x 1.015
    assert out["fair"] == [{"price": 1.92, "source": "from_consensus"}, {"price": 1.9, "source": "from_consensus"}]
    assert out["none"] is None and out["quoted"] == {"price": 2.3, "source": "quoted"}  # a quoted price is never replaced
    one, pari = out["slip"]["books"]["1xbet"], out["slip"]["books"]["parimatch"]
    assert (one["odds"], one["payout"], one["profit"], one["estimatedLegs"]) == (3.0, 1500.0, 1000.0, 0)
    assert pari["estimatedLegs"] == 2 and pari["odds"] == round(1.97 * 1.48, 2) and pari["payout"] == round(500 * 1.97 * 1.48, 2)
    assert out["slip"]["gapPct"] == round(abs(1500 - pari["payout"]) / 1500 * 100, 2) and out["slip"]["gapPct"] < 5  # the "19-20" difference
    assert out["empty"]["books"]["1xbet"]["payout"] is None  # no stake, no return invented


# ================================================================ pillar 14: started legs may be held tighter
def test_pillar_14_holds_a_started_leg_to_the_in_play_limit() -> None:
    policy = FortressPolicy.from_settings(get_settings().model_copy(update={"TWIN_MAX_QUOTE_AGE_SECONDS": 900.0, "TWIN_MAX_INPLAY_QUOTE_AGE_SECONDS": 120.0}))
    quote = Quote("1xbet", 2.0, NOW - timedelta(seconds=600))
    before, started = NOW + timedelta(hours=2), NOW - timedelta(minutes=10)
    assert fortress.pillar_14(False, [quote], NOW, policy, kickoffs=[before]).status is Status.PASS  # pre-match: 10 minutes is within 15
    result = fortress.pillar_14(False, [quote], NOW, policy, kickoffs=[started])
    assert result.status is Status.FAIL and "older than 120s" in result.reason  # in play: 2 minutes
    mixed = fortress.pillar_14(False, [quote, quote], NOW, policy, kickoffs=[before, started])
    assert mixed.status is Status.FAIL and "(limit 120s)" in mixed.reason and mixed.reason.count("1xbet") == 1  # only the started leg is stale
    assert Settings.model_fields["TWIN_MAX_INPLAY_QUOTE_AGE_SECONDS"].default is None  # shipped: one rule for every leg unless set
    one_rule = FortressPolicy.from_settings(get_settings().model_copy(update={"TWIN_MAX_INPLAY_QUOTE_AGE_SECONDS": None}))
    assert fortress.quote_age_limit(started, NOW, one_rule) == one_rule.max_quote_age_seconds


# ================================================================ the Parimatch skin inspects at 1xBet's real prices
@pytest.mark.asyncio
async def test_inspecting_at_parimatch_vets_the_slip_at_1xbets_real_prices(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    await seed(redis, settings, "fx-u130", "Liverpool", "Everton", sharp=PINNACLE_NO_ARB, soft={k: v for k, v in SOFT.items() if k != "parimatch"},
               kickoff=NOW + timedelta(hours=3))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(sessions, redis, settings, user)), base_url="http://test") as client:
        res = await client.post("/api/v1/manual-parlay/inspect", json={"leg_ids": ["fx-u130|Match Odds|HOME"], "kind": "SINGLE", "skin": "parimatch"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["skin"] == "parimatch" and body["book"] == "1xbet" and "vetted at 1xBet's real prices" in body["price_note"]
        assert "parimatch" not in body["audit"]["slip"]["legs"][0]["prices"]  # nothing is invented for Parimatch
        at_1xbet = (await client.post("/api/v1/manual-parlay/inspect", json={"leg_ids": ["fx-u130|Match Odds|HOME"], "kind": "SINGLE", "skin": "one_xbet"})).json()
        assert at_1xbet["price_note"] is None and at_1xbet["book"] == "1xbet"


@pytest.mark.asyncio
async def test_the_synthesizer_says_why_parimatch_has_no_price(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [SimpleNamespace(leg_id="f|Match Odds|HOME", fixture_id="f", kickoff=NOW + timedelta(hours=3), quotes={"1xbet": Quote("1xbet", 1.8, NOW)})]

    async def board(*_: Any) -> list[SimpleNamespace]:
        return rows

    monkeypatch.setattr(synth, "board", board)
    with pytest.raises(synth.SynthesisError) as caught:
        await synth.synthesize_for(None, get_settings(), NOW, stake=Decimal("250"), target_return=Decimal("5000"), book="parimatch")
    assert caught.value.reason == "BOOK_NOT_QUOTED" and "Synthesize at 1xBet" in caught.value.message


# ================================================================ the frontend, and the scanner untouched
def test_the_workbench_slip_is_universal_and_real_time() -> None:
    workbench = (FRONTEND / "components" / "parlay" / "ManualParlayWorkbench.tsx").read_text(encoding="utf-8")
    assert "<UniversalSlipPanel legs={slip} />" in workbench and "syncWithBoard(board.data)" in workbench and "inspection.price_note" in workbench
    assert 'estimate ? "≈" : ""' in workbench and "est.</span>" in workbench and "SOURCE_NOTE.from_1xbet" in workbench
    panel = (FRONTEND / "components" / "parlay" / "UniversalSlipPanel.tsx").read_text(encoding="utf-8")
    assert "universalSlip(legs, valid ? amount : 0)" in panel and "≈ = estimate, not the book's quote" in panel and "est.</span>" in panel
    assert "intervalMs: 5_000" in (FRONTEND / "lib" / "backtest_and_parlay.ts").read_text(encoding="utf-8")
    store = (FRONTEND / "store" / "useParlaySlipStore.ts").read_text(encoding="utf-8")
    assert "syncWithBoard: (board) => set(" in store
    for path in (ROOT / "backend" / "app" / "services" / "vision" / "slip_scanner_service.py", FRONTEND / "components" / "slip_scanner" / "SlipScannerModal.tsx",
                 FRONTEND / "lib" / "slip_scanner.ts"):
        text = path.read_text(encoding="utf-8")
        assert "universal_slip" not in text and "bookPrice" not in text  # the scanner reads the bookmaker's own slip
