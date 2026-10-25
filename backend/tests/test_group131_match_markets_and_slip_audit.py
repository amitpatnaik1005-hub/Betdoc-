"""Group 131: inside the match, one pick per market, the slip audit, longer auto parlays, plain names. Developed for Amit
Ashok Kumar Patnaik.

Auto Parlays builds straight multiples of 4 to ASHOKA_MAX_AUTO_MULTIPLE_LEGS legs from the best ASHOKA_LONG_MULTIPLE_POOL
fixtures (one leg each), under the same EV and probability bars. The manual bet builder opens a fixture's every market
(quoted ones, and for three-way football the BetDoc model's lines no feed quotes, labelled and never inspected as
quotes); the slip keeps one pick per market; the advisor reads the slip's own figures; the audit shows "—" for what it
did not measure. The engines' names stay off the screen while the API keeps them for the settlement feedback; the
smallcase ticker, panel and rebalancer and the Arena's arbitrage scan are gone; the lore build string is retired.
The slip scanner is untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.oracle.parlay_engine import ParlayEngine, SlipKind
from app.models.control_panel import SystemSettingsModel
from tests.test_ashoka_oracle import NOW, market_leg, thresholds

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"


def _read(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def _node(module: str, expression: str) -> object:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    uri = (FRONTEND / "lib" / module).as_uri()
    run = subprocess.run(["node", "--experimental-strip-types", "--no-warnings", "-e", f"import({json.dumps(uri)}).then((m) => console.log(JSON.stringify({expression})))"],
                         capture_output=True, text=True, timeout=60)
    if run.returncode != 0 and "experimental-strip-types" in run.stderr:
        pytest.skip("this node cannot run TypeScript")
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# ================================================================ Auto Parlays: 4 to 6 legs, from a bounded pool
def _board(n: int) -> list:
    # twelve independent fixtures, each one likely leg (92%) a little over the book's price; fx-00 the best EV
    return [market_leg(f"fx-{i:02d}", 0.92, {"1xbet": round(1.17 - i * 0.002, 3)}) for i in range(n)]


def test_auto_parlays_build_accumulators_of_up_to_six_legs_from_the_best_fixtures() -> None:
    legs = _board(12)
    long = ParlayEngine(thresholds(), paths=10_000, seed=7, max_multiple=6, long_pool=8).generate(legs, NOW)
    sizes = sorted(len(s.legs) for s in long.slips if s.kind is SlipKind.ACCUMULATOR)
    assert sizes == [4, 5, 6]  # the best multiple of each size only: compounding EV must not crowd out the rest
    assert {SlipKind.SINGLE, SlipKind.DOUBLE, SlipKind.TREBLE} & {s.kind for s in long.slips} and long.rejected["a better multiple of its size"] > 0
    top8 = {f"fx-{i:02d}" for i in range(8)}
    for s in long.slips:
        if s.kind is SlipKind.ACCUMULATOR:
            assert {leg.fixture_id for leg in s.legs} <= top8 and len({leg.fixture_id for leg in s.legs}) == len(s.legs)  # one leg a fixture
            assert s.sim.joint_ev >= 0.075  # the bars are unchanged
    short = ParlayEngine(thresholds(), paths=10_000, seed=7).generate(legs, NOW)  # the engine's own default: trebles at most
    assert all(len(s.legs) <= 3 for s in short.slips if s.kind is SlipKind.ACCUMULATOR or s.kind in (SlipKind.DOUBLE, SlipKind.TREBLE))
    assert not [s for s in short.slips if s.kind is SlipKind.ACCUMULATOR]


def test_the_shipped_settings_and_the_retired_lore_string() -> None:
    fields = Settings.model_fields
    assert fields["ASHOKA_MAX_AUTO_MULTIPLE_LEGS"].default == 6 and fields["ASHOKA_LONG_MULTIPLE_POOL"].default == 8
    assert SystemSettingsModel.__table__.c.build_info.default.arg == "Production Risk & Intelligence Engine"
    panel = _read("pages", "ControlPanel.tsx")
    assert "/OMEGA|LEVIATHAN/i.test(s.build_info) ? 'Production Risk & Intelligence Engine'" in panel  # a row stored before stays off the hero


# ================================================================ the slip: one pick per market (the real rule, through node)
def test_one_pick_per_market() -> None:
    a = {"leg_id": "fx|Match Odds|HOME"}
    out = _node("slip_rules.ts", "(() => { const away = {leg_id: 'fx|Match Odds|AWAY'}, over = {leg_id: 'fx|Totals 2.5|OVER'}, home = {leg_id: 'fx|Match Odds|HOME'};"
                "const one = m.pick([home], away, true); const two = m.pick(one.legs, over, true); const again = m.pick(two.legs, over, true);"
                "const kept = m.pick(two.legs, over, false); return {one, two, again, kept, market: m.marketOf('fx|Totals 2.5|OVER')}; })()")
    assert out["one"] == {"legs": [{"leg_id": "fx|Match Odds|AWAY"}], "result": {"action": "replaced", "replaced": a}}  # Away replaces Home
    assert [leg["leg_id"] for leg in out["two"]["legs"]] == ["fx|Match Odds|AWAY", "fx|Totals 2.5|OVER"]  # another market of the fixture stays
    assert out["again"]["result"]["action"] == "removed" and len(out["again"]["legs"]) == 1  # clicked again: off the slip
    assert out["kept"]["result"]["action"] == "kept" and len(out["kept"]["legs"]) == 2  # added again (a drop): no duplicate
    assert out["market"] == "fx|Totals 2.5"
    store = _read("store", "useParlaySlipStore.ts")
    assert 'from "../lib/slip_rules"' in store and "pick(get().legs, leg, true)" in store and "announcePick" in store
    assert "announcePick(toggle(leg), leg)" in _read("components", "oracle", "board", "MatchBoard.tsx")


# ================================================================ inside the match, the advisor, the audit
def test_the_match_sheet_and_the_workbench() -> None:
    modal = _read("components", "parlay", "MatchMarketModal.tsx")
    assert "predictFootballFromMarket(targets)" in modal and "BetDoc model · not quoted" in modal and 'role="dialog"' in modal
    assert "prices: {}, fair_probability: 1 / (p.fair_odds as number)" in modal  # a model leg carries no quote, ever
    assert 'e.key === "Escape" && onClose()' in modal and "aria-pressed={picked}" in modal
    workbench = _read("components", "parlay", "ManualParlayWorkbench.tsx")
    assert "<MatchMarketModal card={insideFor}" in workbench and "Go inside the match · all markets" in workbench and "onOpen={() => setInsideId(card.fixture_id)}" in workbench
    assert "disabled={!slip.length || !chosen || modelLegs.length > 0}" in workbench  # model legs are filled at the book, not inspected as quotes
    assert "const advise = (slip: SlipLeg[]" in workbench and "Zero conflicting correlations" not in workbench  # no canned claim
    assert "figures={inspection.audit}" in workbench and "figures={inspection.audit}" in _read("components", "oracle", "board", "SlipRail.tsx")
    meter = _read("components", "builder", "SlipScoreMeter.tsx")
    assert 'passed === null ? "—" : `${passed} / 15`' in meter and "15 / 15" not in meter.replace("`${passed} / 15`", "")  # never a default 15/15
    assert "figures={{ joint_probability: plate.joint_probability" in _read("components", "builder", "ChefsTable.tsx")
    hero = _read("components", "prediction", "ScorePredictionPanel.tsx")
    assert "<ProjectedScoreHero p={result.p}" in hero and "38 de-vigged" not in hero


# ================================================================ plain names on the screen
def test_the_engines_names_stay_off_the_screen() -> None:
    out = _node("model_privacy.ts", "m.publicRationale('fair 52.0%; 1xBet 2.1 implies 47.6%; EV +9.2%; models dixon-coles 52%, poisson 50%, market 47%')")
    assert out == "fair 52.0%; 1xBet 2.1 implies 47.6%; EV +9.2%"
    assert "publicRationale(leg.rationale)" in _read("components", "oracle", "AshokaDesk.tsx")
    shown = {
        ("components", "parlay", "ManualParlayWorkbench.tsx"): ("models ${Object.entries", "Dixon-Coles matrix"),
        ("components", "builder", "ChefsTable.tsx"): ("The models:",),
        ("components", "prediction", "ScoreBackedLegs.tsx"): ("Dixon-Coles fitted",),
        ("components", "prediction", "ScorePredictionPanel.tsx"): ("Dixon-Coles rates", '"Dixon-Coles on'),
        ("components", "sports", "UniversalSportSelector.tsx"): ("Model: {current.modelType}",),
        ("pages", "TheLab.tsx"): ("Poisson 30% · Dixon-Coles 40%",),
        ("pages", "TheOracle.tsx"): ("Poisson + Dixon-Coles + Elo",),
    }
    for parts, gone in shown.items():
        text = _read(*parts)
        assert not [g for g in gone if g in text], parts


def test_smallcases_and_arbitrage_are_off_the_screen() -> None:
    assert "<SmallcaseSlipRebalancer" not in _read("components", "builder", "BetBuilder.tsx")
    assert "<LiveSmallcaseTickerBar" not in _read("pages", "TheOracle.tsx") + _read("pages", "TheVault.tsx")
    assert "<SmallcaseComponentsPanel" not in _read("pages", "TheVault.tsx")
    for page, label in (("CommandCenter.tsx", "Active portfolios"), ("TheArchive.tsx", "Strategy model")):
        assert label in _read("pages", page)
    arena = _read("pages", "TheArena.tsx")
    assert "Scan arbitrage" not in arena and "/signals/surebets" not in arena and "Scan steam" in arena
    assert 'name: "THE VAULT & CAPITAL LEDGER"' in _read("lib", "neural_bus.ts")


def test_the_obsidian_skins_and_the_untouched_scanner() -> None:
    themes = _read("lib", "backtest_and_parlay.ts")
    assert 'const OBSIDIAN = { surface: "#0B0F17", card: "#131B2E", text: "#F8FAFC"' in themes and themes.count("...OBSIDIAN") == 4
    for path in (ROOT / "backend" / "app" / "services" / "vision" / "slip_scanner_service.py", FRONTEND / "components" / "slip_scanner" / "SlipScannerModal.tsx",
                 FRONTEND / "lib" / "slip_scanner.ts"):
        text = path.read_text(encoding="utf-8")
        assert "Group 131" not in text and "MatchMarketModal" not in text and "slip_rules" not in text
