"""Group 64: the arbitrage and hedging maths, proved by hand.

Every figure here is exact ``Decimal`` arithmetic worked out on paper first: commission on net
winnings (Betfair 5% against Pinnacle 0%), FX haircuts on foreign payouts, arbitrage staking, the
balanced (equal profit) and free-bet (zero loss) hedges, the slider between them, and partial-fill
re-sizing. Then the live portfolio built from Redis-shaped books, and the FX and commission tables.
"""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings, get_settings
from app.domain.math.arbitrage_calc import (
    ArbitrageMathError,
    FxQuote,
    HeldBet,
    Offer,
    best_offers,
    book_profits,
    booksum,
    break_even_odds,
    commission_adjusted_odds,
    find_arbitrage,
    from_inr,
    hedge_after_fills,
    hedge_book,
    inr_odds,
    is_arbitrage,
    leg_cost_inr,
    raw_odds_for,
    rescale_after_fill,
    stake_arbitrage,
    to_inr,
)
from app.services.aryabhata_engine import BookLine
from app.services.fx_rates import FxRate, FxRates, FxUnavailableError
from app.services.portfolio_manager import MarketMeta, PricingContext, evaluate_book, portfolio_payload, scan_arbitrage
from app.services.portfolio_positions import OpenBet
from app.services.portfolio_manager import market_offers
from app.services.venue_costs import TermsTable, bookmaker_terms, default_currency

D = Decimal
BETFAIR = D("0.05")
PINNACLE = D("0")


def offer(selection: str, provider: str, odds: str, commission: Decimal = PINNACLE, fx: FxQuote | None = None) -> Offer:
    return Offer(selection, provider, D(odds), commission, fx)


# ================================================================ commission
def test_true_odds_formula_betfair_against_pinnacle() -> None:
    """True_Odds = ((Raw_Odds - 1) * (1 - Commission_Rate)) + 1."""
    assert commission_adjusted_odds("3.00", BETFAIR) == D("2.9000")  # (2 * 0.95) + 1
    assert commission_adjusted_odds("3.00", PINNACLE) == D("3.00")
    assert commission_adjusted_odds("2.04", BETFAIR) == D("1.98800")  # (1.04 * 0.95) + 1
    assert commission_adjusted_odds("1.01", BETFAIR) == D("1.00950")
    # Commission only ever takes from the win: the stake back is untouched
    assert commission_adjusted_odds("11", D("0.02")) == D("10.80")


@pytest.mark.parametrize(("odds", "rate"), [("1.00", "0"), ("0.5", "0"), ("2", "-0.01"), ("2", "0.5"), ("NaN", "0"), (None, "0"), (2.0, float("inf"))])
def test_unpriceable_inputs_are_refused(odds: object, rate: object) -> None:
    with pytest.raises(ArbitrageMathError):
        commission_adjusted_odds(odds, rate)


def test_raw_odds_for_inverts_the_commission_and_rounds_up() -> None:
    assert raw_odds_for(D("1.988"), BETFAIR) == D("2.0400")
    # Rounded up: a floor built from it can never admit a price that returns less
    floor = raw_odds_for(D("1.9"), BETFAIR)
    assert commission_adjusted_odds(floor, BETFAIR) >= D("1.9")
    assert commission_adjusted_odds(floor - D("0.0001"), BETFAIR) < D("1.9")


def test_commission_turns_a_raw_arbitrage_into_none() -> None:
    """Betfair 2.04 HOME + Pinnacle 2.00 AWAY: raw 1/2.04 + 1/2.00 = 0.990196 looks like a 0.98% arb.
    After Betfair's 5%: 1/1.988 + 1/2.00 = 1.003018. No arbitrage. The same price at Pinnacle keeps it."""
    raw = booksum([D("2.04"), D("2.00")])
    assert raw < 1 and raw.quantize(D("0.000001")) == D("0.990196")
    at_betfair = [offer("HOME", "betfair_ex_uk", "2.04", BETFAIR), offer("AWAY", "pinnacle", "2.00")]
    assert not is_arbitrage(at_betfair)
    assert booksum([o.rupee_odds() for o in at_betfair]).quantize(D("0.000001")) == D("1.003018")
    assert find_arbitrage(at_betfair, ["HOME", "AWAY"], 10_000) is None
    at_pinnacle = [offer("HOME", "pinnacle", "2.04"), offer("AWAY", "pinnacle2", "2.00")]
    assert is_arbitrage(at_pinnacle)


