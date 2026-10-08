"""Phase 2: canonical names -> BetDoc UUIDs, any price format -> true probability, MarketTick out."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.adapters.ingestion.base import IngestionBatch, SourcePayload
from app.schemas.market import MarketTick
from app.services.omni_normalizer import (
    ALIAS_DICTIONARY_PATH,
    AliasDictionary,
    CanonicalRef,
    OmniNormalizer,
    QuorumConsensusEngine,
    QuorumPolicy,
    SportSpec,
    default_alias_dictionary,
    implied_probability,
    merge_board_tick,
    normalize_name,
    remove_vig,
)

from .conftest import KICKOFF, odds_api_epl, polymarket_epl_markets, polymarket_nfl_markets


def _batch(source_id: str, payloads: list[SourcePayload]) -> IngestionBatch:
    return IngestionBatch(source_id=source_id, payloads=payloads, fetched_at=datetime.now(UTC), latency_ms=40, requests=1, retries=0)


# ---------------------------------------------------------------- alias dictionary
def test_dictionary_ids_are_unique_and_frozen() -> None:
    doc = json.loads(Path(ALIAS_DICTIONARY_PATH).read_text(encoding="utf-8"))
    ids = [row["id"] for row in doc["entities"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 90
    assert all(UUID(i).version == 5 for i in ids)
    assert {"soccer_epl", "americanfootball_nfl", "basketball_nba"} <= set(doc["sports"])


@pytest.mark.parametrize(
    ("sport", "external", "canonical"),
    [
        ("soccer_epl", "Arsenal FC", "Arsenal"),
        ("soccer_epl", "Brighton & Hove Albion FC", "Brighton & Hove Albion"),
        ("soccer_epl", "Brighton and Hove Albion", "Brighton & Hove Albion"),
        ("soccer_epl", "Nott'm Forest", "Nottingham Forest"),
        ("soccer_epl", "AFC Bournemouth", "AFC Bournemouth"),
        ("soccer_epl", "Bournemouth", "AFC Bournemouth"),
        ("americanfootball_nfl", "Buccaneers", "Tampa Bay Buccaneers"),
        ("basketball_nba", "Sixers", "Philadelphia 76ers"),
    ],
)
def test_external_names_resolve_to_canonical_entities(sport: str, external: str, canonical: str) -> None:
    resolved = default_alias_dictionary().resolve(sport, external)
    assert resolved.canonical and resolved.name == canonical


def test_aliases_are_scoped_per_sport() -> None:
    aliases = default_alias_dictionary()
    assert aliases.resolve("soccer_epl", "Spurs").name == "Tottenham Hotspur"
    assert aliases.resolve("basketball_nba", "Spurs").name == "San Antonio Spurs"


def test_unknown_name_gets_a_stable_provisional_id() -> None:
    aliases = default_alias_dictionary()
    first, second = aliases.resolve("soccer_epl", "Real Madrid CF"), aliases.resolve("soccer_epl", "real madrid")
    assert not first.canonical and first.id == second.id


def test_shared_alias_resolves_to_neither_entity() -> None:
    sports = {"x": SportSpec("x", "X", None, False)}
    a, b = CanonicalRef(uuid4(), "team", "x", "Alpha United", ("United",)), CanonicalRef(uuid4(), "team", "x", "Beta United", ("United",))
    aliases = AliasDictionary(uuid4(), sports, [a, b])
    assert not aliases.resolve("x", "United").canonical
    assert aliases.resolve("x", "Alpha United").id == a.id


def test_name_normalisation() -> None:
    assert normalize_name("  Atlético  Madrid ") == "atletico madrid"
    assert normalize_name("Brighton & Hove") == "brighton and hove"


# ---------------------------------------------------------------- probability
@pytest.mark.parametrize(
    ("price", "fmt", "expected"),
    [(2.5, "decimal", 0.4), (-150, "american", 0.6), (150, "american", 0.4), ("5/2", "fractional", 1 / 3.5), (0.42, "probability", 0.42)],
)
def test_every_format_becomes_an_implied_probability(price, fmt, expected) -> None:
    assert implied_probability(price, fmt) == pytest.approx(expected)


@pytest.mark.parametrize(("price", "fmt"), [(1.0, "decimal"), (0.5, "decimal"), (50, "american"), ("1/0", "fractional"), (1.2, "probability"), ("x", "decimal")])
def test_impossible_prices_are_rejected(price, fmt) -> None:
    with pytest.raises(ValueError):
        implied_probability(price, fmt)


def test_remove_vig_scales_a_book_to_one() -> None:
    fair = remove_vig([1 / 1.9, 1 / 1.9])
    assert fair == pytest.approx([0.5, 0.5]) and math.fsum(fair) == pytest.approx(1.0)


# ---------------------------------------------------------------- Odds API
def test_odds_api_event_becomes_three_canonical_ticks() -> None:
    report = OmniNormalizer().normalize(_batch("odds_api", [SourcePayload("soccer_epl", odds_api_epl())]))
    assert report.events_seen == report.events_normalized == 1 and not report.unmapped
    ticks = {t.selection: t for t in report.ticks}
    assert set(ticks) == {"HOME", "DRAW", "AWAY"}
    assert sum(float(t.true_probability) for t in ticks.values()) == pytest.approx(1.0, abs=1e-5)
    home = ticks["HOME"]
    assert home.home_team == "Arsenal" and home.away_team == "Leeds United"
    assert home.home_team_id == default_alias_dictionary().resolve("soccer_epl", "Arsenal").id
    assert float(home.odds) == 1.45  # best price across the three books
    # Median of each book's de-vigged probability, renormalised
    assert 0.6 < float(home.true_probability) < 0.7
    assert home.source == "odds_api" and home.quorum == "single_source" and home.confidence == pytest.approx(0.67)


def test_odds_api_skips_books_with_unknown_outcomes() -> None:
    events = odds_api_epl()
    events[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["name"] = "Somebody Else"
    report = OmniNormalizer().normalize(_batch("odds_api", [SourcePayload("soccer_epl", events)]))
    assert report.events_normalized == 1
    assert float({t.selection: t for t in report.ticks}["HOME"].odds) == pytest.approx(1.42)  # pinnacle dropped


# ---------------------------------------------------------------- Polymarket
def test_polymarket_soccer_three_way_maps_onto_the_same_match_as_odds_api() -> None:
    normalizer = OmniNormalizer()
    pm = normalizer.normalize(_batch("polymarket", [SourcePayload("epl", {"league": "epl", "ordering": "home", "markets": polymarket_epl_markets()})]))
    oa = normalizer.normalize(_batch("odds_api", [SourcePayload("soccer_epl", odds_api_epl())]))
    assert pm.events_seen == 1  # the futures market without a game time was ignored
    assert {t.match_id for t in pm.ticks} == {t.match_id for t in oa.ticks}  # cross-source canonical match id
    ticks = {t.selection: t for t in pm.ticks}
    assert float(ticks["HOME"].true_probability) == pytest.approx(0.705 / (0.705 + 0.185 + 0.105), abs=1e-5)
    assert float(ticks["HOME"].odds) == pytest.approx(1 / 0.71, abs=1e-4)  # buy at the ask
    assert ticks["HOME"].sport_key == "soccer_epl"


def test_polymarket_two_way_respects_away_first_titles() -> None:
    report = OmniNormalizer().normalize(_batch("polymarket", [SourcePayload("nfl", {"league": "nfl", "ordering": "away", "markets": polymarket_nfl_markets()})]))
    ticks = {t.selection: t for t in report.ticks}
    assert set(ticks) == {"HOME", "AWAY"}
    assert ticks["HOME"].home_team == "Dallas Cowboys" and ticks["HOME"].away_team == "Tampa Bay Buccaneers"
    assert float(ticks["HOME"].true_probability) == pytest.approx(0.805, abs=1e-4)  # complement of the outcome-0 mid
    assert float(ticks["AWAY"].odds) == pytest.approx(1 / 0.20, abs=1e-4)


def test_polymarket_incomplete_soccer_book_is_skipped() -> None:
    markets = [m for m in polymarket_epl_markets() if "Draw" not in (m.get("groupItemTitle") or "")]
    report = OmniNormalizer().normalize(_batch("polymarket", [SourcePayload("epl", {"league": "epl", "ordering": "home", "markets": markets})]))
    assert report.events_seen == 1 and report.events_normalized == 0 and not report.ticks


def test_polymarket_closed_market_is_suspended() -> None:
    markets = polymarket_epl_markets()
    markets[0]["acceptingOrders"] = False
    report = OmniNormalizer().normalize(_batch("polymarket", [SourcePayload("epl", {"league": "epl", "ordering": "home", "markets": markets})]))
    assert all(t.is_suspended for t in report.ticks)


# ---------------------------------------------------------------- board merge
def _tick(source: str, probability: float, odds: float, *, age: float = 0.0, suspended: bool = False) -> MarketTick:
    return MarketTick(
        match_id="m-1", home_team="Arsenal", away_team="Leeds United", market_type="Match Odds", selection="HOME",
        odds=Decimal(str(odds)), true_probability=Decimal(str(probability)), is_suspended=suspended,
        source=source, sources=(source,), confidence=0.8, observed_at=datetime.now(UTC) - timedelta(seconds=age), commence_time=KICKOFF,
    )


@pytest.fixture
def engine() -> QuorumConsensusEngine:
    return QuorumConsensusEngine(QuorumPolicy(variance_threshold=0.05, half_life_seconds=60, max_age_seconds=300, zero_tolerance=1e-9))


def test_merge_single_source_passes_through(engine) -> None:
    merged = merge_board_tick([_tick("polymarket", 0.70, 1.41)], engine)
    assert merged.quorum == "single_source" and merged.sources == ("polymarket",)


def test_merge_agreeing_sources_takes_best_price_and_consensus(engine) -> None:
    merged = merge_board_tick([_tick("polymarket", 0.70, 1.41), _tick("odds_api", 0.69, 1.45)], engine)
    assert merged.quorum == "consensus" and merged.sources == ("odds_api", "polymarket")
    assert float(merged.odds) == 1.45 and merged.source == "odds_api"
    assert 0.69 <= float(merged.true_probability) <= 0.70


def test_merge_disagreeing_sources_is_flagged_quarantined(engine) -> None:
    merged = merge_board_tick([_tick("polymarket", 0.70, 1.41), _tick("odds_api", 0.50, 1.95)], engine)
    assert merged.quorum == "quarantined"


def test_merge_ignores_stale_sources_and_suspended_prices(engine) -> None:
    merged = merge_board_tick([_tick("polymarket", 0.70, 1.41), _tick("odds_api", 0.40, 2.4, age=900)], engine)
    assert merged.quorum == "single_source" and merged.source == "polymarket"
    merged = merge_board_tick([_tick("polymarket", 0.70, 1.41), _tick("odds_api", 0.70, 9.9, suspended=True)], engine)
    assert float(merged.odds) == 1.41 and not merged.is_suspended
