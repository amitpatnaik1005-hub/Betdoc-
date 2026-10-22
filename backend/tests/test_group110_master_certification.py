"""Group 110: the master certification of the Groups 96-109 blueprint. Developed for Amit Ashok Kumar Patnaik.

The engines answer from their real entry points (the commander roster, the score predictor, the Slip Score rater, the
auto-fix, the Consultant's rules and verdicts, the Bet-Slip ticket, the two-currency engine's Section 115BBJ TDS, the
stop-loss triggers, the pre-match cadence), and the purged features leave nothing behind: their modules cannot be
imported, nothing imports them, and their screens are gone. The renamed homes of what survived them (the Lab's quant
toolkit, the wagering rules) are where the code imports them from.
"""

from __future__ import annotations

import importlib.util
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.domain.builder import recipe
from app.domain.consultant import diagnostics as dx
from app.domain.manual_parlay import cognitive_parlay_rater as rater
from app.domain.pre_match.cadence import tier_for
from app.domain.prediction import score_predictor as sp
from app.domain.risk.stop_loss import TRIGGER_CONSECUTIVE, TRIGGER_DAILY_LOSS, TRIGGER_SESSION_LOSS, TRIGGER_TRAILING
from app.domain.slip_ornament import ticket as tk
from app.domain.sports.registry import SportAdapterRegistry
from app.models.the_hive import LegendaryBot
from app.services.currency_service import CurrencyService
from app.services.slip_ornament import CHANNELS

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend" / "src"


# ================================================================ the engines
def test_master_commander_roster() -> None:
    """14 commanders since Group 103: GARUDA (96), ARJUNA (97) and ARYABHATA (103) retired."""
    roster = [m.value for m in LegendaryBot]
    assert len(roster) == 14 and LegendaryBot.ASHOKA.value == "ASHOKA"
    assert not {"ARYABHATA", "GARUDA", "ARJUNA"} & set(roster)


def test_master_flagship_score_prediction_engine() -> None:
    """Football scorelines from xG (exact Dixon-Coles), limited-overs innings from the ball-by-ball cricket engine."""
    football = sp.football_from_xg(1.8, 0.9, heatmap_goals=5, top=3)
    assert len(football.top_scorelines) == 3 and football.home_win > football.away_win and 0 < football.btts_yes < 1
    assert len(football.heatmap) == 6 and all(len(row) == 6 for row in football.heatmap)  # 0..5 goals a side
    cricket = sp.predict_cricket(SportAdapterRegistry.get_adapter("cricket"), {"batting_rating": 115.0}, {"batting_rating": 110.0},
                                 {"format": "t20"}, coverage=0.8)
    first = cricket.first_innings
    assert first.median >= 120 and first.powerplay_median is not None and len(first.interval) == 2 and first.interval[0] <= first.median <= first.interval[1]


def test_master_bet_builder_and_slip_rater() -> None:
    """The Slip Score's tiers, highest floor first, and where 95 / 80 / 60 / 40 land."""
    policy = rater.RaterPolicy.from_settings(get_settings())
    assert [name for name, _ in policy.tiers] == ["DIAMOND", "GOLD", "SILVER", "BRONZE"]
    assert [rater.tier_for(s, policy) for s in (95.0, 80.0, 60.0, 40.0)] == ["DIAMOND", "GOLD", "SILVER", "BRONZE"]


def test_master_chef_table_and_auto_fix() -> None:
    """The 1-click auto-fix replaces the weaker of two legs on one fixture (the Same-Match Tax) first."""
    legs = ["f1|Market 1|HOME", "f2|Market 2|AWAY", "f1|Market 3|OVER"]
    offender = recipe.choose_offender(legs, {legs[0]: 0.08, legs[1]: 0.05, legs[2]: -0.04}, {}, set(), False)
    assert offender is not None and offender[0] == legs[2] and "Same-Match" in offender[1]


def test_master_consultant_diagnostics_and_legal_rules() -> None:
    """The Lawyer's settlement rules, and the three verdicts a signed report can carry."""
    assert len(dx.RULES) >= 3 and all(rule.code == code for code, rule in dx.RULES.items())
    assert {v.value for v in dx.VerdictStatus} == {"APPROVED_GOLD", "APPROVED_WITH_CURE", "REJECTED"}


def test_master_slip_ornament_contracts() -> None:
    """The Bet-Slip ticket's book badges, its gold fallback, its number prefix, and the delivery channels."""
    assert tk.BOOK_BADGES["parimatch"] == ("#F8FF13", "#111111") and tk.BOOK_BADGES["1xbet"] == ("#007ACC", "#FFFFFF")
    assert tk.GOLD_BADGE == ("#C89B3C", "#1C1917") and tk.TICKET_PREFIX == "BD-"
    assert {"telegram", "sms", "email"} <= set(CHANNELS)