# ================================================================ FX
def test_fx_haircut_on_a_foreign_leg() -> None:
    """A GBP account: true odds 2.90 (Betfair 3.00 at 5%), payout home through a 0.5% haircut."""
    gbp = FxQuote("GBP", D("110.00"), D("0.005"))
    leg = offer("HOME", "betfair_ex_uk", "3.00", BETFAIR, gbp)
    assert leg.true_odds() == D("2.9000")
    assert leg.rupee_odds() == D("2.8855000")  # 2.90 * 0.995
    assert inr_odds(D("2.9"), None) == D("2.9")  # an INR leg carries no haircut
    # ₹10,000 buys £90.90 (rounded down to the penny), which cost ₹9,999.00
    assert from_inr(D("10000"), gbp) == D("90.90")
    assert to_inr(D("90.90"), gbp) == D("9999.0000")


def test_fx_quote_refuses_a_bad_rate() -> None:
    for rate in (D("0"), D("-1"), D("NaN")):
        with pytest.raises(ArbitrageMathError):
            FxQuote("USD", rate)
    with pytest.raises(ArbitrageMathError):
        FxQuote("USD", D("84"), D("1"))


def test_an_arbitrage_across_currencies_counts_the_haircut() -> None:
    """INR book 2.10 vs a GBP book 2.10: 1/2.10 + 1/(2.10 * 0.98) = 0.962099 (a 2% haircut)."""
    gbp = FxQuote("GBP", D("105"), D("0.02"))
    arb = find_arbitrage([offer("HOME", "inr_book", "2.10"), offer("AWAY", "uk_book", "2.10", PINNACLE, gbp)], ["HOME", "AWAY"], 10_000)
    assert arb is not None
    assert arb.booksum.quantize(D("0.000001")) == D("0.962099")
    gbp_leg = next(leg for leg in arb.legs if leg.offer.currency == "GBP")
    assert gbp_leg.stake_inr == to_inr(gbp_leg.stake_ccy, gbp)  # the rupee stake is exactly what the pounds cost
    for leg in arb.legs:
        assert leg.payout_inr - arb.total_stake_inr >= arb.guaranteed_profit_inr  # every outcome at least the guarantee


# ================================================================ arbitrage
def test_arbitrage_stakes_pay_the_same_on_every_outcome() -> None:
    """3-way: 1/2.70 + 1/3.80 + 1/3.40 = 0.370370 + 0.263158 + 0.294118 = 0.927646, ₹10,000 staked returns ₹10,000 / B either way."""
    offers = [offer("HOME", "a", "2.70"), offer("DRAW", "b", "3.80"), offer("AWAY", "c", "3.40")]
    arb = find_arbitrage(offers, ["HOME", "DRAW", "AWAY"], "10000")
    assert arb is not None
    assert arb.booksum.quantize(D("0.0000001")) == D("0.9276459")
    payouts = [leg.payout_inr for leg in arb.legs]
    assert max(payouts) - min(payouts) <= D("0.05")  # level to within the paisa rounding
    assert arb.guaranteed_profit_inr == min(payouts) - arb.total_stake_inr
    assert arb.guaranteed_profit_inr > 0 and arb.total_stake_inr <= D("10000")
    assert all(leg.stake_inr == leg.stake_inr.quantize(D("0.01")) for leg in arb.legs)


def test_an_uncovered_outcome_is_never_an_arbitrage() -> None:
    """HOME 3.00 + AWAY 3.00 sums to 0.667, but nothing covers the DRAW: every leg loses on it."""
    offers = [offer("HOME", "a", "3.00"), offer("AWAY", "b", "3.00")]
    assert find_arbitrage(offers, ["HOME", "DRAW", "AWAY"], 1000) is None
    assert find_arbitrage(offers, ["HOME", "AWAY"], 1000) is not None


