"""Group 111: the permanent BetDoc UI and the official pure-white Bet-Slip.

The ticket is white paper with a gold strip, BetDoc's desk beside the book's badge, notches and a perforation, the legs,
the money grid (Kelly stake, gross return, the net after Section 194BA's TDS, the stop-loss floor), a Code 39 barcode
that scans back to the ticket number and the developer's watermark. The page guide is a one-line ribbon whose "Guide"
opens the three steps in a modal. The Oracle opens on the work: a one-line desk strip, four tabs (Match board, Bet
builder, Score engine, Quant desk) and the Chef's Table's parlay of the day above the first match cards. The rail holds
six sections (the rest under More, all twelve in the command palette), 56px collapsed, under a 48px header.
Developed for Amit Ashok Kumar Patnaik.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.domain.slip_ornament.ticket import TICKET_PREFIX

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
pytestmark = pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")


def read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- the barcode
# Code 39 as published (ISO/IEC 16388): each character's 12 modules with a wide element two modules, 1 = bar, 0 = space.
# An independent table, so the module's own table is checked rather than echoed.
CODE39_REFERENCE = {
    "*": "100101101101", "0": "101001101101", "1": "110100101011", "2": "101100101011", "3": "110110010101",
    "4": "101001101011", "5": "110100110101", "6": "101100110101", "7": "101001011011", "8": "110100101101",
    "9": "101100101101", "A": "110101001011", "B": "101101001011", "C": "110110100101", "D": "101011001011",
    "E": "110101100101", "F": "101101100101", "G": "101010011011", "H": "110101001101", "I": "101101001101",
    "J": "101011001101", "K": "110101010011", "L": "101101010011", "M": "110110101001", "N": "101011010011",
    "O": "110101101001", "P": "101101101001", "Q": "101010110011", "R": "110101011001", "S": "101101011001",
    "T": "101011011001", "U": "110010101011", "V": "100110101011", "W": "110011010101", "X": "100101101011",
    "Y": "110010110101", "Z": "100110110101", "-": "100101011011", ".": "110010101101", " ": "100110101101",
}


def modules(pattern: str) -> str:
    """'nnnwwnwnn' (bar, space, bar, …) as modules with a wide element two modules wide."""
    return "".join(("1" if i % 2 == 0 else "0") * (2 if kind == "w" else 1) for i, kind in enumerate(pattern))


def test_the_barcode_table_is_code_39() -> None:
    source = read("lib/barcode.ts")
    table = dict(re.findall(r'"?([0-9A-Z*.\- ])"?: "([nw]{9})"', source[source.index("const PATTERNS"):source.index("};")]))
    assert set(table) == set(CODE39_REFERENCE)
    for char, pattern in table.items():
        assert pattern.count("w") == 3 and modules(pattern) == CODE39_REFERENCE[char], char


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_ticket_number_scans_back() -> None:
    """The bars the ticket draws decode, with the published table, to the ticket number between the start/stop stars."""
    ticket_no = TICKET_PREFIX + uuid.uuid4().hex[:10].upper()
    module = (FRONTEND / "lib" / "barcode.ts").as_uri()
    script = f"import({json.dumps(module)}).then((m) => console.log(JSON.stringify([m.code39({json.dumps(ticket_no)}), m.code39('bd-ü'), m.code39('bd-1a')])))"
    run = subprocess.run(["node", "--experimental-strip-types", "--no-warnings", "-e", script], capture_output=True, text=True, timeout=60)
    if run.returncode != 0 and "experimental-strip-types" in run.stderr:
        pytest.skip("this node cannot run TypeScript")
    assert run.returncode == 0, run.stderr
    code, unknown, lower = json.loads(run.stdout)
    assert unknown is None and lower is not None  # no Code 39 form → no barcode; lower case is upper-cased, as scanners read it

    # read the bars as a scanner does: the widths of the bars and the gaps between them, wide being above 1.5 modules
    bars = code["bars"]
    elements: list[float] = []
    for i, bar in enumerate(bars):
        if i:
            elements.append(bar["x"] - (bars[i - 1]["x"] + bars[i - 1]["width"]))
        elements.append(bar["width"])
    assert bars[-1]["x"] + bars[-1]["width"] == pytest.approx(code["width"])
    by_pattern = {v: k for k, v in CODE39_REFERENCE.items()}
    decoded = []
    for start in range(0, len(elements), 10):  # nine elements a character, then the narrow gap between characters
        chunk = elements[start:start + 9]
        if start + 9 < len(elements):
            assert elements[start + 9] == pytest.approx(1)
        pattern = "".join("w" if e > 1.5 else "n" for e in chunk)
        decoded.append(by_pattern[modules(pattern)])
    assert "".join(decoded) == f"*{ticket_no}*"


# ---------------------------------------------------------------- the ticket
def test_the_ticket_is_white_paper_with_the_official_layout() -> None:
    card = read("components/slip_ornament/LuxuryBetCardOrnament.tsx")
    article = card[card.index('<article id="betdoc-ticket"'):]
    article_class = re.search(r'className="([^"]+)"', article).group(1)  # type: ignore[union-attr]
    assert "bg-white" in article_class and "dark:bg" not in article_class  # paper in either theme
    for part in ("BetDoc official Bet-Slip", "BETDOC QUANTITATIVE DESK", "border-dashed", "code39(", "at signing",
                 '"Kelly stake"', '"Gross return"', "Net after ${tdsRate}% TDS", '"Stop-loss floor"', "Section 194BA",
                 "Official Quantitative Slip · Developed for {ticket.architect || \"Amit Ashok Kumar Patnaik\"}"):
        assert part in card, part
    assert "dark:bg-[#0c0a09]" in card  # the notches are the page behind the ticket, in either theme
    assert "<BetdocLogo" in card and "PARIMATCH" in card and "1xBET" in card  # BetDoc's logo beside the book's own badge
    page = read("pages/TheBetSlip.tsx")
    assert "<CommanderHero" not in page and "Official Bet-Slip" in page and "re-check prices" in page
    app = read("App.tsx")
    assert "{pathname === '/wire' && <NeuralPipelineBar />}" in app  # the pipeline bar rides on The Wire only
    poster = read("lib/slip_ornament.ts")
    assert 'paper: "#FFFFFF"' in poster and "ivory" not in poster and "barcodeSvg(t.ticket_no" in poster
    assert "BETDOC OFFICIAL BET-SLIP" in poster and "NET AFTER ${t.tds_rate_pct ?? 30}% TDS" in poster and "GROSS RETURN" in poster
    assert "Official Quantitative Slip · Developed for" in poster


# ---------------------------------------------------------------- the ribbon and its modal
def test_the_page_guide_is_a_ribbon_with_a_modal() -> None:
    kit = read("ui/kit.tsx")
    banner = kit[kit.index("export const ComponentGuideBanner"):]
    assert "steps: [string, string, string]" in kit  # three steps, held by the type as well as by Group 99's test
    assert "min-h-9" in banner and 'aria-haspopup="dialog"' in banner
    assert "createPortal(" in kit and 'role="dialog"' in kit and 'aria-modal="true"' in kit and '"Escape"' in kit
    assert "dense ? " in kit  # the Oracle's tighter page rhythm


# ---------------------------------------------------------------- The Oracle
def test_the_oracle_opens_on_the_work() -> None:
    oracle = read("pages/TheOracle.tsx")
    tabs = re.findall(r"\{ value: '(\w+)', label: '([^']+)', icon: '(\w+)' \}", oracle[oracle.index("const ORACLE_TABS"):])
    assert [(v, label) for v, label, _ in tabs[:4]] == [("board", "Match board"), ("builder", "Bet builder"), ("score", "Score engine"), ("desk", "Vetting desk")]  # Group 131: renamed
    assert "v === 'engine' ? 'score'" in oracle  # Group 103's ?tab=engine links (and /sports) still land
    assert "<CommanderHero" not in oracle and "<Page dense>" in oracle and "ASHOKA · The Oracle" in oracle
    assert "<LiveSmallcaseTickerBar" not in oracle and "<InfinityFlexiViewToggle" in oracle  # Group 131: the basket chips are retired
    board = oracle[oracle.index("{tab === 'board' &&"):oracle.index("{tab === 'builder' &&")]
    assert board.index("<ChefTopPlate") < board.index("<MatchBoard")  # the parlay of the day above the first match cards
    match_board = read("components/oracle/board/MatchBoard.tsx")
    assert match_board.index("<MatchRow") < match_board.index("<TopParlays")  # the fixtures first, then Ashoka's top parlays
    ticker = read("components/smallcase/LiveSmallcaseTickerBar.tsx")
    assert "compact ? (" in ticker and "onClick={() => setOpen(b.id)}" in ticker  # a chip opens the inspector, where Load lives
    score = oracle[oracle.index("{tab === 'score' &&"):oracle.index("{tab === 'desk' &&")]
    assert "<ScorePredictionPanel" in score and "<SportsEngineDesk" in score
    assert "useMatchBoard(null, 'all')" in oracle  # the strip's sports and fixtures are the board's own count
    plate = read("components/oracle/board/ChefTopPlate.tsx")
    assert "useChefTable(" in plate and "<ConfirmButton" in plate and "/oracle?tab=builder&mode=manual" in plate
    assert "TIER_STYLE[plate.rating.tier]" in plate and "plate.joint_ev" in plate


# ---------------------------------------------------------------- the rail and the header
def nav_targets(app: str, name: str) -> list[str]:
    block = app[app.index(f"const {name}: readonly NavItem[] = ["):]
    return re.findall(r"\{ to: '([^']+)'", block[:block.index("];")])


def test_the_rail_holds_six_sections_and_reaches_all() -> None:
    app = read("App.tsx")
    primary, more = nav_targets(app, "PRIMARY_NAV"), nav_targets(app, "MORE_NAV")
    assert primary == ["/oracle", "/consultant", "/bet-slip", "/wire", "/vault", "/control-panel"]
    assert sorted(more) == ["/archive", "/arena", "/command-center", "/core", "/hive", "/lab"]
    assert "const NAV_ITEMS: readonly NavItem[] = [...PRIMARY_NAV, ...MORE_NAV];" in app and "useNavigateCommands(NAV_ITEMS)" in app
    for to in primary + more:
        assert f'path="{to}"' in app, to
    assert "{ to: '/control-panel',  label: 'Settings'" in app
    assert "collapsed ? 'absolute right-1.5 top-1.5' : 'relative'" in app  # the status dot leaves the flow, so rail icons centre
    assert "isLeftCollapsed ? 56 : 248" in app and "min-h-[48px]" in app and "min-h-[56px]" not in app
    assert 'aria-controls="rail-more"' in app and '<NavLink to="/command-center"' in app
    assert "isLeftCollapsed: true," in read("store/useUIStore.ts")


def test_score_prediction_opens_on_the_score_engine_tab() -> None:
    nav = read("components/layout/MobileBottomNav.tsx")
    assert 'to: "/oracle?tab=score"' in nav and "/lab#score-prediction" not in nav
    manifest = json.loads((FRONTEND.parent / "public" / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert {"name": "Score Prediction", "url": "/oracle?tab=score"}.items() <= next(s for s in manifest["shortcuts"] if s["name"] == "Score Prediction").items()


# ---------------------------------------------------------------- honest, finished sources
G111_SOURCES = ("ui/kit.tsx", "lib/barcode.ts", "lib/slip_ornament.ts", "components/slip_ornament/LuxuryBetCardOrnament.tsx",
                "components/oracle/board/ChefTopPlate.tsx", "pages/TheOracle.tsx", "App.tsx", "components/layout/MobileBottomNav.tsx")


@pytest.mark.parametrize("rel", G111_SOURCES)
def test_no_placeholders_or_stock_figures(rel: str) -> None:
    source = read(rel)
    assert not re.search(r"\bTODO\b|\bFIXME\b|lorem ipsum|coming soon|>\s*\.\.\.\s*<", source, re.I), rel  # finished, not stubbed
    assert "lucide-react" not in source  # Material Symbols only
    assert not re.search(r"94/100|\+11\.8%|9 Sports Active|Arsenal ML|India v Australia|/\s*84(\.0+)?\b", source), rel  # real figures only