def test_master_two_currency_engine_and_indian_tds() -> None:
    """Section 115BBJ: 30% TDS withheld on the net winnings; the 4% cess on that tax makes 31.2% effective."""
    tds = CurrencyService().calculate_indian_tds(gross_win=10000.0, stake=2000.0)
    assert tds["net_profit"] == 8000.0 and tds["tds_deducted"] == 2400.0 and tds["tds_rate_pct"] == 30.0
    assert tds["cess"] == pytest.approx(96.0) and tds["effective_rate_pct"] == pytest.approx(31.2)
    assert tds["net_take_home"] == 7600.0  # the payout less the TDS withheld; the cess is due when the return is filed, not withheld


def test_master_stop_loss_risk_triggers() -> None:
    assert (TRIGGER_DAILY_LOSS, TRIGGER_CONSECUTIVE, TRIGGER_TRAILING, TRIGGER_SESSION_LOSS) == ("DAILY_LOSS", "CONSECUTIVE_LOSSES", "TRAILING_STOP", "SESSION_LOSS")


def test_master_pre_match_cadence_tiers() -> None:
    """Tier 1 far out (72h), tier 3 inside the last two hours."""
    settings = get_settings()
    assert tier_for(259_200.0, settings) == 1 and tier_for(7_200.0, settings) == 3
    assert tier_for(settings.PRE_MATCH_TIER1_KICKOFF_SECONDS - 1, settings) == 2


# ================================================================ zero dead code
PURGED_MODULES = ("app.domain.phantom", "app.api.v1.phantom", "app.domain.execution.paper_exchange", "app.models.paper_trading",
                  "app.api.v1.paper_trading", "app.services.execution_gateway", "app.domain.paper_trading")


@pytest.mark.parametrize("module", PURGED_MODULES)
def test_purged_modules_are_gone_and_nothing_imports_them(module: str) -> None:
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:  # its parent package is gone too
        spec = None
    assert spec is None, module
    pattern = re.compile(rf"(from|import)\s+{re.escape(module)}\b")
    offenders = [str(p.relative_to(BACKEND)) for root in ("app", "tests", "alembic") for p in (BACKEND / root).rglob("*.py")
                 if p.name != Path(__file__).name and pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == [], offenders


def test_what_survived_the_purges_lives_under_honest_names() -> None:
    """Group 96's quant toolkit and Group 80's wagering rules, renamed in Group 110."""
    from app.domain.quant_toolkit import QuantToolkitError, QuantToolkitManager  # noqa: PLC0415
    from app.domain.wagering.tax import TDS_RATE, section_115bbj  # noqa: PLC0415

    assert QuantToolkitManager and issubclass(QuantToolkitError, Exception) and TDS_RATE == Decimal("0.30")
    assert section_115bbj(Decimal("10000"), Decimal("2000")).tds == Decimal("2400.00")


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
@pytest.mark.parametrize("screen", ["Phantom", "PaperTradingSandbox", "LiveDesk", "ExecutionTerminal", "CurrencyConverter", "TradingModeToggle"])
def test_retired_screens_are_gone(screen: str) -> None:
    assert not list(FRONTEND.rglob(f"{screen}.tsx")), screen
    imports = [str(p) for p in FRONTEND.rglob("*.ts*") if re.search(rf"from\s+['\"][^'\"]*/{screen}['\"]", p.read_text(encoding="utf-8"))]
    assert imports == [], imports


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_component_modules_export_only_components() -> None:
    """React Fast Refresh: the shared UI modules keep their constants in their own files (Group 110)."""
    for path, moved in (("ui/kit.tsx", ("SPRING", "CARD_VARIANTS", "num")), ("ui/hero.tsx", ("MOTIFS",)), ("ui/brand.tsx", ("BRAND", "SURFACE"))):
        source = (FRONTEND / path).read_text(encoding="utf-8")
        for name in moved:
            assert not re.search(rf"export const {name}\b", source), f"{name} is exported from {path} again"
    for path in ("ui/motion.ts", "ui/num.ts", "ui/motifs.tsx", "ui/tokens.ts", "lib/commands.ts"):
        assert (FRONTEND / path).exists(), path


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
@pytest.mark.parametrize("page", sorted(p.name for p in (FRONTEND / "pages").glob("*.tsx")) if FRONTEND.exists() else [])
def test_every_page_opens_with_a_micro_guide(page: str) -> None:
    """Pillar 11: every section carries Group 99's guide: full width, exactly three steps, right under its hero."""
    content = (FRONTEND / "pages" / page).read_text(encoding="utf-8")
    blocks = [part[: part.index("/>")] for part in content.split("<ComponentGuideBanner")[1:]]
    assert blocks, f"{page} has no guide"
    for block in blocks:
        steps = block[block.index("steps={["): block.index("]}")]
        assert 'className="lg:col-span-12"' in block and len(re.findall(r'^\s+"(?:[^"\\]|\\.)*",?$', steps, re.M)) == 3, page
    if "<CommanderHero" in content:
        assert content.index("<CommanderHero") < content.index("<ComponentGuideBanner"), page