def test_best_offer_is_chosen_after_commission() -> None:
    """Betfair 2.10 at 5% is 2.045; Pinnacle's 2.06 at 0% beats it."""
    best = best_offers([offer("HOME", "betfair_ex_uk", "2.10", BETFAIR), offer("HOME", "pinnacle", "2.06")])
    assert best["HOME"].provider == "pinnacle"


def test_rounding_never_leaves_a_negative_guarantee() -> None:
    rng = random.Random(64)
    for _ in range(400):
        odds = [D(str(round(rng.uniform(1.05, 9.0), 2))) for _ in range(rng.choice((2, 3)))]
        commissions = [rng.choice((PINNACLE, BETFAIR, D("0.02"))) for _ in odds]
        offers = [Offer(f"O{i}", f"b{i}", o, c) for i, (o, c) in enumerate(zip(odds, commissions, strict=True))]
        arb = stake_arbitrage(offers, D(str(rng.randint(5, 50_000))))
        if arb is not None:
            assert arb.guaranteed_profit_inr > 0
            assert all(leg.payout_inr - arb.total_stake_inr >= arb.guaranteed_profit_inr for leg in arb.legs)


# ================================================================ partial fills
def test_partial_fill_scales_the_other_legs() -> None:
    """Leg A asked ₹10,000 and got ₹4,000: every other leg is staked at 40%."""
    assert rescale_after_fill({"HOME": D("10000"), "AWAY": D("9523.80")}, "HOME", "4000") == {"AWAY": D("3809.52")}
    assert rescale_after_fill({"HOME": D("10000"), "DRAW": D("7000"), "AWAY": D("5000")}, "HOME", "4000") == {"DRAW": D("2800.00"), "AWAY": D("2000.00")}
    assert rescale_after_fill({"HOME": D("10000"), "AWAY": D("9000")}, "HOME", "10000") == {"AWAY": D("9000.00")}
    with pytest.raises(ArbitrageMathError):
        rescale_after_fill({"HOME": D("10000"), "AWAY": D("9000")}, "HOME", "10001")


def test_break_even_odds() -> None:
    """₹4,000 + ₹4,000 staked: the second leg must return ₹8,000 from ₹4,000, odds 2.00."""
    assert break_even_odds(D("8000"), D("4000")) == D("2")


# ================================================================ hedging
def single_bet(stake: str, odds: str, selection: str = "HOME", outcomes: tuple[str, ...] = ("HOME", "AWAY")) -> dict[str, Decimal]:
    return book_profits([HeldBet(selection, D(stake), D(odds))], outcomes)


def test_balanced_hedge_by_hand() -> None:
    """₹1,000 on HOME @ 3.00, AWAY now 2.50 (Pinnacle). h = 1000 * 3 / 2.5 = ₹1,200.
    HOME: 3000 - 1000 - 1200 = 800; AWAY: 1200 * 2.5 - 2200 = 800. S * (E * (1 - B) - 1) = 800."""
    plan = hedge_book(single_bet("1000", "3.00"), {"AWAY": offer("AWAY", "pinnacle", "2.50")}, fraction=1)
    assert plan.kind == "balanced" and plan.anchor == "HOME"
    assert [(leg.offer.selection, leg.stake_inr) for leg in plan.legs] == [("AWAY", D("1200.00"))]
    assert plan.after == {"HOME": D("800.00"), "AWAY": D("800.00")}
    assert plan.locks_profit and plan.worst_before == D("-1000.00")


def test_free_bet_hedge_loses_exactly_nothing() -> None:
    """Free bet: H = S * B / (1 - B) = 1000 * 0.4 / 0.6 = ₹666.67. If HOME fails we lose nothing;
    every rupee of profit sits on HOME. Stakes round *up*, so rounding can't make it a loss."""
    plan = hedge_book(single_bet("1000", "3.00"), {"AWAY": offer("AWAY", "pinnacle", "2.50")}, fraction=0)
    assert plan.kind == "free_bet"
    assert plan.legs[0].stake_inr == D("666.68")
    assert plan.after["AWAY"] == D("0.02")  # never below zero
    assert plan.after["HOME"] == D("1333.32")


