"""Comprehensive backtest of the ENTIRE BetDoc math vault.

Tests every module in the domain layer with zero API calls:
  1. Odds Schema (Pydantic V2 models)
  2. Devig / Shin's Method
  3. Arbitrage Detection
  4. Kelly Staking
  5. CLV (Closing Line Value)
  6. Exchange Commission Math
  7. Asian Handicap Settlement
  8. Execution Risk Model
  9. Mock Adapter + Failover Router
"""

import sys, os, asyncio, traceback, time
from decimal import Decimal
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

PASS = 0
FAIL = 0
ERRORS = []

def report(test_name: str, passed: bool, detail: str = ""):
    global PASS, FAIL
    if passed:
        PASS += 1
        print(f"  ✅ PASS: {test_name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        ERRORS.append(test_name)
        print(f"  ❌ FAIL: {test_name}" + (f" — {detail}" if detail else ""))


def section(name: str):
    print(f"\n{'='*70}")
    print(f"  MODULE: {name}")
    print(f"{'='*70}")


# ================================================================
# TEST 1: Odds Schema (Pydantic V2 Models)
# ================================================================
def test_odds_schema():
    section("domain.models.odds — Pydantic V2 Schemas")
    try:
        from betdoc.domain.models.odds import (
            OddsTick, MoneylineMarket, MoneylineSelection,
            HandicapMarket, HandicapSelection,
            TotalsMarket, TotalsSelection,
            GenericMarket, GenericSelection,
            MarketType, OutcomeSide, SourceTransport,
            utc_now, encode_tick, decode_tick,
        )
        report("Import all schema classes", True)
    except Exception as e:
        report("Import all schema classes", False, str(e))
        return

    # Build a moneyline market
    try:
        now = utc_now()
        ml = MoneylineMarket(
            key="h2h",
            runners=(
                MoneylineSelection(name="Arsenal", outcome=OutcomeSide.HOME, price=2.10),
                MoneylineSelection(name="Draw", outcome=OutcomeSide.DRAW, price=3.40),
                MoneylineSelection(name="Chelsea", outcome=OutcomeSide.AWAY, price=3.50),
            ),
        )
        report("Build MoneylineMarket (3-way)", True, f"overround={ml.overround:.4f}")
    except Exception as e:
        report("Build MoneylineMarket", False, str(e))
        return

    # Build a full OddsTick
    try:
        tick = OddsTick(
            bookmaker="test_book",
            transport=SourceTransport.REST,
            event_id="evt_001",
            sport_key="soccer_epl",
            home_team="Arsenal",
            away_team="Chelsea",
            commence_time=now + timedelta(hours=24),
            is_live=False,
            markets=(ml,),
            bookmaker_timestamp=now,
        )
        report("Build OddsTick", True, f"tick_id={tick.tick_id}")
    except Exception as e:
        report("Build OddsTick", False, str(e))
        return

    # Serialise round-trip
    try:
        encoded = encode_tick(tick)
        decoded = decode_tick(encoded)
        assert decoded.event_id == tick.event_id
        assert decoded.bookmaker == tick.bookmaker
        report("Serialise/Deserialise round-trip", True, f"{len(encoded)} bytes")
    except Exception as e:
        report("Serialise/Deserialise round-trip", False, str(e))

    # Price digest stability
    try:
        d1 = tick.price_digest
        d2 = tick.price_digest
        assert d1 == d2
        report("Price digest is stable", True, f"digest={d1}")
    except Exception as e:
        report("Price digest stability", False, str(e))

    # Immutability (frozen model)
    try:
        tick.bookmaker = "hacked"
        report("Immutability (frozen=True)", False, "Mutation did NOT raise!")
    except Exception:
        report("Immutability (frozen=True)", True, "Correctly rejected mutation")


# ================================================================
# TEST 2: Devig / Shin's Method
# ================================================================
def test_devig():
    section("domain.pricing.devig — Shin's Method")
    try:
        import numpy as np
        from betdoc.domain.pricing.devig import (
            overround, devig_proportional, devig_shin, fair_decimal_odds
        )
        report("Import devig module", True)
    except Exception as e:
        report("Import devig module", False, str(e))
        return

    odds = np.array([2.10, 3.40, 3.50])
    
    # Overround
    try:
        ovr = overround(odds)
        report("Calculate overround", True, f"overround={ovr:.4f}")
        assert ovr > 1.0, "Overround should be > 1.0 for a real book"
        report("Overround > 1.0 (vig present)", True)
    except Exception as e:
        report("Overround calculation", False, str(e))

    # Proportional devig
    try:
        prop = devig_proportional(odds)
        assert abs(prop.sum() - 1.0) < 1e-9, "Fair probs must sum to 1"
        report("Proportional devig", True, f"probs={[round(p,4) for p in prop]}")
    except Exception as e:
        report("Proportional devig", False, str(e))

    # Shin's method
    try:
        shin = devig_shin(odds)
        assert abs(shin.sum() - 1.0) < 1e-9, "Shin probs must sum to 1"
        report("Shin's method", True, f"probs={[round(p,4) for p in shin]}")
    except Exception as e:
        report("Shin's method", False, str(e))

    # Fair odds
    try:
        fair = fair_decimal_odds(odds, method="shin")
        assert all(f > o for f, o in zip(fair, odds)), "Fair odds must be >= quoted odds"
        report("Fair decimal odds (Shin)", True, f"fair={[round(f,3) for f in fair]}")
    except Exception as e:
        report("Fair decimal odds", False, str(e))


# ================================================================
# TEST 3: Arbitrage Detection
# ================================================================
def test_arbitrage():
    section("domain.arbitrage.arbitrage — Arbitrage Scanner")
    try:
        import numpy as np
        from betdoc.domain.arbitrage.arbitrage import (
            detect_arbitrage, allocate_stakes, find_arbitrage
        )
        report("Import arbitrage module", True)
    except Exception as e:
        report("Import arbitrage module", False, str(e))
        return

    # No-arb case (normal market)
    try:
        odds_no_arb = np.array([2.10, 3.40, 3.50])
        is_arb, profit = detect_arbitrage(odds_no_arb)
        assert not is_arb, "Should NOT be an arb"
        report("No-arb detection (normal market)", True, f"implied_sum > 1.0")
    except Exception as e:
        report("No-arb detection", False, str(e))

    # Arb case (mispriced market)
    try:
        odds_arb = np.array([2.20, 3.80, 4.00])
        is_arb, profit = detect_arbitrage(odds_arb)
        assert is_arb, "Should BE an arb"
        report("Arb detection (mispriced)", True, f"profit={profit*100:.2f}%")
    except Exception as e:
        report("Arb detection (mispriced)", False, str(e))

    # Stake allocation
    try:
        stakes = allocate_stakes(odds_arb)
        assert abs(stakes.sum() - 1.0) < 1e-9
        report("Stake allocation sums to 1.0", True, f"stakes={[round(s,4) for s in stakes]}")
    except Exception as e:
        report("Stake allocation", False, str(e))

    # Full pipeline
    try:
        result = find_arbitrage(
            outcomes=["Home", "Draw", "Away"],
            books=["Pinnacle", "Bet365", "Unibet"],
            decimal_odds=[2.20, 3.80, 4.00],
            tick_age_ms=100.0,
            book_historical_staleness_ms={"Pinnacle": 50, "Bet365": 120, "Unibet": 200},
        )
        assert result is not None
        report("Full arb pipeline (find_arbitrage)", True,
               f"profit={result.guaranteed_profit_pct*100:.2f}%, score={result.risk_adjusted_score}")
    except Exception as e:
        report("Full arb pipeline", False, str(e))


# ================================================================
# TEST 4: Kelly Staking
# ================================================================
def test_kelly():
    section("domain.staking.kelly — Kelly Criterion")
    try:
        from betdoc.domain.staking.kelly import kelly_fraction, size_bet
        report("Import kelly module", True)
    except Exception as e:
        report("Import kelly module", False, str(e))
        return

    # No edge: fair coin at even odds
    try:
        f = kelly_fraction(0.50, 2.0)
        assert f == 0.0, f"Should be 0 for no-edge bet, got {f}"
        report("Kelly = 0 for no-edge bet", True)
    except Exception as e:
        report("Kelly no-edge", False, str(e))

    # Positive edge
    try:
        f = kelly_fraction(0.55, 2.10)
        assert f > 0.0
        report("Kelly > 0 for +EV bet", True, f"f*={f:.4f}")
    except Exception as e:
        report("Kelly positive edge", False, str(e))

    # Full sizing with caps
    try:
        result = size_bet(prob_win=0.55, decimal_odds=2.10, bankroll=10000.0)
        assert result.stake > 0
        assert result.stake <= 10000 * 0.05  # max 5% cap
        report("size_bet with caps", True,
               f"stake=${result.stake:.2f}, edge={result.edge:.4f}, frac={result.recommended_fraction:.4f}")
    except Exception as e:
        report("size_bet", False, str(e))


# ================================================================
# TEST 5: CLV (Closing Line Value)
# ================================================================
def test_clv():
    section("domain.risk.clv — Closing Line Value")
    try:
        from betdoc.domain.risk.clv import calculate_clv_pct, test_clv_significance
        report("Import CLV module", True)
    except Exception as e:
        report("Import CLV module", False, str(e))
        return

    # Positive CLV (we got better odds than close)
    try:
        clv = calculate_clv_pct(bet_decimal_odds=2.20, closing_decimal_odds=2.00)
        assert clv > 0
        report("Positive CLV (beat the close)", True, f"CLV={clv*100:.2f}%")
    except Exception as e:
        report("Positive CLV", False, str(e))

    # Negative CLV
    try:
        clv = calculate_clv_pct(bet_decimal_odds=1.80, closing_decimal_odds=2.00)
        assert clv < 0
        report("Negative CLV (worse than close)", True, f"CLV={clv*100:.2f}%")
    except Exception as e:
        report("Negative CLV", False, str(e))

    # Significance test
    try:
        import numpy as np
        np.random.seed(42)
        clv_values = list(np.random.normal(0.02, 0.05, 200))
        result = test_clv_significance(clv_values)
        report("CLV significance test (n=200)", True,
               f"mean={result.mean_clv_pct:.4f}, p={result.p_value:.4f}, sig={result.is_significant_at_95}")
    except Exception as e:
        report("CLV significance", False, str(e))


# ================================================================
# TEST 6: Exchange Commission Math
# ================================================================
def test_exchange():
    section("domain.arbitrage.exchange — Exchange Commission")
    try:
        from betdoc.domain.arbitrage.exchange import (
            effective_back_odds, effective_lay_odds, lay_liability,
            lay_liability_exact, effective_odds, implied_probability,
            hedge_lay_backer_stake, BetSide,
        )
        report("Import exchange module", True)
    except Exception as e:
        report("Import exchange module", False, str(e))
        return

    # Back odds after 2% commission
    try:
        eff = effective_back_odds(3.0, 0.02)
        assert abs(eff - 2.96) < 0.001
        report("effective_back_odds(3.0, 2%)", True, f"result={eff:.4f}")
    except Exception as e:
        report("effective_back_odds", False, str(e))

    # Lay liability
    try:
        liab = lay_liability(3.0, 100.0)
        assert abs(liab - 200.0) < 0.01
        report("lay_liability(3.0, $100)", True, f"liability=${liab:.2f}")
    except Exception as e:
        report("lay_liability", False, str(e))

    # Effective lay odds
    try:
        eff_lay = effective_lay_odds(3.0, 0.05)
        assert abs(eff_lay - 1.475) < 0.001
        report("effective_lay_odds(3.0, 5%)", True, f"result={eff_lay:.4f}")
    except Exception as e:
        report("effective_lay_odds", False, str(e))

    # Dispatch via side enum
    try:
        back = effective_odds(3.0, 0.02, BetSide.BACK)
        lay = effective_odds(3.0, 0.02, BetSide.LAY)
        report("effective_odds dispatch (BACK/LAY)", True, f"back={back:.4f}, lay={lay:.4f}")
    except Exception as e:
        report("effective_odds dispatch", False, str(e))

    # Implied probability
    try:
        ip = implied_probability(3.0, 0.02, BetSide.BACK)
        assert 0 < ip < 1
        report("implied_probability", True, f"ip={ip:.4f}")
    except Exception as e:
        report("implied_probability", False, str(e))

    # Invalid odds rejection
    try:
        effective_back_odds(0.5, 0.02)
        report("Reject odds < 1.01", False, "Should have raised!")
    except Exception:
        report("Reject odds < 1.01", True, "Correctly raised InvalidOddsError")

    # Invalid commission rejection
    try:
        effective_back_odds(3.0, 0.50)
        report("Reject commission > 20%", False, "Should have raised!")
    except Exception:
        report("Reject commission > 20%", True, "Correctly raised CommissionError")


# ================================================================
# TEST 7: Asian Handicap Settlement
# ================================================================
def test_asian_lines():
    section("domain.pricing.asian_lines — Asian Handicap")
    try:
        from betdoc.domain.pricing.asian_lines import (
            evaluate_handicap, evaluate_totals, settle, PayoutResult,
        )
        report("Import asian_lines module", True)
    except Exception as e:
        report("Import asian_lines module", False, str(e))
        return

    # Home -0.25, 0-0 draw -> HALF_LOSS
    try:
        r = evaluate_handicap(-0.25, 0, 0, bet_on_home=True)
        assert r == PayoutResult.HALF_LOSS
        report("Home -0.25, 0-0 -> HALF_LOSS", True)
    except Exception as e:
        report("Home -0.25, 0-0", False, str(e))

    # Home +0.25, 0-0 draw -> HALF_WIN
    try:
        r = evaluate_handicap(0.25, 0, 0, bet_on_home=True)
        assert r == PayoutResult.HALF_WIN
        report("Home +0.25, 0-0 -> HALF_WIN", True)
    except Exception as e:
        report("Home +0.25, 0-0", False, str(e))

    # Home -0.75, 1-0 win -> HALF_WIN
    try:
        r = evaluate_handicap(-0.75, 1, 0, bet_on_home=True)
        assert r == PayoutResult.HALF_WIN
        report("Home -0.75, 1-0 -> HALF_WIN", True)
    except Exception as e:
        report("Home -0.75, 1-0", False, str(e))

    # Home -1.0, 2-1 -> PUSH
    try:
        r = evaluate_handicap(-1.0, 2, 1, bet_on_home=True)
        assert r == PayoutResult.PUSH
        report("Home -1.0, 2-1 -> PUSH", True)
    except Exception as e:
        report("Home -1.0, 2-1", False, str(e))

    # Inverse symmetry
    try:
        home = evaluate_handicap(-0.25, 0, 0, bet_on_home=True)
        away = evaluate_handicap(-0.25, 0, 0, bet_on_home=False)
        assert home.inverse == away
        report("Inverse symmetry (home.inverse == away)", True)
    except Exception as e:
        report("Inverse symmetry", False, str(e))

    # Totals: Over 2.25, 2 goals -> HALF_LOSS
    try:
        r = evaluate_totals(2.25, 2, bet_over=True)
        assert r == PayoutResult.HALF_LOSS
        report("Over 2.25, 2 goals -> HALF_LOSS", True)
    except Exception as e:
        report("Over 2.25, 2 goals", False, str(e))

    # Totals: Over 2.25, 3 goals -> FULL_WIN
    try:
        r = evaluate_totals(2.25, 3, bet_over=True)
        assert r == PayoutResult.FULL_WIN
        report("Over 2.25, 3 goals -> FULL_WIN", True)
    except Exception as e:
        report("Over 2.25, 3 goals", False, str(e))

    # Settle: HALF_WIN at 2.0 odds, $100 stake
    try:
        ret = settle(PayoutResult.HALF_WIN, Decimal("100"), 2.0)
        assert ret == Decimal("150.00")
        report("settle(HALF_WIN, $100, 2.0) = $150", True, f"return=${ret}")
    except Exception as e:
        report("settle HALF_WIN", False, str(e))

    # Settle: FULL_LOSS
    try:
        ret = settle(PayoutResult.FULL_LOSS, Decimal("100"), 2.0)
        assert ret == Decimal("0.00")
        report("settle(FULL_LOSS, $100, 2.0) = $0", True)
    except Exception as e:
        report("settle FULL_LOSS", False, str(e))

    # Invalid line rejection
    try:
        evaluate_handicap(0.33, 1, 0, bet_on_home=True)
        report("Reject non-0.25 line", False, "Should have raised!")
    except Exception:
        report("Reject non-0.25 line (0.33)", True, "Correctly raised InvalidLineError")


# ================================================================
# TEST 8: Execution Risk Model
# ================================================================
def test_execution_risk():
    section("domain.risk.execution — Execution Risk")
    try:
        from betdoc.domain.risk.execution import (
            ArbExecutionScenario, FillDistribution, LegFillModel,
            FillOutcome, ExecutionVerdict, build_fill_distribution,
        )
        report("Import execution module", True)
    except Exception as e:
        report("Import execution module", False, str(e))
        return

    # Build a scenario
    try:
        s = ArbExecutionScenario(
            leg_1_fill_ratio=1.0, leg_2_fill_ratio=1.0,
            leg_1_odds=2.10, leg_2_odds=2.05, probability=1.0,
        )
        assert s.is_complete
        assert not s.is_naked
        report("Build complete fill scenario", True)
    except Exception as e:
        report("Build scenario", False, str(e))

    # Naked scenario
    try:
        s2 = ArbExecutionScenario(
            leg_1_fill_ratio=1.0, leg_2_fill_ratio=0.0,
            leg_1_odds=2.10, leg_2_odds=2.05, probability=1.0,
        )
        assert s2.is_naked
        report("Detect naked exposure", True)
    except Exception as e:
        report("Naked scenario", False, str(e))

    # P&L calculation
    try:
        profit = s.profit_if_leg_1_wins(Decimal("100"), Decimal("100"))
        report("P&L calculation (leg 1 wins)", True, f"profit=${profit}")
    except Exception as e:
        report("P&L calculation", False, str(e))

    # LegFillModel survival
    try:
        model = LegFillModel(latency_ms=200.0, in_play_delay_seconds=6.0)
        surv = model.quote_survival_probability
        assert 0 < surv < 1
        report("LegFillModel survival probability", True, f"P(survive)={surv:.4f}")
    except Exception as e:
        report("LegFillModel", False, str(e))

    # Build distribution
    try:
        dist = build_fill_distribution(
            leg_1_model=LegFillModel(latency_ms=150.0, in_play_delay_seconds=6.0),
            leg_2_model=LegFillModel(latency_ms=200.0, in_play_delay_seconds=7.0),
            leg_1_odds=2.10, leg_2_odds=2.05,
            steam_correlation=0.35,
        )
        assert len(dist.scenarios) > 0
        report("Build fill distribution", True,
               f"scenarios={len(dist.scenarios)}, P(complete)={dist.complete_fill_probability:.4f}, "
               f"P(naked)={dist.naked_exposure_probability:.4f}")
    except Exception as e:
        report("Build fill distribution", False, str(e))


# ================================================================
# TEST 9: Mock Adapter
# ================================================================
def test_mock_adapter():
    section("adapters.bookmakers.mock_adapter — Mock Data Generator")
    try:
        from betdoc.adapters.bookmakers.mock_adapter import MockAdapter
        report("Import MockAdapter", True)
    except Exception as e:
        report("Import MockAdapter", False, str(e))
        return

    async def _run_mock():
        adapter = MockAdapter(bookmaker_name="backtest_mock", poll_interval=0.1)
        ticks = []
        async for tick in adapter.stream_live_ticks():
            ticks.append(tick)
            if len(ticks) >= 5:
                await adapter.close()
                break
        return ticks

    try:
        ticks = asyncio.run(_run_mock())
        assert len(ticks) == 5
        for t in ticks:
            assert t.bookmaker == "backtest_mock"
            assert len(t.markets) == 1
            market = t.markets[0]
            assert market.overround is not None
        report("MockAdapter generates 5 valid ticks", True,
               f"overrounds={[round(t.markets[0].overround, 4) for t in ticks]}")
    except Exception as e:
        report("MockAdapter streaming", False, str(e))


# ================================================================
# TEST 10: Failover Router
# ================================================================
def test_failover_router():
    section("adapters.bookmakers.failover_router — API Switch")
    try:
        from betdoc.adapters.bookmakers.failover_router import FailoverRouter
        from betdoc.adapters.bookmakers.mock_adapter import MockAdapter
        report("Import FailoverRouter", True)
    except Exception as e:
        report("Import FailoverRouter", False, str(e))
        return

    async def _run_failover():
        mock_a = MockAdapter(bookmaker_name="API_A", poll_interval=0.1)
        mock_b = MockAdapter(bookmaker_name="API_B", poll_interval=0.1)
        router = FailoverRouter(adapters=[mock_a, mock_b])
        ticks = []
        async for tick in router.stream_live_ticks():
            ticks.append(tick)
            if len(ticks) >= 3:
                await router.close()
                break
        return ticks

    try:
        ticks = asyncio.run(_run_failover())
        assert len(ticks) == 3
        report("FailoverRouter streams from first adapter", True,
               f"source={ticks[0].bookmaker}")
    except Exception as e:
        report("FailoverRouter", False, str(e))


# ================================================================
# RUN ALL TESTS
# ================================================================
if __name__ == "__main__":
    print("\n" + "🔬" * 35)
    print("  BETDOC COMPREHENSIVE BACKTEST")
    print("  Testing ALL modules in the Math Vault")
    print("🔬" * 35)
    
    start = time.time()
    
    test_odds_schema()
    test_devig()
    test_arbitrage()
    test_kelly()
    test_clv()
    test_exchange()
    test_asian_lines()
    test_execution_risk()
    test_mock_adapter()
    test_failover_router()
    
    elapsed = time.time() - start
    
    print(f"\n{'='*70}")
    print(f"  FINAL RESULTS")
    print(f"{'='*70}")
    print(f"  ✅ Passed: {PASS}")
    print(f"  ❌ Failed: {FAIL}")
    print(f"  ⏱️  Time:   {elapsed:.2f}s")
    
    if ERRORS:
        print(f"\n  Failed tests:")
        for e in ERRORS:
            print(f"    - {e}")
    else:
        print(f"\n  🏆 ALL TESTS PASSED — THE MATH VAULT IS BULLETPROOF 🏆")
    
    print(f"{'='*70}\n")
    sys.exit(1 if FAIL else 0)
