"""Group 61: the Aryabhata quant engine.

Hardcoded cases pin every formula to values worked out by hand (exact fractions where the maths
allows it), and the edge cases the brief names (negative EV, odds of 1.01, a zero bankroll) are
shown never to crash and never to produce a negative or over-cap stake. Pipeline tests run against
a real Redis (Lua scripts, streams, pub/sub) on a dedicated, flushed database index and skip when
none is reachable.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

import pytest
import pytest_asyncio
import redis as redis_sync
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.adapters.ingestion.base import IngestionBatch, SourcePayload
from app.core.config import Settings, get_settings
from app.models import BetLedger, ExchangeAccount, RiskMandate, User
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.schemas.aryabhata import SIGNAL_TTL, BookQuote, EdgeSignal, MarketQuote, TradeSignal
from app.schemas.market import MarketTick
from app.services import aryabhata_engine as engine
from app.services import aryabhata_pipeline as pipeline
from app.services.aryabhata_engine import (
    MAX_PLAUSIBLE_EV,
    MIN_EV,
    BookLine,
    DevigFailure,
    InvalidMarketError,
    MarketState,
    RiskLimits,
    booksum,
    consensus_probabilities,
    devig,
    evaluate_market,
    expected_value,
    kelly_fraction,
    mpo_probabilities,
    multiplicative_probabilities,
    overround,
    recommend_stake,
    robust_center,
    shin_probabilities,
    to_decimal,
)
from app.services.omni_normalizer import OmniNormalizer, shin_devig
from tests.omni_fleet.conftest import odds_api_epl

D = Decimal
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
KICKOFF = NOW + timedelta(hours=3)
LINE_AGE = timedelta(seconds=90)
BOOK_AGE = timedelta(seconds=300)


def exact(value: Decimal, places: int = 28) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 40
        return value.quantize(D(1).scaleb(-places))


def frac(numerator: int, denominator: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 34
        return D(numerator) / D(denominator)


# ================================================================ inputs and overround
@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), -float("inf"), "nan", "NaN", "sNaN", "Infinity", "abc", "", object(), [], D("NaN"), D("-Infinity")])
def test_to_decimal_rejects_everything_unusable(value: object) -> None:
    assert to_decimal(value) is None


def test_to_decimal_keeps_the_written_value_of_floats() -> None:
    assert to_decimal(2.1) == D("2.1")  # the shortest repr, not 2.100000000000000088817...
    assert to_decimal("  1.95 ") == D("1.95")
    assert to_decimal(3) == D(3)


def test_booksum_and_overround_are_exact() -> None:
    odds = [D("2.0"), D("3.5"), D("4.0")]  # 1/2 + 2/7 + 1/4 = 29/28
    assert exact(booksum(odds)) == exact(frac(29, 28))
    assert exact(overround(odds)) == exact(frac(1, 28))
    assert overround([D("2.1"), D("2.1")]) < 0  # an arbitrage book has a negative overround


@pytest.mark.parametrize(
    "odds",
    [[D("2.0")], [], [D("1.0"), D("3.0")], [D("0.5"), D("3.0")], [D("-2"), D("2")], [float("nan"), 2.0], [float("inf"), 2.0], ["x", 2.0], [None, 2.0], [True, 2.0]],
)
def test_unpriceable_books_raise_invalid_market_never_typeerror(odds: list[Any]) -> None:
    with pytest.raises(InvalidMarketError):
        devig(odds)


# ================================================================ de-vig methods
def test_multiplicative_is_exactly_proportional() -> None:
    p = multiplicative_probabilities([D("2.0"), D("3.5"), D("4.0")])
    assert [exact(x) for x in p] == [exact(frac(14, 29)), exact(frac(8, 29)), exact(frac(7, 29))]


def test_mpo_matches_buchdahls_formula() -> None:
    odds = [D("2.0"), D("3.5"), D("4.0")]
    p = mpo_probabilities(odds)
    # fair_odds = n*o / (n - M*o) with n = 3, M = 1/28 -> fair probabilities 41/84, 23/84, 20/84
    assert [exact(x) for x in p] == [exact(frac(41, 84)), exact(frac(23, 84)), exact(frac(20, 84))]
    margin = frac(1, 28)
    for price, prob in zip(odds, p, strict=True):
        assert exact(1 / prob) == exact(3 * price / (3 - margin * price))


def test_mpo_refuses_a_longshot_beyond_n_over_margin() -> None:
    # M = 0.2733.., n/M = 10.98: the 25.0 longshot cannot carry its share of the margin
    with pytest.raises(DevigFailure):
        mpo_probabilities([D("1.2"), D("2.5"), D("25")])


def test_shin_symmetric_book_is_half_half_with_z_equal_to_the_margin() -> None:
    p, z = shin_probabilities([D("1.9"), D("1.9")])
    assert [exact(x, 25) for x in p] == [D("0.5").quantize(D("1e-25"))] * 2
    assert exact(z, 25) == exact(frac(1, 19), 25)  # z^2 - (20/19)z + 1/19 = 0 -> z = 1/19


def test_shin_two_way_matches_the_closed_form() -> None:
    odds = [D("1.5"), D("2.6")]
    with localcontext() as ctx:
        ctx.prec = 34
        q1, q2 = 1 / odds[0], 1 / odds[1]
        total = q1 + q2
        d = (q1 * q1 - q2 * q2) / total
        # Two outcomes solve in closed form: 1 - z = 2(1 - (q1^2 + q2^2)/B) / (1 - D^2)
        z_expected = 1 - 2 * (1 - (q1 * q1 + q2 * q2) / total) / (1 - d * d)
        p1 = ((z_expected * z_expected + 4 * (1 - z_expected) * q1 * q1 / total).sqrt() - z_expected) / (2 * (1 - z_expected))
    p, z = shin_probabilities(odds)
    assert abs(z - z_expected) < D("1e-24")
    assert abs(p[0] - p1) < D("1e-24")
    assert abs(sum(p) - 1) < D("1e-30")


def test_shin_three_way_models_the_favourite_longshot_bias() -> None:
    odds = [D("1.45"), D("4.6"), D("7.5")]
    p, z = shin_probabilities(odds)
    mult = multiplicative_probabilities(odds)
    assert abs(sum(p) - 1) < D("1e-30")
    assert D(0) < z < D("0.1")
    assert p[0] > mult[0]  # the favourite keeps more probability than proportional scaling gives it
    assert p[2] < mult[2]  # the longshot loses more
    reference = shin_devig([1 / 1.45, 1 / 4.6, 1 / 7.5])  # the fleet's independent float implementation
    assert all(abs(float(a) - b) < 1e-9 for a, b in zip(p, reference, strict=True))


def test_devig_prefers_shin_on_a_normal_book() -> None:
    result = devig([D("2.10"), D("3.40"), D("3.60")])
    assert result.method == "shin" and not result.fallbacks
    with localcontext() as ctx:
        ctx.prec = 34
        assert result.shin_z is not None and result.overround == result.booksum - 1 > 0


def test_devig_without_overround_degrades_to_multiplicative() -> None:
    result = devig([D("2.1"), D("2.1")])
    assert result.method == "multiplicative"
    assert len(result.fallbacks) == 2 and "shin" in result.fallbacks[0] and "mpo" in result.fallbacks[1]
    assert result.probabilities == (D("0.5"), D("0.5"))


def test_devig_falls_back_to_mpo_when_shin_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_: object) -> None:
        raise DevigFailure("shin: did not converge")

    monkeypatch.setattr(engine, "shin_probabilities", broken)
    result = devig([D("2.0"), D("3.5"), D("4.0")])
    assert result.method == "mpo" and result.fallbacks == ("shin: did not converge",)


def test_devig_survives_arithmetic_faults_in_every_upper_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    def zero_division(_: object) -> None:
        raise ZeroDivisionError

    def invalid(_: object) -> None:
        raise InvalidOperation

    monkeypatch.setattr(engine, "shin_probabilities", zero_division)
    monkeypatch.setattr(engine, "mpo_probabilities", invalid)
    result = devig([D("2.0"), D("3.5"), D("4.0")])
    assert result.method == "multiplicative"
    assert result.fallbacks == ("shin: ZeroDivisionError", "mpo: InvalidOperation")


def test_preferred_method_sets_the_first_tier() -> None:
    assert devig([D("2.0"), D("3.5"), D("4.0")], "mpo").method == "mpo"
    assert devig([D("2.0"), D("3.5"), D("4.0")], "multiplicative").method == "multiplicative"


def test_engine_ignores_the_callers_decimal_context() -> None:
    expected = devig([D("1.45"), D("4.6"), D("7.5")]).probabilities
    with localcontext() as ctx:
        ctx.prec = 3
        assert devig([D("1.45"), D("4.6"), D("7.5")]).probabilities == expected


# ================================================================ consensus
def test_robust_center_drops_a_tukey_outlier() -> None:
    assert robust_center([D("0.50"), D("0.51"), D("0.49"), D("0.50"), D("0.80")]) == D("0.50")
    assert robust_center([D("0.40"), D("0.60")]) == D("0.50")  # too few to judge: plain median


def test_consensus_renormalises_to_exactly_one() -> None:
    books = [{"HOME": D("0.50"), "AWAY": D("0.52")}, {"HOME": D("0.48"), "AWAY": D("0.50")}]
    consensus = consensus_probabilities(books, ["HOME", "AWAY"])
    assert sum(consensus.values()) == 1
    assert consensus["HOME"] == D("0.49") / D("1.00")


# ================================================================ EV and Kelly (hardcoded)
@pytest.mark.parametrize(
    ("p", "odds", "ev"),
    [
        (D("0.55"), D("2.0"), D("0.10")),
        (D("0.5"), D("2.1"), D("0.05")),
        (D("0.25"), D("4.4"), D("0.10")),
        (D("0.4"), D("2.0"), D("-0.2")),  # negative EV
        (D("0.995"), D("1.01"), D("0.00495")),  # odds of 1.01
        (D("0.5"), D("2.0"), D("0")),
    ],
)
def test_expected_value(p: Decimal, odds: Decimal, ev: Decimal) -> None:
    assert expected_value(p, odds) == ev


@pytest.mark.parametrize(
    ("p", "odds", "f_star"),
    [
        (D("0.55"), D("2.0"), D("0.1")),  # (b*p - q)/b = (0.55 - 0.45)/1
        (D("0.6"), D("1.8"), D("0.1")),  # 0.08 / 0.8
        (D("0.3"), D("4.0"), frac(1, 15)),  # 0.2 / 3
        (D("0.995"), D("1.01"), D("0.495")),  # 0.00495 / 0.01: tiny edges at short odds want huge fractions
        (D("0.4"), D("2.0"), D("0")),  # negative EV: never bet
        (D("0.5"), D("2.0"), D("0")),  # no edge
    ],
)
def test_full_kelly(p: Decimal, odds: Decimal, f_star: Decimal) -> None:
    assert exact(kelly_fraction(p, odds)) == exact(f_star)


@pytest.mark.parametrize(("p", "odds"), [(D("0.5"), D("1.0")), (D("0.5"), D("0.9")), (D("1.2"), D("2.0")), (D("-0.1"), D("2.0")), (float("nan"), 2.0), (0.5, float("inf")), (None, 2.0)])
def test_ev_and_kelly_reject_bad_inputs_without_raising(p: object, odds: object) -> None:
    assert expected_value(p, odds) is None
    assert kelly_fraction(p, odds) == 0


# ================================================================ stake sizing (hardcoded)
@pytest.mark.parametrize(
    ("f_star", "bankroll", "multiplier", "pct", "max_bet", "stake", "binding"),
    [
        (D("0.10"), D("100000"), D("0.25"), D("5"), None, D("2500.00"), "kelly"),
        (D("0.40"), D("100000"), D("0.25"), D("5"), None, D("5000.00"), "pct_cap"),
        (D("0.40"), D("100000"), D("0.25"), D("1"), None, D("1000.00"), "pct_cap"),
        (D("0.40"), D("100000"), D("0.25"), D("10"), None, D("10000.00"), "kelly"),  # exactly at the cap
        (D("0.40"), D("100000"), D("1"), D("50"), None, D("10000.00"), "pct_cap"),  # cap clamped to 10%
        (D("0.10"), D("100000"), D("0.25"), D("0.2"), None, D("1000.00"), "pct_cap"),  # cap clamped up to 1%
        (D("0.10"), D("100000"), D("0.25"), D("5"), D("50"), D("50.00"), "max_bet"),
        (D("0.10"), D("333.33"), D("0.25"), D("5"), None, D("8.33"), "kelly"),  # 8.33325 rounds DOWN
        (D("0.495"), D("100000"), D("0.25"), D("5"), None, D("5000.00"), "pct_cap"),  # odds 1.01 edge
        (D("0.10"), D("100000"), D("2"), D("5"), None, D("5000.00"), "pct_cap"),  # multiplier clamped to 1
    ],
)
def test_recommend_stake(f_star: Decimal, bankroll: Decimal, multiplier: Decimal, pct: Decimal, max_bet: Decimal | None, stake: Decimal, binding: str) -> None:
    decision = recommend_stake(f_star, bankroll, RiskLimits(multiplier, pct, max_bet))
    assert decision.stake_inr == stake
    assert decision.binding == binding
    assert decision.fraction == (stake / bankroll).quantize(D("1e-8"), rounding="ROUND_DOWN")


@pytest.mark.parametrize("bankroll", [D(0), 0, D("-500"), float("nan"), float("inf"), None, "abc"])
def test_zero_or_invalid_bankroll_stakes_nothing(bankroll: object) -> None:
    decision = recommend_stake(D("0.2"), bankroll, RiskLimits(D("0.25"), D("5")))
    assert decision.stake_inr == 0 and decision.binding == "no_bankroll"


@pytest.mark.parametrize("f_star", [D("-0.3"), D(0), None, float("nan")])
def test_negative_or_missing_edge_stakes_nothing(f_star: object) -> None:
    decision = recommend_stake(f_star, D("100000"), RiskLimits(D("0.25"), D("5")))
    assert decision.stake_inr == 0 and decision.binding == "no_edge"


def test_halted_and_zero_multiplier_stake_nothing() -> None:
    assert recommend_stake(D("0.2"), D("100000"), RiskLimits(D("0.25"), D("5"), halted=True)).binding == "halted"
    assert recommend_stake(D("0.2"), D("100000"), RiskLimits(D(0), D("5"))).stake_inr == 0


def test_stake_never_exceeds_any_cap_and_is_never_negative() -> None:
    fractions = [D("0.0001"), D("0.01"), D("0.05"), D("0.25"), D("0.495"), D("1")]
    bankrolls = [D(0), D("0.01"), D("1"), D("99.99"), D("1000"), D("123456.78"), D("10000000")]
    pcts = [D(x) / 2 for x in range(2, 21)]  # 1.0% .. 10.0% in half steps
    for f_star in fractions:
        for bankroll in bankrolls:
            for pct in pcts:
                for max_bet in (None, D(0), D("50"), D("1000000")):
                    decision = recommend_stake(f_star, bankroll, RiskLimits(D("0.25"), pct, max_bet))
                    assert decision.stake_inr >= 0
                    assert decision.stake_inr <= bankroll * pct / 100
                    assert max_bet is None or decision.stake_inr <= max_bet
                    assert decision.stake_inr == decision.stake_inr.quantize(D("0.01"))


# ================================================================ one market
def book(name: str, prices: tuple[str, ...], *, age: float = 0, suspended: bool = False, source: str = "odds_api") -> BookLine:
    labels = ("HOME", "AWAY") if len(prices) == 2 else ("HOME", "DRAW", "AWAY")
    return BookLine(source, name, {label: D(p) for label, p in zip(labels, prices, strict=True)}, NOW - timedelta(seconds=age), suspended)


def market(*books: BookLine, commence: datetime | None = KICKOFF) -> MarketState:
    return MarketState("fx-1", "Match Odds", "Arsenal", "Leeds United", books, "soccer_epl", commence)


def evaluate(state: MarketState) -> engine.MarketEvaluation:
    return evaluate_market(state, now=NOW, line_max_age=LINE_AGE, book_max_age=BOOK_AGE)


SHARP = (book("pinnacle", ("1.90", "1.90")), book("betfair", ("1.95", "1.95")))


def test_noise_filter_discards_edges_under_half_a_percent() -> None:
    result = evaluate(market(*SHARP, book("soft", ("2.008", "2.008"))))  # EV = 0.5 * 2.008 - 1 = +0.4%
    assert result.consensus == {"HOME": D("0.5"), "AWAY": D("0.5")}
    assert result.edges == ()


def test_edge_at_exactly_half_a_percent_is_kept() -> None:
    result = evaluate(market(*SHARP, book("soft", ("2.01", "2.01"))))  # EV = 0.5 * 2.01 - 1 = +0.5%
    assert {e.selection for e in result.edges} == {"HOME", "AWAY"}
    edge = result.edges[0]
    assert edge.ev == MIN_EV and edge.ev_percent == D("0.5000")
    assert edge.odds == D("2.01") and edge.bookmaker_id == "soft" and edge.books == 3
    assert exact(edge.full_kelly) == exact(D("0.005") / D("1.01"))


def test_implausible_edges_are_rejected_as_bad_quotes() -> None:
    result = evaluate(market(*SHARP, book("palpable", ("3.0", "3.0"))))  # EV +50%
    assert result.edges == ()
    assert result.skipped is not None and result.skipped["implausible"] == 2
    assert MAX_PLAUSIBLE_EV == D("0.25")


def test_a_single_book_never_signals_against_itself() -> None:
    result = evaluate(market(book("solo", ("2.20", "1.75"))))
    assert result.edges == () and result.skipped == {"too_few_books": 1}


def test_a_stale_line_feeds_the_consensus_but_is_never_bet() -> None:
    result = evaluate(market(*SHARP, book("slow", ("2.05", "2.05"), age=120)))
    assert result.books_priced == 3  # 120s old: inside the 300s consensus window
    assert result.edges == ()  # but outside the 90s line window, and the fresh best (1.95) has no edge


def test_books_beyond_the_consensus_window_are_dropped() -> None:
    result = evaluate(market(*SHARP, book("ancient", ("1.40", "3.20"), age=400)))
    assert result.books_priced == 2 and result.skipped == {"stale": 1}


def test_started_markets_never_signal() -> None:
    result = evaluate(market(*SHARP, book("soft", ("2.10", "2.10")), commence=NOW - timedelta(minutes=1)))
    assert result.edges == () and result.skipped == {"started": 1}


def test_suspended_books_are_ignored() -> None:
    result = evaluate(market(*SHARP, book("soft", ("2.10", "2.10"), suspended=True)))
    assert result.edges == () and result.skipped == {"suspended": 1}


def test_an_outlier_book_cannot_drag_the_consensus() -> None:
    sharp = [book(f"sharp{i}", ("1.95", "1.95")) for i in range(4)]
    result = evaluate(market(*sharp, book("broken", ("1.30", "4.00"))))
    assert result.consensus is not None and result.consensus["HOME"] == D("0.5")


def test_books_quoting_a_different_shape_are_left_out() -> None:
    result = evaluate(market(book("a", ("2.10", "3.40", "3.60")), book("b", ("2.05", "3.50", "3.70")), book("two_way", ("1.50", "2.60"))))
    assert result.labels == ("HOME", "DRAW", "AWAY") and result.books_priced == 2
    assert result.skipped is not None and result.skipped["shape"] == 1


def test_unpriceable_books_are_skipped_not_fatal() -> None:
    result = evaluate(market(*SHARP, book("zero", ("1.0", "1.9"))))
    assert result.books_priced == 2 and result.skipped == {"invalid": 1}


def test_three_way_edges_obey_every_formula() -> None:
    books = (
        book("pinnacle", ("2.10", "3.40", "3.60")),
        book("betfair", ("2.05", "3.50", "3.70")),
        book("matchbook", ("2.08", "3.45", "3.55")),
        book("softbook", ("2.30", "3.30", "3.30")),
    )
    result = evaluate(market(*books))
    assert result.consensus is not None and sum(result.consensus.values()) == 1
    assert [e.selection for e in result.edges] == ["HOME"]
    edge = result.edges[0]
    p = result.consensus["HOME"]
    assert edge.bookmaker_id == "softbook" and edge.odds == D("2.30")
    with localcontext() as ctx:
        ctx.prec = 34  # the engine's precision
        assert edge.ev == p * D("2.30") - 1 and edge.ev >= MIN_EV
        assert edge.ev_percent == (edge.ev * 100).quantize(D("0.0001"))
        assert edge.full_kelly == edge.ev / D("1.30")
        assert edge.true_prob == p.quantize(D("1e-10"))
        assert edge.overround == booksum([D("2.30"), D("3.30"), D("3.30")]) - 1
    assert edge.devig_method == "shin" and edge.books == 4
    assert edge.expires_at - edge.timestamp == SIGNAL_TTL == timedelta(seconds=15)
    assert edge.key == "fx-1|HOME"


# ================================================================ TradeSignal
def _edge() -> EdgeSignal:
    books = (book("pinnacle", ("2.10", "3.40", "3.60")), book("betfair", ("2.05", "3.50", "3.70")), book("softbook", ("2.30", "3.30", "3.30")))
    return evaluate(market(*books)).edges[0]


def test_personalize_sizes_the_stake_for_one_bankroll() -> None:
    edge = _edge()
    signal = pipeline.personalize(edge, D("100000"), RiskLimits(D("0.25"), D("5")))
    expected = recommend_stake(edge.full_kelly, D("100000"), RiskLimits(D("0.25"), D("5")))
    assert signal.kelly_stake_inr == expected.stake_inr > 0
    assert signal.kelly_stake_inr <= D("5000")
    wire = signal.model_dump(mode="json")
    assert isinstance(wire["odds"], float) and isinstance(wire["kelly_stake_inr"], float) and isinstance(wire["true_prob"], float)
    assert set(TradeSignal.model_fields) >= {"signal_id", "fixture_id", "market_id", "selection", "odds", "true_prob", "ev_percent", "kelly_stake_inr", "bookmaker_id", "timestamp", "expires_at"}


def test_personalize_with_zero_bankroll_does_not_crash() -> None:
    signal = pipeline.personalize(_edge(), D(0), RiskLimits(D("0.25"), D("5")))
    assert signal.kelly_stake_inr == 0 and signal.stake_binding == "no_bankroll"
    assert pipeline.personalize(_edge(), None, RiskLimits(D("0.25"), D("5"))).kelly_stake_inr == 0


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at_offset": timedelta(seconds=16)},
        {"expires_at_offset": timedelta(0)},
        {"odds": D("1.0")},
        {"true_prob": D("1")},
        {"kelly_stake_inr": D("-1")},
        {"extra": "field"},
        {"odds": 2.3},  # strict: a float is not a Decimal
    ],
)
def test_trade_signal_is_strict(change: dict[str, Any]) -> None:
    good = pipeline.personalize(_edge(), D("1000"), RiskLimits(D("0.25"), D("5"))).model_dump()
    offset = change.pop("expires_at_offset", None)
    if offset is not None:
        good["expires_at"] = good["timestamp"] + offset
    good.update(change)
    with pytest.raises(ValidationError):
        TradeSignal(**good)


# ================================================================ frames from ingestion
def test_normalizer_emits_one_frame_per_fixture_with_named_books() -> None:
    batch = IngestionBatch(source_id="odds_api", payloads=[SourcePayload("soccer_epl", odds_api_epl())], fetched_at=NOW, latency_ms=1, requests=1, retries=0)
    report = OmniNormalizer().normalize(batch)
    assert len(report.quotes) == 1
    quote = report.quotes[0]
    assert quote.match_id == report.ticks[0].match_id and quote.source == "odds_api" and quote.fetched_at == NOW
    assert [b.bookmaker_id for b in quote.books] == ["pinnacle", "betfair_ex_uk", "williamhill"]
    assert quote.books[0].prices == {"HOME": D("1.45"), "DRAW": D("4.6"), "AWAY": D("7.5")}


def _tick(selection: str, odds: str, source: str = "partner") -> MarketTick:
    return MarketTick(match_id="m1", home_team="Arsenal", away_team="Leeds", market_type="Match Odds", selection=selection, odds=D(odds), true_probability=D("0.4"), is_suspended=False, source=source)


def test_pushed_ticks_become_one_book_per_source() -> None:
    quotes = pipeline.quotes_from_ticks([_tick("HOME", "2.1"), _tick("AWAY", "1.8"), _tick("HOME", "2.0", "other")], NOW)
    assert len(quotes) == 1  # "other" priced one selection only: not a market
    assert quotes[0].books[0].bookmaker_id == "partner"
    assert quotes[0].books[0].prices == {"HOME": D("2.1"), "AWAY": D("1.8")}


# ================================================================ Redis pipeline
TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]  # forced onto the isolated test database by tests/conftest.py
_SENTINEL = "betdoc:test-sentinel"


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        if await client.dbsize() and not await client.exists(_SENTINEL) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):  # claimed by tests/conftest.py
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours; refusing to flush it")
    except (RedisError, OSError):
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    await client.set(_SENTINEL, "1")
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "arya_test", **overrides})


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _quote(source: str, books: dict[str, tuple[str, str]], fetched_at: datetime) -> MarketQuote:
    return MarketQuote(
        match_id="fx-ars-lee",
        market_type="Match Odds",
        home_team="Arsenal",
        away_team="Leeds United",
        sport_key="soccer_epl",
        commence_time=fetched_at + timedelta(hours=2),
        source=source,
        fetched_at=fetched_at,
        books=tuple(BookQuote(bookmaker_id=name, prices={"HOME": D(h), "AWAY": D(a)}) for name, (h, a) in books.items()),
    )


async def _drain_messages(pubsub: Any, timeout: float = 2.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if message and message.get("type") == "message":
            out.append(json.loads(message["data"]))
        elif out:
            break
    return out


@pytest.mark.asyncio
async def test_stream_frames_become_committed_and_published_edges(redis: Redis) -> None:
    settings, clock = _settings(), Clock()
    keys = pipeline.AryabhataKeys(settings.ARYABHATA_PREFIX)
    consumer = pipeline.AryabhataConsumer(redis, settings, name="test", clock=clock)
    await consumer.ensure_group()
    pubsub = redis.pubsub()
    await pubsub.subscribe(keys.channel)

    sharp = _quote("odds_api", {"pinnacle": ("1.90", "1.90"), "betfair": ("1.95", "1.95")}, clock.now)
    soft = _quote("partner", {"partner": ("2.10", "1.70")}, clock.now)
    assert await pipeline.publish_market_quotes(redis, [sharp, soft], settings)
    assert await consumer.step() == 2

    active = await pipeline.read_active_edges(redis, settings, clock.now)
    assert [(e.selection, e.bookmaker_id, e.odds) for e in active] == [("HOME", "partner", D("2.10"))]
    messages = await _drain_messages(pubsub)
    assert messages[-1]["type"] == "market" and messages[-1]["signals"][0]["selection"] == "HOME"
    pending = await redis.xpending(keys.frames, keys.group)
    assert pending["pending"] == 0  # every frame acknowledged

    # The partner moves its price below the edge: the card is withdrawn at once, not left to expire
    clock.now += timedelta(seconds=2)
    assert await pipeline.publish_market_quotes(redis, [_quote("partner", {"partner": ("1.92", "1.92")}, clock.now)], settings)
    await consumer.step()
    assert await pipeline.read_active_edges(redis, settings, clock.now) == []
    withdrawn = await _drain_messages(pubsub)
    assert withdrawn[-1]["withdrawn"] == ["fx-ars-lee|HOME"] and withdrawn[-1]["signals"] == []
    await pubsub.aclose()


@pytest.mark.asyncio
async def test_a_new_frame_replaces_everything_its_source_said(redis: Redis) -> None:
    settings, clock = _settings(), Clock()
    keys = pipeline.AryabhataKeys(settings.ARYABHATA_PREFIX)
    consumer = pipeline.AryabhataConsumer(redis, settings, name="test", clock=clock)
    await consumer.handle_quotes([_quote("odds_api", {"a": ("1.9", "1.9"), "b": ("1.95", "1.95")}, clock.now), _quote("partner", {"p": ("2.0", "1.8")}, clock.now)])
    await consumer.handle_quotes([_quote("odds_api", {"a": ("1.9", "1.9")}, clock.now)])
    fields = sorted(f for f in await redis.hkeys(keys.books("fx-ars-lee|Match Odds")) if not f.startswith("~"))
    assert fields == ["odds_api|a", "partner|p"]  # "b" stopped quoting; the other source is untouched


@pytest.mark.asyncio
async def test_an_older_evaluation_can_never_overwrite_a_newer_one(redis: Redis) -> None:
    settings = _settings()
    keys = pipeline.AryabhataKeys(settings.ARYABHATA_PREFIX)
    commit = redis.register_script(pipeline._COMMIT_EDGES)
    books = keys.books("m|Match Odds")
    now = str(datetime.now(UTC).timestamp())
    assert await commit(keys=[books, keys.active, keys.active_exp], args=["2000", now, keys.channel, "m|Match Odds", "0"]) == 1
    assert await commit(keys=[books, keys.active, keys.active_exp], args=["1000", now, keys.channel, "m|Match Odds", "0"]) == 0


@pytest.mark.asyncio
async def test_frames_older_than_the_line_window_never_signal(redis: Redis) -> None:
    settings, clock = _settings(), Clock()
    consumer = pipeline.AryabhataConsumer(redis, settings, name="test", clock=clock)
    old = clock.now - timedelta(seconds=120)  # a backlog: fetched two minutes ago, processed now
    evaluations = await consumer.handle_quotes([_quote("odds_api", {"a": ("1.9", "1.9"), "b": ("1.95", "1.95")}, old), _quote("partner", {"p": ("2.1", "1.7")}, old)])
    assert all(not e.edges for e in evaluations)


@pytest.mark.asyncio
async def test_publishing_frames_without_redis_never_raises() -> None:
    assert await pipeline.publish_market_quotes(None, [_quote("x", {"a": ("2", "2")}, NOW)], _settings()) is False
    broken = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    assert await pipeline.publish_market_quotes(broken, [_quote("x", {"a": ("2", "2")}, NOW)], _settings()) is False
    await broken.aclose()


# ---------------------------------------------------------------- risk limits and bankroll (SQLite)
def _sqlite_metadata() -> MetaData:
    md = MetaData()
    for table in (User.__table__, ExchangeAccount.__table__, RiskMandate.__table__, BetLedger.__table__, SystemSettingsModel.__table__):
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and "~" in str(c.sqltext)]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    sql = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with sql.begin() as conn:
        await conn.run_sync(_sqlite_metadata().create_all)
    try:
        yield async_sessionmaker(sql, expire_on_commit=False)
    finally:
        await sql.dispose()


@pytest.mark.asyncio
async def test_risk_limits_come_from_the_db_then_redis_and_follow_the_slider(redis: Redis, session_factory: async_sessionmaker[AsyncSession]) -> None:
    settings = _settings()
    keys = pipeline.AryabhataKeys(settings.ARYABHATA_PREFIX)
    async with session_factory() as session:
        session.add(SystemSettingsModel(id=SETTINGS_SINGLETON_ID, default_kelly_fraction=0.25, max_bet_size=50000.0, max_daily_exposure=500.0, max_stake_pct=D("5.00")))
        await session.commit()
    limits = await pipeline.load_risk_limits(redis, session_factory, settings)
    assert limits == RiskLimits(D("0.25"), D("5.00"), D("50000.0"), False)
    assert (await redis.hgetall(keys.risk))["max_stake_pct"] == "5.00"  # mirrored for the next read

    pubsub = redis.pubsub()
    await pubsub.subscribe(keys.channel)
    async with session_factory() as session:
        row = await session.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
        assert row is not None
        row.max_stake_pct = D("2.50")
        await session.commit()
        await pipeline.publish_risk_limits(redis, row, settings)
    assert (await pipeline.load_risk_limits(redis, session_factory, settings)).max_stake_pct == D("2.50")
    assert {"type": "risk"} in await _drain_messages(pubsub)
    await pubsub.aclose()


@pytest.mark.asyncio
async def test_risk_limits_fail_closed_when_nothing_can_answer() -> None:
    def broken_factory() -> Any:
        raise OSError("database down")

    limits = await pipeline.load_risk_limits(None, broken_factory, _settings())  # type: ignore[arg-type]
    assert limits.halted and recommend_stake(D("0.2"), D("100000"), limits).stake_inr == 0


def test_emergency_stop_halts_stakes() -> None:
    row = SystemSettingsModel(id=SETTINGS_SINGLETON_ID, default_kelly_fraction=0.25, max_bet_size=50.0, max_daily_exposure=0.0, max_stake_pct=D("5"))
    assert pipeline.limits_from_row(row).halted
    assert pipeline.limits_from_row(None) == RiskLimits(D("0.25"), D("5.00"), None, False)  # mirrors the trading gate


@pytest.mark.asyncio
async def test_live_bankroll_is_starting_capital_plus_realised_pnl(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        user = User(username="quant", hashed_password="x")
        session.add(user)
        await session.flush()
        account = ExchangeAccount(user_id=user.id, exchange_name="Pinnacle", api_key_encrypted="k", api_secret_encrypted="s")
        session.add(account)
        await session.flush()

        def bet(status: str, stake: str, payout: str | None) -> BetLedger:
            return BetLedger(
                idempotency_key=str(uuid.uuid4()), exchange_account_id=account.id, match_id="m", market_type="Match Odds",
                selection="HOME", odds=D("2.10"), stake=D(stake), payout=None if payout is None else D(payout),
                true_probability=D("0.5"), status=status, resolved_at=NOW if payout is not None else None,
            )

        session.add_all([bet("WON", "100", "210"), bet("LOST", "40", "0"), bet("ACCEPTED", "500", None)])
        await session.commit()
        bankroll = await pipeline.live_bankroll(session, user.id, 10_000.0)
    assert bankroll == D("10070")  # +110 - 40; the open bet is exposure, not realised


# ---------------------------------------------------------------- /ws/signals
@pytest.fixture
def ws_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    try:
        probe = redis_sync.Redis.from_url(TEST_REDIS_URL)
        probe.ping()
        if probe.dbsize() and not probe.exists(_SENTINEL) and not os.environ.get("BETDOC_TEST_REDIS_CLAIMED"):  # claimed by tests/conftest.py
            pytest.skip(f"{TEST_REDIS_URL} holds data that is not ours")
        probe.flushdb()
        probe.set(_SENTINEL, "1")
    except (RedisError, OSError):
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    settings = _settings()

    async def fixed_limits(*_: Any) -> RiskLimits:
        return RiskLimits(D("0.25"), D("5"), None, False)

    async def fixed_bankroll(*_: Any) -> Decimal:
        return D("100000")

    class NoSession:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_: Any) -> None:
            return None

    monkeypatch.setattr(pipeline, "load_risk_limits", fixed_limits)
    monkeypatch.setattr(pipeline, "live_bankroll", fixed_bankroll)
    app = FastAPI()
    user_id = uuid.uuid4()

    @app.websocket("/signals")
    async def signals(websocket: WebSocket) -> None:
        client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        try:
            await pipeline.run_signal_socket(websocket, user_id, client, lambda: NoSession(), settings)  # type: ignore[arg-type,return-value]
        finally:
            await client.aclose()

    with TestClient(app) as client:
        yield client
    probe.flushdb()
    probe.close()


def test_signals_socket_stakes_edges_for_its_user_and_resizes_on_risk_changes(ws_app: TestClient) -> None:
    settings = _settings()
    keys = pipeline.AryabhataKeys(settings.ARYABHATA_PREFIX)
    stamp = datetime.now(UTC)
    edge = _edge().model_copy(update={"timestamp": stamp, "expires_at": stamp + SIGNAL_TTL})
    publisher = redis_sync.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    with ws_app.websocket_connect("/signals") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot" and snapshot["signals"] == [] and snapshot["bankroll"] == 100000.0
        assert snapshot["risk"]["max_stake_pct"] == 5.0 and snapshot["ttl_seconds"] == 15

        payload = {"type": "market", "market": "fx-1|Match Odds", "version": 1, "signals": [json.loads(edge.model_dump_json())], "withdrawn": []}
        for _ in range(50):  # the socket subscribes before its snapshot; retry until the relay is listening
            if publisher.publish(keys.channel, json.dumps(payload)):
                break
        message = ws.receive_json()
        assert message["type"] == "signals" and message["withdrawn"] == []
        signal = message["signals"][0]
        expected = recommend_stake(edge.full_kelly, D("100000"), RiskLimits(D("0.25"), D("5")))
        assert signal["kelly_stake_inr"] == float(expected.stake_inr) and 0 < signal["kelly_stake_inr"] <= 5000
        assert signal["fixture_id"] == "fx-1" and signal["selection"] == "HOME" and isinstance(signal["odds"], float)

        publisher.publish(keys.channel, '{"type":"risk"}')
        assert ws.receive_json()["type"] == "snapshot"

        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}
    publisher.close()


def test_trade_signal_wire_numbers_are_finite() -> None:
    wire = pipeline.personalize(_edge(), D("5000"), RiskLimits(D("0.25"), D("5"))).model_dump(mode="json")
    for name in ("odds", "true_prob", "ev_percent", "kelly_stake_inr", "overround", "stake_fraction"):
        assert isinstance(wire[name], float) and math.isfinite(wire[name])