def test_commission_on_the_hedge_leg_costs_the_lock_in() -> None:
    """The same hedge on Betfair at 5%: AWAY 2.50 is 2.425. Balanced = 1000 * (3 * (1 - 1/2.425) - 1)."""
    pinnacle = hedge_book(single_bet("1000", "3.00"), {"AWAY": offer("AWAY", "pinnacle", "2.50")})
    betfair = hedge_book(single_bet("1000", "3.00"), {"AWAY": offer("AWAY", "betfair_ex_uk", "2.50", BETFAIR)})
    expected = D("1000") * (D("3") * (1 - 1 / D("2.425")) - 1)
    assert expected.quantize(D("0.01")) == D("762.89")
    assert abs(betfair.worst_after - expected) <= D("0.02")
    assert pinnacle.worst_after - betfair.worst_after > D("37")  # the commission's bite


def test_three_way_hedges() -> None:
    """₹500 on HOME @ 4.00; DRAW 3.60, AWAY 2.80 now. B = 1/3.6 + 1/2.8 = 0.634921.
    Balanced profit = 500 * (4 * (1 - B) - 1) = ₹230.16 on every outcome."""
    book = single_bet("500", "4.00", outcomes=("HOME", "DRAW", "AWAY"))
    offers = {"DRAW": offer("DRAW", "x", "3.60"), "AWAY": offer("AWAY", "y", "2.80")}
    balanced = hedge_book(book, offers, fraction=1)
    assert all(abs(v - D("230.16")) <= D("0.03") for v in balanced.after.values())
    free = hedge_book(book, offers, fraction=0)
    assert free.after["DRAW"] >= 0 and free.after["AWAY"] >= 0 and free.after["DRAW"] <= D("0.05")
    assert free.after["HOME"] > balanced.after["HOME"]


def test_the_slider_moves_profit_from_the_anchor_to_the_rest() -> None:
    book = single_bet("1000", "3.00")
    offers = {"AWAY": offer("AWAY", "pinnacle", "2.50")}
    plans = [hedge_book(book, offers, fraction=f) for f in ("0", "0.25", "0.5", "0.75", "1")]
    anchors = [p.after["HOME"] for p in plans]
    others = [p.after["AWAY"] for p in plans]
    assert anchors == sorted(anchors, reverse=True) and others == sorted(others)
    assert plans[2].kind == "partial" and plans[2].after["AWAY"] >= D("400")  # half of the ₹800 balanced profit


def test_a_hedge_needs_a_price_for_every_outcome_it_covers() -> None:
    with pytest.raises(ArbitrageMathError, match="DRAW"):
        hedge_book(single_bet("500", "4.00", outcomes=("HOME", "DRAW", "AWAY")), {"AWAY": offer("AWAY", "y", "2.80")}, fraction=0)


def test_an_outcome_already_above_target_gets_no_stake() -> None:
    """HOME ₹1,000 @ 3.0 and an earlier ₹500 on DRAW @ 10.0: DRAW already makes ₹3,500. A free bet
    on HOME covers AWAY alone: H = 500 / (1 - 1/3) = ₹750 (AWAY: 750 * 3 - 2250 = 0), which leaves
    DRAW at ₹2,750. (Solved with both, DRAW's stake comes out negative: a lay we don't have.)"""
    book = book_profits([HeldBet("HOME", D("1000"), D("3")), HeldBet("DRAW", D("500"), D("10"))], ("HOME", "DRAW", "AWAY"))
    assert book == {"HOME": D("1500"), "DRAW": D("3500"), "AWAY": D("-1500")}
    plan = hedge_book(book, {"DRAW": offer("DRAW", "x", "4.0"), "AWAY": offer("AWAY", "y", "3.0")}, anchor="HOME", fraction=0)
    assert [(leg.offer.selection, leg.stake_inr) for leg in plan.legs] == [("AWAY", D("750.01"))]
    assert D("0") <= plan.after["AWAY"] <= D("0.03") and plan.after["DRAW"] == D("2749.99")


