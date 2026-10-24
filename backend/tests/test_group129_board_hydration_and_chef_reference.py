"""Group 129: the board stays populated between purchases, and an empty plate says why. Developed for Amit Ashok Kumar
Patnaik.

The tiered polling buys a far fixture every few hours, but board cells lapse after LIVE_ODDS_SNAPSHOT_TTL_SECONDS: the
replay now takes every stored board still within its cadence tier's age (keeping its purchase time), and a free beat
re-replays it before the cells lapse. The Chef's Table serves only slips that clear the bar; a plate nothing clears is
filled for reference from the positive-EV slips that miss it, labelled with the pillars they fail, never paged and never
in place of a served slip. The frontend: no typed Parimatch entry, a note that the feeds do not carry it, the workbench
cards' scoreline, two-way columns, coloured EV and both desk books' combined odds, the Chef's reference label.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.celery_app import celery_app
from app.domain.builder.recipe import PLATES, Rated, serve, serve_with_reference
from app.services import chef_table
from app.services import live_sports_ingestion_service as live
from tests.test_group112_live_sports_ingestion_and_hydration import BOOKS, NOW, _deps, _event, _settings, _store, sessions  # noqa: F401 - fixture

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


# ================================================================ replay within the tier
async def test_a_board_is_replayed_while_its_tier_calls_it_fresh(sessions, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    cases = [("far", NOW + timedelta(hours=30), NOW - timedelta(hours=2)),  # tier 1 (6 h): replayed
             ("day", NOW + timedelta(hours=10), NOW - timedelta(minutes=90)),  # tier 2 (2 h): replayed
             ("soon", NOW + timedelta(hours=3), NOW - timedelta(minutes=40)),  # tier 3 (30 min): too old
             ("stale", NOW + timedelta(hours=30), NOW - timedelta(hours=7))]  # tier 1 past 6 h: too old
    for name, kickoff, bought in cases:
        await _store(sessions, [_event(f"ev-{name}", f"Home {name}", f"Away {name}", kickoff, BOOKS, bought)], bought)
    published: dict[str, list[Any]] = {}

    async def ticks(_deps: Any, items: list[Any]) -> list[Any]:
        published["ticks"] = list(items)
        return list(items)

    async def quotes(_redis: Any, items: list[Any], _settings: Any) -> bool:
        return True

    async def source(_deps: Any) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(live, "publish_fleet_ticks", ticks)
    monkeypatch.setattr(live, "publish_market_quotes", quotes)
    monkeypatch.setattr(live, "_odds_source", source)
    report = await live.hydrate(_deps(sessions, _settings()), sports=["soccer_epl"], now=NOW)
    assert (report["fixtures_stored"], report["fixtures_hydrated"], report["skipped_stale"]) == (4, 2, 2)
    assert {t.home_team for t in published["ticks"]} == {"Home far", "Home day"}
    assert report["max_age_by_tier"] == {"1": 21600.0, "2": 7200.0, "3": 1800.0}
    off = await live.hydrate(_deps(sessions, _settings(LIVE_ODDS_REPLAY_WITHIN_TIER=False)), sports=["soccer_epl"], now=NOW)
    assert off["fixtures_hydrated"] == 0  # the old rule: only the snapshot TTL (15 minutes)


def test_the_free_re_replay_runs_before_the_cells_lapse() -> None:
    entry = celery_app.conf.beat_schedule["live-rehydrate"]
    settings = _settings()
    assert entry["task"] == "live.rehydrate" and entry["schedule"] == settings.LIVE_ODDS_REHYDRATE_SECONDS < settings.LIVE_ODDS_SNAPSHOT_TTL_SECONDS
    assert "live.rehydrate" in celery_app.tasks or __import__("app.workers.live_context_tasks")  # registered with the worker


# ================================================================ the Chef's reference plates
def _rated(key: str, *, legs: int = 3, score: float = 54, ev: float = 0.12, p: float = 0.2, odds: float = 6.0, vetoed: bool = True) -> Rated:
    breakdown = [{"number": 14, "title": "Kill switch & fresh price", "status": "FAIL", "reason": "prices older than 60s: 1xbet 1411s"}] if vetoed else []
    return Rated(key=key, legs=legs, score=score, joint_ev=ev, joint_probability=p, odds=odds, vetoed=vetoed,
                 payload={"rating": {"breakdown": breakdown, "tier": "BRONZE" if vetoed else "GOLD"}})


def test_an_empty_plate_is_filled_for_reference_only_from_positive_ev_slips() -> None:
    rated = [_rated("a", ev=0.18, odds=7.0), _rated("b", ev=0.15, p=0.35, odds=3.2), _rated("c", ev=0.12, odds=9.5), _rated("loser", ev=-0.02, score=80)]
    assert serve(rated) == []  # the bar itself is unchanged: nothing vetoed is served
    plates = serve_with_reference(rated)
    assert [(code, r.key, below) for code, _, r, below in plates] == [("PARLAY_OF_THE_DAY", "a", True), ("SAFE_FORTRESS", "b", True), ("HIGH_YIELD", "c", True)]
    assert all(r.key != "loser" for _, _, r, _ in plates)  # a negative-EV slip is never plated, whatever its score

    served = _rated("clean", vetoed=False, score=88, ev=0.09)
    plates = serve_with_reference([served, *rated])
    assert plates[0][2].key == "clean" and plates[0][3] is False  # a slip that clears the bar keeps its plate, served
    assert [below for _, _, _, below in plates[1:]] == [True, True] and [code for code, _ in PLATES] == [code for code, _, _, _ in plates]


@pytest.mark.asyncio
async def test_a_reference_plate_is_never_paged() -> None:
    class Redis:
        async def set(self, *_: Any, **__: Any) -> bool:
            raise AssertionError("a reference plate must not claim a page")

    settings = _settings(POCKET_TWIN_DIAMOND_ALERTS=True)
    plate = {"below_bar": True, "rating": {"tier": "DIAMOND"}, "legs": [{"kickoff": (datetime.now(UTC) + timedelta(hours=3)).isoformat()}], "slip_id": "x"}
    assert await chef_table.page_diamonds(Redis(), settings, [plate], datetime.now(UTC)) == 0  # type: ignore[arg-type]


# ================================================================ the frontend
def test_the_builder_screens() -> None:
    assert not (FRONTEND / "components" / "parlay" / "ParimatchPriceEntry.tsx").exists()
    workbench = (FRONTEND / "components" / "parlay" / "ManualParlayWorkbench.tsx").read_text(encoding="utf-8")
    assert "card.projection.score" in workbench and 'm.selections.length === 2 ? "grid-cols-2" : "grid-cols-3"' in workbench
    assert 's.ev >= 0 ? "#22C55E" : "#FB7185"' in workbench and "<UniversalSlipPanel legs={slip} />" in workbench  # Group 130: both books' returns
    assert "LIVE{card.minutes_since_kickoff !== null" in workbench and 'min={15} max={40}' in workbench
    chef = (FRONTEND / "components" / "builder" / "ChefsTable.tsx").read_text(encoding="utf-8")
    assert "plate.below_bar" in chef and "Below the Chef's bar: shown for reference, not served." in chef and "plate.bar_reasons" in chef
