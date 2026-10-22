"""Group 99: the micro-guides and the KPI glossary exist, are wired into the three core pages, and every glossary
example's arithmetic is right (a guide that teaches a wrong number is worse than none).

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
GLOSSARY = FRONTEND / "ui" / "kpi_glossary.ts"
KIT = FRONTEND / "ui" / "kit.tsx"
REQUIRED = ("EV", "OVERROUND", "SLIP_SCORE", "KELLY_STAKE", "BRIER_SCORE", "CLV")
pytestmark = pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")


def _glossary() -> str:
    return GLOSSARY.read_text(encoding="utf-8")


def test_kpi_glossary_is_complete() -> None:
    content = _glossary()
    for kpi in REQUIRED:
        assert re.search(rf"^\s*{kpi}: \{{", content, re.M), f"missing KPI {kpi}"
        assert f'code: "{kpi}"' in content


def test_glossary_makes_no_claims_about_real_results() -> None:
    content = _glossary()
    assert "BetDoc Brier Score" not in content and "Parimatch overround" not in content  # examples, not claims
    assert "15 pillars" in content  # Group 104: the Slip Score is the fortress's 15 pillars plus measured badges


def test_glossary_examples_check_out() -> None:
    assert round(0.50 * 2.20 - 1, 4) == 0.10  # EV: +10%
    assert round(2 / 1.90 * 100, 2) == 105.26  # overround of 1.90 / 1.90
    full_kelly = 0.06 / (2.10 - 1)
    assert round(full_kelly * 100, 2) == 5.45 and round(10_000 * full_kelly / 4) == 136  # quarter Kelly on ₹10,000
    assert round((1 - 0.58) ** 2, 3) == 0.176  # Brier of a 58% forecast that came in
    assert round((2.20 / 1.95 - 1) * 100, 1) == 12.8  # CLV
    fortress = round(100 * (12 + 3 * 0.25) / 15)
    assert fortress == 85 and fortress + 8 + 5 == 98 and fortress - 18 + 5 == 72  # Slip Score (Group 104): 98 DIAMOND, 72 SILVER
    content = _glossary()
    for text in ("+10% EV", "105.26%", "about ₹136", "= 0.176", "+12.8% CLV", "= 85", "98, Diamond Fortress", "72, Silver Caution"):
        assert text in content, text


def test_kit_exports_the_micro_guide_components() -> None:
    content = KIT.read_text(encoding="utf-8")
    assert "export const ComponentGuideBanner" in content and "export const KpiGlossaryPill" in content
    assert 'from "./kpi_glossary"' in content


@pytest.mark.parametrize(("page", "kpis"), [
    ("TheOracle.tsx", ("EV", "SLIP_SCORE", "BRIER_SCORE")),
    ("TheLab.tsx", ("EV", "OVERROUND")),
    ("TheVault.tsx", ("KELLY_STAKE", "EV")),
    ("TheConsultant.tsx", ("SLIP_SCORE", "EV", "OVERROUND", "KELLY_STAKE")),  # Group 105
])
def test_core_pages_open_with_a_guide(page: str, kpis: tuple[str, ...]) -> None:
    content = (FRONTEND / "pages" / page).read_text(encoding="utf-8")
    # A page may hold one guide per tab (The Oracle since Group 103): each is full width with exactly three steps, and
    # together they cover the page's key figures
    blocks = []
    for part in content.split("<ComponentGuideBanner")[1:]:
        blocks.append(part[:part.index("/>")])
    assert blocks, f"{page} has no guide"
    for block in blocks:
        assert 'className="lg:col-span-12"' in block
        steps = block[block.index("steps={["):block.index("]}")]
        assert len(re.findall(r'^\s+"(?:[^"\\]|\\.)*",?$', steps, re.M)) == 3  # exactly three steps (escaped quotes allowed)
    for kpi in kpis:
        assert any(f'"{kpi}"' in block for block in blocks), kpi
    assert content.index("<CommanderHero") < content.index("<ComponentGuideBanner")  # right under the hero