def test_covering_one_outcome_can_uncover_another() -> None:
    """With the earlier DRAW bet at 4.0 (DRAW makes ₹500), covering AWAY alone costs ₹900 and would
    drop DRAW to -₹400, so the free bet stakes both: H = 375 / (1 - 7/12) = ₹900, DRAW ₹100, AWAY ₹800."""
    book = book_profits([HeldBet("HOME", D("1000"), D("3")), HeldBet("DRAW", D("500"), D("4"))], ("HOME", "DRAW", "AWAY"))
    plan = hedge_book(book, {"DRAW": offer("DRAW", "x", "4.0"), "AWAY": offer("AWAY", "y", "3.0")}, anchor="HOME", fraction=0)
    stakes = {leg.offer.selection: leg.stake_inr for leg in plan.legs}
    assert abs(stakes["DRAW"] - D("100")) <= D("0.02") and abs(stakes["AWAY"] - D("800")) <= D("0.02")
    assert plan.after["DRAW"] >= 0 and plan.after["AWAY"] >= 0


def test_hedge_after_a_partial_fill_resolves_the_rest() -> None:
    """3-way free bet: the DRAW leg only half filled. Re-solving AWAY with DRAW traded keeps AWAY at
    ₹0 or better; the DRAW shortfall is what the receipt reports."""
    book = single_bet("500", "4.00", outcomes=("HOME", "DRAW", "AWAY"))
    offers = {"DRAW": offer("DRAW", "x", "3.60"), "AWAY": offer("AWAY", "y", "2.80")}
    plan = hedge_book(book, offers, fraction=0)
    draw = next(leg for leg in plan.legs if leg.offer.selection == "DRAW")
    half = (draw.stake_inr / 2).quantize(D("0.01"))
    after_draw = {o: p - half + (half * D("3.60") if o == "DRAW" else 0) for o, p in book.items()}
    stakes = hedge_after_fills(after_draw, offers, anchor="HOME", target=D("0"), exclude=frozenset({"DRAW"}))
    away = stakes["AWAY"]
    assert after_draw["AWAY"] - away + away * D("2.80") >= 0


def test_free_bets_never_lose_across_many_books() -> None:
    rng = random.Random(6402)
    for _ in range(300):
        stake, own = D(str(rng.randint(10, 20_000))), D(str(round(rng.uniform(1.3, 8.0), 2)))
        others = {o: Offer(o, "b", D(str(round(rng.uniform(2.2, 9.0), 2))), rng.choice((PINNACLE, BETFAIR))) for o in ("DRAW", "AWAY")}
        book = single_bet(str(stake), str(own), outcomes=("HOME", "DRAW", "AWAY"))
        try:
            plan = hedge_book(book, others, anchor="HOME", fraction=0)
        except ArbitrageMathError:
            continue  # the other two overpriced together: no hedge exists
        assert plan.after["DRAW"] >= 0 and plan.after["AWAY"] >= 0


# ================================================================ FX rates and commission terms
def test_fx_rates_fail_closed() -> None:
    settings = get_settings().model_copy(update={"FX_MAX_AGE_SECONDS": 600, "FX_HAIRCUT_PCT": 0.5})
    now = time.time()
    fx = FxRates(None, settings, clock=lambda: now)
    rates = {"GBP": FxRate("GBP", D("110.25"), now - 60, "test"), "EUR": FxRate("EUR", D("92"), now - 3600, "test")}
    assert fx.quote("INR", rates) is None
    assert fx.quote("gbp", rates) == FxQuote("GBP", D("110.25"), D("0.005"))
    with pytest.raises(FxUnavailableError, match="stale"):
        fx.quote("EUR", rates)
    with pytest.raises(FxUnavailableError, match="missing"):
        fx.quote("USD", rates)


def venue(**overrides: object) -> VenueConfig:
    base: dict[str, object] = dict(
        id="betfair_ex_uk", display_name="Betfair", adapter="generic_json", base_url="https://api.example", auth_type="static_bearer",
        place_path="/bets", status_path="/bets", bets_per_second=D("2"), burst=2,
    )
    return VenueConfig(**{**base, **overrides})  # type: ignore[arg-type]


def test_commission_terms_come_from_the_venue_then_the_defaults() -> None:
    settings = get_settings().model_copy(update={"BOOKMAKER_CURRENCIES": {"betfair_ex_uk": "GBP"}})
    assert bookmaker_terms("betfair_ex_uk", settings).commission == D("0.05")
    assert bookmaker_terms("betfair_ex_uk", settings).currency == "GBP"
    assert bookmaker_terms("pinnacle", settings).commission == D("0")
    assert bookmaker_terms("unknown_book", settings).currency == "INR"
    own = venue(commission_rate=D("0.02"), currency="EUR")
    assert bookmaker_terms("betfair_ex_uk", settings, own).commission == D("0.02")
    assert bookmaker_terms("betfair_ex_uk", settings, own).currency == "EUR"
    # The sandbox stands in for the real bookmaker: it never changes the bookmaker's terms
    sandbox = venue(id="sandbox", is_sandbox=True, routes=("*",), commission_rate=D("0"))
    assert bookmaker_terms("betfair_ex_uk", settings, sandbox).commission == D("0.05")
    assert TermsTable(settings, [own])("betfair_ex_uk").commission == D("0.02")


# ================================================================ the live portfolio
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


RUPEE_ACCOUNTS = {"betfair_ex_uk": "INR", "smarkets": "INR"}  # the accounts these books hold are in rupees


def ctx(settings: Settings | None = None, rates: dict[str, FxRate] | None = None) -> PricingContext:
    settings = settings or get_settings().model_copy(update={"BOOKMAKER_CURRENCIES": RUPEE_ACCOUNTS})
    return PricingContext(
        terms=TermsTable(settings),
        fx=FxRates(None, settings, clock=lambda: NOW.timestamp()),
        rates=rates or {},
        now=NOW,
        max_age=timedelta(seconds=120),
        routable=lambda _: True,
    )


def book(bookmaker: str, prices: dict[str, str], age: int = 5, suspended: bool = False) -> BookLine:
    return BookLine("odds_api", bookmaker, {k: D(v) for k, v in prices.items()}, NOW - timedelta(seconds=age), suspended)


def open_bet(selection: str, stake: str, odds: str, bookmaker: str = "pinnacle", unconfirmed: bool = False, fixture: str = "fx-1") -> OpenBet:
    return OpenBet(
        id=f"bet-{selection}-{stake}", fixture_id=fixture, market="Match Odds", selection=selection, bookmaker_id=bookmaker,
        stake_inr=D(stake), odds=D(odds), status="PENDING", unconfirmed=unconfirmed, strategy=None, group_id=None,
        requested_stake_inr=None, currency="INR", stake_ccy=None, commence_time=None, created_at=NOW.isoformat(),
    )


def test_a_book_with_a_profitable_hedge_pulses() -> None:
    """HOME ₹1,000 @ 3.00 (Pinnacle); AWAY now 2.50 at Pinnacle and 2.60 at Betfair (2.52 after 5%).
    Betfair's 2.52 is the best AWAY: balanced profit 1000 * (3 * (1 - 1/2.52) - 1) = ₹809.52."""
    books = [book("pinnacle", {"HOME": "2.80", "AWAY": "2.50"}), book("betfair_ex_uk", {"HOME": "2.75", "AWAY": "2.60"})]
    row = evaluate_book("fx-1|Match Odds", [open_bet("HOME", "1000", "3.00")], books, ctx(), None)
    assert row["blocked"] is None and row["profitable"] is True
    assert row["live"]["AWAY"]["bookmaker_id"] == "betfair_ex_uk" and row["live"]["AWAY"]["rupee_odds"] == "2.5200"
    assert abs(D(row["cash_out"]) - D("809.52")) <= D("0.02")
    assert row["hedge"]["free_bet"]["after"]["AWAY"] >= "0"
    assert row["worst_case"] == "-1000.00" and row["best_case"] == "2000.00"


def test_stale_and_suspended_books_never_price_a_hedge() -> None:
    books = [book("pinnacle", {"HOME": "2.8", "AWAY": "2.5"}, age=600), book("bet365", {"HOME": "2.8", "AWAY": "9.0"}, suspended=True)]
    row = evaluate_book("fx-1|Match Odds", [open_bet("HOME", "1000", "3.00")], books, ctx(), None)
    assert row["blocked"] == "NO_HEDGE" and row["live"] == {}


def test_an_unconfirmed_bet_blocks_the_hedge() -> None:
    books = [book("pinnacle", {"HOME": "2.8", "AWAY": "2.5"})]
    row = evaluate_book("fx-1|Match Odds", [open_bet("HOME", "1000", "3.00"), open_bet("AWAY", "200", "2.4", unconfirmed=True)], books, ctx(), None)
    assert row["blocked"] == "UNCONFIRMED_BET" and row["hedge"] is None


def test_portfolio_payload_totals_and_ordering() -> None:
    books = {
        "fx-1|Match Odds": [book("pinnacle", {"HOME": "2.8", "AWAY": "2.5"})],
        "fx-2|Match Odds": [book("pinnacle", {"HOME": "2.0", "AWAY": "1.5"})],
    }
    bets = [open_bet("HOME", "1000", "3.00"), open_bet("HOME", "1000", "1.90", fixture="fx-2")]
    payload = portfolio_payload("user-1", bets, books, ctx(), {}, seq=7)
    assert payload["seq"] == 7 and payload["totals"]["open_bets"] == 2 and payload["totals"]["staked_inr"] == "2000.00"
    assert payload["markets"][0]["fixture_id"] == "fx-1" and payload["markets"][0]["profitable"]
    assert payload["markets"][1]["profitable"] is False  # 1.90 backed, 1.5 against: the lock-in is a loss
    assert payload["totals"]["profitable_hedges"] == 1


def test_scanner_finds_commission_adjusted_arbs_pre_match_only() -> None:
    metas = {
        "fx-1|Match Odds": MarketMeta("fx-1", "Match Odds", "Arsenal", "Leeds", NOW + timedelta(hours=2), {"HOME", "AWAY"}),
        "fx-2|Match Odds": MarketMeta("fx-2", "Match Odds", "Spurs", "Fulham", NOW + timedelta(hours=2), {"HOME", "AWAY"}),
        "fx-3|Match Odds": MarketMeta("fx-3", "Match Odds", "Chelsea", "Wolves", NOW - timedelta(minutes=5), {"HOME", "AWAY"}),
    }
    books = {
        # Real arb: Pinnacle 2.10 + Smarkets 2.10 at 2% (2.078): 0.476 + 0.481 = 0.957
        "fx-1|Match Odds": [book("pinnacle", {"HOME": "2.10", "AWAY": "1.80"}), book("smarkets", {"HOME": "1.70", "AWAY": "2.10"})],
        # Raw arb that Betfair's 5% erases: 2.04 + 2.00
        "fx-2|Match Odds": [book("pinnacle", {"HOME": "1.90", "AWAY": "2.00"}), book("betfair_ex_uk", {"HOME": "2.04", "AWAY": "1.80"})],
        # In play: never scanned
        "fx-3|Match Odds": [book("pinnacle", {"HOME": "2.50", "AWAY": "1.80"}), book("smarkets", {"HOME": "1.70", "AWAY": "2.50"})],
    }
    arbs = scan_arbitrage(metas, books, ctx(), get_settings())  # (the scanner reads terms from the context)
    assert [a["fixture_id"] for a in arbs] == ["fx-1"]
    legs = {leg["selection"]: leg for leg in arbs[0]["legs"]}
    assert legs["AWAY"]["bookmaker_id"] == "smarkets" and legs["AWAY"]["true_odds"] == "2.0780"
    assert D(arbs[0]["guaranteed_profit_inr"]) > 0


# ================================================================ currencies and executable prices
@pytest.mark.parametrize(
    ("bookmaker", "currency"),
    [("betfair_ex_uk", "GBP"), ("betfair_ex_au", "AUD"), ("unibet_nl", "EUR"), ("polymarket", "USD"), ("williamhill", "GBP"), ("onexbet", "INR"), ("pinnacle", "INR")],
)
def test_each_book_defaults_to_its_own_currency(bookmaker: str, currency: str) -> None:
    assert default_currency(bookmaker) == currency
    assert bookmaker_terms(bookmaker, get_settings().model_copy(update={"BOOKMAKER_CURRENCIES": {}})).currency == currency


def test_a_foreign_book_without_a_live_rate_is_left_out() -> None:
    """unibet_nl settles in EUR. No EUR/INR rate: its prices are dropped (and say so), never read as
    rupees. With a rate, its payouts come home through the haircut."""
    plain = get_settings().model_copy(update={"BOOKMAKER_CURRENCIES": {}, "FX_HAIRCUT_PCT": 0.5})
    books = [book("unibet_nl", {"HOME": "2.50", "AWAY": "1.60"}), book("pinnacle", {"HOME": "2.40", "AWAY": "1.62"})]
    context = ctx(plain)
    offers, notes = market_offers(books, context)
    assert {o.provider for o in offers} == {"pinnacle"} and notes == ["unibet_nl skipped: no live EUR/INR rate"] and context.fx_errors == {"EUR"}
    priced = ctx(plain, {"EUR": FxRate("EUR", D("101.40"), NOW.timestamp() - 30, "test")})
    offers, notes = market_offers(books, priced)
    home = next(o for o in offers if o.provider == "unibet_nl" and o.selection == "HOME")
    assert notes == [] and home.currency == "EUR" and home.rupee_odds() == D("2.4875000")  # 2.50 * 0.995


def test_prediction_market_prices_round_down_to_what_an_order_can_carry() -> None:
    """Polymarket at 31% is 1/0.31 = 3.2258064516...; an order carries 4 places, rounded down."""
    usd = ctx(rates={"USD": FxRate("USD", D("88"), NOW.timestamp(), "test")})
    offers, _ = market_offers([book("polymarket", {"HOME": "3.2258064516129035", "AWAY": "1.4492753623188406"})], usd)
    assert sorted(str(o.raw_odds) for o in offers) == ["1.4492", "3.2258"]


def test_a_foreign_stake_costs_whole_paise_and_pays_from_the_exact_amount() -> None:
    """£0.17 at ₹117.60 is ₹19.992: the ledger reserves ₹20.00 (never under-reserved), and the
    payout is computed from the ₹19.992 the pounds are actually worth."""
    gbp = FxQuote("GBP", D("117.60"), D("0.005"))
    assert leg_cost_inr(D("0.17"), gbp) == D("20.00")
    assert leg_cost_inr(D("250.00"), None) == D("250.00")
    arb = stake_arbitrage([offer("HOME", "uk_book", "2.30", PINNACLE, gbp), offer("AWAY", "pinnacle", "2.10")], "5000")
    assert arb is not None
    uk = next(leg for leg in arb.legs if leg.offer.currency == "GBP")
    assert uk.stake_inr == leg_cost_inr(uk.stake_ccy, gbp) and uk.stake_inr == uk.stake_inr.quantize(D("0.01"))
    assert uk.payout_inr == (uk.stake_ccy * D("117.60") * D("2.30") * D("0.995")).quantize(D("0.01"), rounding="ROUND_DOWN")


def test_a_balanced_book_needs_no_dust_hedge() -> None:
    """A book already level to the paisa (₹8.21 / ₹8.19 / ₹8.19): the "hedge" that would level it is
    ₹0.003 a leg, which a euro cent (₹1.02) would round into a loss. No legs; the cash-out is the book."""
    eur = FxQuote("EUR", D("102.40"), D("0.005"))
    book = {"HOME": D("8.21"), "DRAW": D("8.19"), "AWAY": D("8.19")}
    plan = hedge_book(book, {"DRAW": offer("DRAW", "unibet_nl", "3.65", PINNACLE, eur), "AWAY": offer("AWAY", "onexbet", "2.49")}, fraction=1)
    assert plan.legs == () and plan.worst_after == D("8.19")
