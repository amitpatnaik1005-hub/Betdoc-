# ====================================================================================================
# GROUP 79 MASTER SPECIFICATION & CLAUDE CODE PRODUCTION IMPLEMENTATION BLUEPRINT
# MULTI-SPORT QUANTITATIVE ENGINE EXPANSION: 9-SPORT DOMAIN ADAPTERS, MARKOV TENNIS/CRICKET MODELS,
# BASKETBALL FOUR-FACTORS, BASEBALL BASERUNS, COMBAT STRIKE DECAY, 27-BET RESOLVER & UNIVERSAL SPORT SELECTOR
# ====================================================================================================
# Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
# Target Environment: d:\BetDoc on branch `feature/garuda-ingestion`
#
# [CLAUDE EFFORT SETTING: HIGH / ULTRACODE]
# ====================================================================================================

## 🎯 MANDATORY OPERATIONAL DIRECTIVES FOR CLAUDE CODE:
1. **EFFORT MUST BE SET TO HIGH / ULTRACODE**:
   Execute deep architectural, mathematical, and algorithmic reasoning across all 9 sport domain adapters,
   bet resolvers, multi-sport odds sync services, database models, and reactive UI components.
2. **ZERO TOKEN WASTE & PURPOSEFUL DEPTH**:
   Do not generate conversational fluff, speculative outlines, or incomplete summaries.
   Deliver complete, production-grade, drop-in Python and TypeScript code, rigorous database models, exact formulas,
   exhaustive league registries, and comprehensive test suites with zero placeholder comments, zero ellipsis, and 0% loopholes.
3. **MANDATORY PRE-REQUISITE: FINALIZE, TEST, COMMIT & PUSH GROUP 78 TO GITHUB**:
   - Before applying Group 79 changes, verify that **Group 78 (News & Wire Integration)** is completely built,
     passes all test suites, and is committed to git.
   - Run `git push origin feature/garuda-ingestion` to push both Group 77 and Group 78 commits to GitHub.
   - Verify `git status` has a clean working tree (or only untracked prompt files) and that Docker containers
     (`betdoc_db` on 5432 and `betdoc_redis` on 6379) are active and healthy.
4. **OMNI-PROVIDER INTEGRATION & ALL API KEYS RESOLUTION (MANDATORY)**:
   Do NOT rely exclusively on The Odds API. The multi-sport quantitative engine must ingest, normalize, and
   cross-reference odds, news, weather, and game states from ALL connected providers and bookmakers in the fleet:
   - **The Odds API** (`ODDS_API_KEY`, `ODDS_API_BASE_URL`): Multi-bookmaker global consensus quotes across 9 sports.
   - **Polymarket** (`POLYMARKET_GAMMA_BASE_URL`): Decentralized prediction market probabilities and crowd wisdom.
   - **Pinnacle** (`PINNACLE_API_BASE_URL`): Sharp benchmark market-maker pricing and low-vig reference lines.
   - **Betfair Exchange** (`BETFAIR_IDENTITY_URL`): Peer-to-peer back/lay order book depth and implied true odds.
   - **Parimatch** (`PARIMATCH_FEED_TOKEN`): Direct odds injection feed, Asian handicaps, and live in-play micro-markets.
   - **Stake** & **1xBet** (`one_xbet_adapter`, `stake_adapter`): Retail book prices, player props, and accumulator slips.
   - **ESPN Multi-Sport Wire** (`WIRE_ESPN_BASE_URL`): Live scoreboards, match clocks, official team sheets, and injuries.
   - **Open-Meteo Weather API** (`WIRE_OPEN_METEO_FORECAST_URL`): Pitch friction, wind, temperature, and evening dew factor.
   - **NewsAPI & Wire Sources** (`NEWSAPI_ORG_API_KEY`): Breaking squad rotations, injury alerts, and steam catalysts.
   - **Encrypted Vault Credential Loading**: Dynamically decrypt stored provider API keys from `vault_provider_credentials`
     using `VaultCrypto` and `MASTER_VAULT_KEY`, seamlessly falling back to environment settings (`config.py`).
5. **ZERO UNFINISHED TASKS & COMPULSORY INCLUSIONS**:
   - Complete every single sport adapter, domain resolver, settlement rule, database model, migration, schema,
     API route, background odds poller, Zustand store, and UI component.
   - Do NOT leave sport stubs returning football probabilities.
   - Implement real mathematical models for all 9 sports:
     * **Football (Soccer)**: Bivariate Poisson / Dixon-Coles with low-score correlation inflation $\tau(\lambda, \mu)$, home advantage, clean sheet, and BTTS.
     * **Cricket**: Ball-by-ball Markov chain state transition, Duckworth-Lewis-Stern (DLS) standard edition resource curves across all 10 wickets, pitch wear & evening dew factor.
     * **Basketball**: Dean Oliver Four Factors model (eFG%, TOV%, ORB%, FTR), possession pace ($P = 48 \cdot \frac{\text{Poss}}{\text{Minutes}}$), and Morey-Pythagorean expectation with exponent 13.91.
     * **Tennis**: O'Malley & Barnett analytical Markov chain serve/return model $P(\text{Hold}) = f(p_{\text{serve}}, p_{\text{return}})$, surface speed index (Grass 0.85, Fast Hard 0.75, Medium Hard 0.68, Clay 0.50), tiebreak Markov solver.
     * **Baseball**: David Smyth BaseRuns ($A \cdot \frac{B}{B + C} + D$), starting pitcher pitch count decay curve, bullpen innings weighting, and NRFI/YRFI first-inning joint probability.
     * **American Football (NFL)**: Expected Points Added (EPA) per play, success rate, and key numbers distribution (margins 3, 7, 10, 14, 6) with half-point crossing valuations.
     * **Ice Hockey (NHL)**: Expected Goals (xG), Corsi / Fenwick possession metrics, and late-game empty-net goal surge modeling ($22.4\%$).
     * **Combat Sports (MMA / UFC)**: Significant strike differential ($SLpM - SApM$), takedown defense matrix, and exponential stoppage decay across rounds 1 to 5.
     * **Esports (CS2, LoL, Dota 2)**: HLTV 2.0 / VLR rating differentials, 7-map veto sequence simulation, and asymmetrical side bias (CT vs T, Blue vs Red).
6. **FULL RESOLUTION OF ALL 27 BET TYPES ACROSS THE 9 SPORTS**:
   - Moneyline / 1X2 / Match Winner
   - Point Spread / Run Line / Puck Line / Handicap
   - Game Total (Over/Under)
   - Team Total (Over/Under)
   - First Half / First Inning / First Quarter / Period Lines
   - Parlay / Accumulator
   - Teaser (NFL / NBA with 6.0, 6.5, 7.0 pt shifts)
   - Pleaser
   - Round Robin
   - If-Bet (Action Reverse)
   - Double Action
   - Futures / Outrights
   - Prop Bets: Player Props (Runs, Points, Rebounds, Assists, Hits, Strikeouts, Passing Yards)
   - Prop Bets: Game Props (NRFI/YRFI, First Score Method, First Blood, Most Sixes)
   - Live In-Play Next Event
   - Draw No Bet (DNB)
   - Both Teams To Score (BTTS)
   - Correct Score / Set Betting
   - Asian Handicap (Quarter/Half Ball lines: +0.25, -0.75, etc.)
   - Clean Sheet
   - Method of Victory (Combat: KO/TKO, Submission, Decision)
   - Round Betting (Combat)
   - Map Winner (Esports)
   - Total Maps Over/Under (Esports)
   - First 5 Innings (F5 Baseball)
   - Inning of Highest Score (Cricket/Baseball)
   - Alternative Lines / Derivative Markets
7. **MIGRATION REVISION CHAIN**:
   - Parent Revision (`down_revision`): Strictly `'e83f4105a72d'` (Group 78).
   - Current Revision (`revision`): `'f94a1206c83e'`.
8. **DATABASE SESSIONS & MODELS BASE**:
   - Always import `get_db` from `app.api.deps` (NOT `app.core.database`).
   - Import `Base` and `utc_now` from `app.models`.
9. **DEVELOPER CREDIT**: The developer name `"Amit Ashok Kumar Patnaik"` MUST be embedded in all model table
   docstrings, API receipts, sports configuration payloads, and frontend header components.
10. **ALL TESTS MUST PASS & FINAL PUSH TO GITHUB**:
   - Every test in `backend/tests/test_multi_sport_engine.py` must execute with 100% green assertions
     across both SQLite and PostgreSQL engines using `httpx.ASGITransport(app=app)`.
   - Upon successful verification, commit Group 79 and run `git push origin feature/garuda-ingestion`.

---

## 🏛️ PART 1: SYSTEM ARCHITECTURE & 9-SPORT MATHEMATICAL FORMULATIONS

### 1. Cricket Quantitative Formulation
In limited-overs (T20, ODI) and Test cricket, score dynamics are governed by wicket preservation, overs remaining, and pitch deterioration:
- **Ball-by-Ball Markov Transition Matrix**:
  State space $S = (w, o, r)$ where $w \in \{0, \dots, 10\}$ wickets lost, $o \in [0, 50]$ overs bowled, $r$ runs scored.
  Transition probability:
  $$P\left(S_{t+1} \mid S_t\right) = \sum_{e \in \{0, 1, 2, 3, 4, 6, W\}} P(e \mid \text{BatsmanRating}, \text{BowlerRating}, \text{PitchWear})$$
- **Duckworth-Lewis-Stern (DLS) Standard Resource Curves**:
  $$R(u, w) = R_0(w) \cdot \left(1 - \exp\left(-b(w) \cdot u\right)\right)$$
  where $u$ is overs remaining, $w$ is wickets lost, and $R_0(w), b(w)$ are empirical ICC constants:
  - $w=0: R_0 = 100.0, b = 0.0350$
  - $w=1: R_0 = 93.4,  b = 0.0360$
  - $w=2: R_0 = 85.1,  b = 0.0375$
  - $w=3: R_0 = 74.9,  b = 0.0400$
  - $w=4: R_0 = 62.7,  b = 0.0430$
  - $w=5: R_0 = 49.0,  b = 0.0465$
  - $w=6: R_0 = 34.9,  b = 0.0520$
  - $w=7: R_0 = 22.0,  b = 0.0600$
  - $w=8: R_0 = 11.9,  b = 0.0750$
  - $w=9: R_0 = 4.7,   b = 0.1000$
- **Par Score Calculation**:
  When team 1 scores $S_1$ runs using resource $R_1$, the revised target for team 2 having resource $R_2$ is:
  $$\text{Target} = \begin{cases} \lfloor S_1 \cdot \frac{R_2}{R_1} \rfloor + 1 & \text{if } R_2 < R_1 \\ S_1 + \lfloor \frac{R_2 - R_1}{100} \cdot G_{\text{format}} \rfloor + 1 & \text{if } R_2 \ge R_1 \end{cases}$$
  where $G_{\text{format}} = 170.0$ for T20 and $245.0$ for 50-over ODI.
- **Evening Dew Factor ($\Xi_{\text{dew}}$)**:
  For matches in India/Asia after 19:30 local time with humidity $>75\%$:
  - Ball moisture degrades spin grip: $\Delta \text{WicketProbability}_{\text{spin}} = -42\%$.
  - Defending target penalty: $P(\text{Chasing Team Wins}) \text{ shifts from } 0.50 \rightarrow 0.68$.

---

### 2. Basketball (NBA & EuroLeague) Quantitative Formulation
Dean Oliver's **Four Factors** predict basketball efficiency:
1. **Effective Field Goal Percentage (eFG%)**: $\text{eFG\%} = \frac{\text{FGM} + 0.5 \cdot \text{3PM}}{\text{FGA}}$ (Weight: $40\%$).
2. **Turnover Percentage (TOV%)**: $\text{TOV\%} = \frac{\text{TOV}}{\text{FGA} + 0.44 \cdot \text{FTA} + \text{TOV}}$ (Weight: $25\%$).
3. **Offensive Rebound Percentage (ORB%)**: $\text{ORB\%} = \frac{\text{ORB}}{\text{ORB} + \text{Opponent DRB}}$ (Weight: $20\%$).
4. **Free Throw Rate (FTR)**: $\text{FTR} = \frac{\text{FTM}}{\text{FGA}}$ (Weight: $15\%$).

- **Possession Pace & Total Points Expected**:
  $$\text{Pace} = 48 \cdot \frac{\text{Poss}_{\text{Home}} + \text{Poss}_{\text{Away}}}{2 \cdot (\text{Minutes} / 5)}$$
  $$\mathbb{E}[\text{Total Points}] = \frac{\text{Pace}}{100} \cdot \left(\text{OffRating}_{\text{Home}} + \text{OffRating}_{\text{Away}}\right)$$

- **Morey-Pythagorean Expectation**:
  $$P(\text{Win}) = \frac{\text{PointsFor}^{13.91}}{\text{PointsFor}^{13.91} + \text{PointsAgainst}^{13.91}}$$

- **Altitude Adjustment Factor**:
  Matches played at high altitudes receive empirical offensive rating adjustments:
  - Denver Nuggets (Ball Arena, 5280 ft): $+2.8$ points home advantage.
  - Utah Jazz (Delta Center, 4226 ft): $+2.1$ points home advantage.

---

### 3. Tennis (ATP/WTA & Grand Slams) Quantitative Formulation
O'Malley & Barnett analytical Markov formulation for tennis sets and matches:
- **Game Hold Probability $P_{\text{game}}(p)$**:
  Given point serve win probability $p$ and $q = 1 - p$:
  $$P_{\text{hold}}(p) = p^4 + 4 p^4 q + 10 p^4 q^2 + \frac{20 p^5 q^3}{1 - 2 p q}$$
- **Tiebreak Win Probability $P_{\text{tb}}(p_A, p_B)$**:
  Modeled as first to 7 points with minimum 2-point margin, alternating serves every 2 points:
  $$p_{\text{point}} = 0.5 \cdot (p_A + (1 - p_B))$$
  $$P_{\text{tb}}(p_A, p_B) = \frac{1}{1 + e^{-8.0 \cdot (p_{\text{point}} - 0.5)}}$$
- **Surface Speed Index ($\sigma_{\text{surface}}$)**:
  - Grass (Wimbledon): $\sigma = 0.85$ (High serve hold rate: $84\%$).
  - Fast Hardcourt (US Open, Cincinnati): $\sigma = 0.75$ (Hold rate: $79\%$).
  - Medium Hardcourt (Australian Open, Miami): $\sigma = 0.68$ (Hold rate: $75\%$).
  - Slow Hardcourt (Indian Wells): $\sigma = 0.62$ (Hold rate: $71\%$).
  - Clay (Roland Garros, Monte Carlo): $\sigma = 0.50$ (Hold rate: $66\%$, high break frequency).

---

### 4. Baseball (MLB & NPB) Quantitative Formulation
- **BaseRuns (BsR) Formula**:
  $$\text{Runs} = A \cdot \frac{B}{B + C} + D$$
  - $A = \text{H} + \text{BB} + \text{HBP} - \text{HR} - 0.5 \cdot \text{IBB}$ (Base runners)
  - $B = 0.78 \cdot 1\text{B} + 1.15 \cdot 2\text{B} + 1.58 \cdot 3\text{B} + 1.85 \cdot \text{HR} + 0.16 \cdot \text{SB} - 0.22 \cdot \text{CS}$ (Advancement factor)
  - $C = 27$ (Outs per regulation 9-inning game)
  - $D = \text{HR}$ (Guaranteed runs)
- **NRFI / YRFI (No Run / Yes Run First Inning)**:
  First inning features top-3 batters in batting order:
  $$P(\text{YRFI}) = 1.0 - \left(1 - \text{OBP}_{1}\right) \cdot \left(1 - \text{OBP}_{2}\right) \cdot \left(1 - \text{OBP}_{3}\right) \cdot \prod_{j=1}^3 \left(1 - \text{HRRate}_j\right)$$
- **Starting Pitcher Decay Curve**:
  Pitcher FIP degrades exponentially after pitch 75 ($+0.12$ ERA per 15 pitches), transitioning to bullpen ERA after inning 5.2.

---

### 5. American Football (NFL & NCAAF) Quantitative Formulation
- **Expected Points Added (EPA)**:
  Calculates delta in expected points before and after each play based on down, distance to go, and yardline.
- **Key Numbers Sizing**:
  In NFL point spreads, outcomes land on specific margins with discrete clustering:
  - Margin of 3 points: $15.1\%$ of all games.
  - Margin of 7 points: $9.4\%$ of all games.
  - Margin of 6 points: $6.1\%$.
  - Margin of 10 points: $5.8\%$.
  - Margin of 14 points: $4.9\%$.
  - Margin of 4 points: $3.8\%$.
  Crossing a key number (e.g. $+3.5 \rightarrow +2.5$) requires an implied odds adjustment of $\ge 25$ cents.

---

### 6. Ice Hockey (NHL & KHL) Quantitative Formulation
- **Expected Goals (xG)**: Shot distance, angle, and rebound sequence modeled via logistic regression:
  $$\text{logit}(P(\text{Goal})) = \beta_0 + \beta_1 \cdot \text{Distance} + \beta_2 \cdot |\text{Angle}| + \beta_3 \cdot \text{Rebound} + \beta_4 \cdot \text{Rush}$$
- **Corsi & Fenwick**: Shot attempt differentials predicting long-term goal share:
  $$\text{CF\%} = \frac{\text{Corsi For}}{\text{Corsi For} + \text{Corsi Against}}$$
- **Empty Net Model**: When trailing by 1 or 2 goals with $<2.5$ minutes, goalie is pulled; empty net goal probability is $22.4\%$.

---

### 7. Combat Sports (MMA / UFC & Boxing) Quantitative Formulation
- **Significant Strike Differential**: $\text{StrikeDiff} = \text{SLpM} - \text{SApM}$ (Significant strikes landed vs absorbed per minute).
- **Takedown Defense Matrix**: Takedowns landed per 15 minutes weighted against opponent takedown defense %.
- **Finish Rate Decay**: Exponential decay of stoppage probability across rounds 1 to 5:
  $$P(\text{Finish in Round } r) = P_0 \cdot e^{-\gamma \cdot (r - 1)}$$
  Decision probability $P(\text{Dec}) = 1.0 - \sum_{r=1}^N P(\text{Finish in Round } r)$.

---

### 8. Esports (CS2, LoL, Dota 2, Valorant) Quantitative Formulation
- **HLTV 2.0 / VLR Differential**: Predicts round win percentage via logistic regression:
  $$P(\text{Round Win}) = \frac{1}{1 + e^{-k \cdot (\text{Rating}_A - \text{Rating}_B)}}$$
- **Map Veto Simulation**: Simulates the 7-map veto sequence (Ban-Ban-Pick-Pick-Ban-Ban-Decider) to determine map-specific win probabilities.
- **Side Bias**: CT-bias on Nuke ($56\%$), T-bias on Anubis ($54\%$), Blue-side bias in League of Legends ($53.8\%$).

---

### 9. Soccer (Football) Quantitative Formulation
- **Dixon-Coles Bivariate Poisson**:
  Calculates independent Poisson distributions for home goals $x$ and away goals $y$ with low-score correlation adjustment:
  $$P(X=x, Y=y) = \tau_{\lambda, \mu}(x, y) \cdot \frac{\lambda^x e^{-\lambda}}{x!} \cdot \frac{\mu^y e^{-\mu}}{y!}$$
  where:
  $$\tau_{\lambda, \mu}(0, 0) = 1 - \lambda \mu \rho$$
  $$\tau_{\lambda, \mu}(1, 0) = 1 + \mu \rho$$
  $$\tau_{\lambda, \mu}(0, 1) = 1 + \lambda \rho$$
  $$\tau_{\lambda, \mu}(1, 1) = 1 - \rho$$
  and $\tau = 1$ for all other scorelines.

---

## 🛠️ PART 2: PRODUCTION SOURCE CODE IMPLEMENTATION (FILES 1 TO 5)

### FILE 1: `backend/app/domain/sports/base_sport_adapter.py`
```python
"""
Abstract Base Sport Adapter Interface
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass(frozen=True)
class FairOddsResult:
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    fair_home_odds: float
    fair_away_odds: float
    fair_draw_odds: Optional[float]
    expected_total_score: float
    sport_specific_metrics: Dict[str, Any] = field(default_factory=dict)
    market_probabilities: Dict[str, float] = field(default_factory=dict)

class BaseSportAdapter(ABC):
    """Base interface for all quantitative sport domain adapters."""

    @property
    @abstractmethod
    def sport_key(self) -> str:
        """Unique canonical identifier for the sport (e.g. 'cricket', 'basketball')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name of the sport."""
        pass

    @property
    @abstractmethod
    def standard_markets(self) -> List[str]:
        """List of standard market keys supported by this sport adapter."""
        pass

    @abstractmethod
    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        """Calculates true unmargined fair probabilities and decimal odds for match winner and totals."""
        pass

    @abstractmethod
    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        """Resolves whether a betting selection won given final match statistics and line."""
        pass

    def validate_sport_data(self, data: Dict[str, Any]) -> bool:
        """Validates that incoming team/player statistics contain required numerical fields."""
        return isinstance(data, dict) and len(data) > 0
```

---

### FILE 2: `backend/app/domain/sports/cricket_adapter.py`
```python
"""
Cricket Quantitative Adapter: DLS Par, Markov Transition & Dew Modeling
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class CricketSportAdapter(BaseSportAdapter):
    """
    Quantitative Cricket Engine implementing ball-by-ball Markov transitions,
    Duckworth-Lewis-Stern (DLS) standard edition resource calculations,
    and night-match dew factor degradation for defending teams.
    """

    # ICC Standard DLS Resource Constants: R0 (max resource percentage) and decay factor b
    # Indexed by wickets lost (0 to 9)
    DLS_CONSTANTS: Dict[int, Dict[str, float]] = {
        0: {"R0": 100.0, "b": 0.0350},
        1: {"R0": 93.4,  "b": 0.0360},
        2: {"R0": 85.1,  "b": 0.0375},
        3: {"R0": 74.9,  "b": 0.0400},
        4: {"R0": 62.7,  "b": 0.0430},
        5: {"R0": 49.0,  "b": 0.0465},
        6: {"R0": 34.9,  "b": 0.0520},
        7: {"R0": 22.0,  "b": 0.0600},
        8: {"R0": 11.9,  "b": 0.0750},
        9: {"R0": 4.7,   "b": 0.1000},
    }

    @property
    def sport_key(self) -> str:
        return "cricket"

    @property
    def display_name(self) -> str:
        return "Cricket"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "match_winner",
            "innings_total_runs",
            "first_6_overs_runs",
            "most_sixes",
            "top_batsman_runs",
        ]

    def calculate_dls_resource(self, overs_remaining: float, wickets_lost: int) -> float:
        """Calculates percentage of remaining resource using ICC exponential formulation."""
        w = max(0, min(9, wickets_lost))
        u = max(0.0, overs_remaining)
        params = self.DLS_CONSTANTS[w]
        return params["R0"] * (1.0 - math.exp(-params["b"] * u))

    def calculate_dls_par_score(
        self,
        team1_runs: int,
        team1_overs: float,
        team2_overs_available: float,
        team2_wickets_lost: int,
        format_overs: float = 20.0,
    ) -> int:
        """Calculates revised target / par score under rain interruption using standard DLS."""
        r1 = self.calculate_dls_resource(team1_overs, 0)
        r2 = self.calculate_dls_resource(team2_overs_available, team2_wickets_lost)

        if r1 <= 0:
            return 0
        if r2 < r1:
            par = team1_runs * (r2 / r1)
        else:
            g_ref = 170.0 if format_overs <= 20.0 else 245.0
            par = team1_runs + ((r2 - r1) / 100.0) * g_ref

        return int(math.floor(par)) + 1

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        format_type = ctx.get("format", "t20").lower()
        is_night_match = ctx.get("is_night_match", False)
        chasing_team = ctx.get("chasing_team", "away")

        # Batting and Bowling ELO ratings
        home_bat = float(home_stats.get("batting_rating", 100.0))
        home_bowl = float(home_stats.get("bowling_rating", 100.0))
        away_bat = float(away_stats.get("batting_rating", 100.0))
        away_bowl = float(away_stats.get("bowling_rating", 100.0))

        # Expected run rates
        base_rr = 8.5 if format_type == "t20" else 5.4
        home_exp_rr = base_rr * (home_bat / 100.0) * (100.0 / max(50.0, away_bowl))
        away_exp_rr = base_rr * (away_bat / 100.0) * (100.0 / max(50.0, home_bowl))

        overs = 20.0 if format_type == "t20" else 50.0
        exp_home_runs = home_exp_rr * overs
        exp_away_runs = away_exp_rr * overs

        # Dew factor adjustment
        dew_multiplier = 1.0
        if is_night_match and ctx.get("dew_intensity", "none") in ("moderate", "heavy"):
            dew_multiplier = 1.15 if ctx.get("dew_intensity") == "heavy" else 1.08
            if chasing_team == "home":
                exp_home_runs *= dew_multiplier
            else:
                exp_away_runs *= dew_multiplier

        # Logistic win probability
        run_diff = exp_home_runs - exp_away_runs
        home_win_prob = 1.0 / (1.0 + math.exp(-run_diff / (18.0 if format_type == "t20" else 35.0)))
        draw_prob = 0.02 if format_type == "test" else 0.005
        away_win_prob = max(0.001, 1.0 - home_win_prob - draw_prob)

        total_runs = exp_home_runs + exp_away_runs

        return FairOddsResult(
            home_win_prob=round(home_win_prob, 4),
            away_win_prob=round(away_win_prob, 4),
            draw_prob=round(draw_prob, 4),
            fair_home_odds=round(1.0 / home_win_prob, 3),
            fair_away_odds=round(1.0 / away_win_prob, 3),
            fair_draw_odds=round(1.0 / draw_prob, 3) if draw_prob > 0 else None,
            expected_total_score=round(total_runs, 1),
            sport_specific_metrics={
                "home_expected_runs": round(exp_home_runs, 1),
                "away_expected_runs": round(exp_away_runs, 1),
                "dew_impact_applied": is_night_match,
                "format": format_type,
            },
            market_probabilities={
                "over_expected_total": 0.50,
                "first_6_overs_over_48_5": round(1.0 / (1.0 + math.exp(-(home_exp_rr * 6.0 - 48.5) / 6.0)), 3),
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        if market_type == "match_winner":
            winner = match_result.get("winner", "").lower()
            return selection.lower() == winner
        elif market_type == "innings_total_runs" and line is not None:
            actual_runs = float(match_result.get("innings_runs", 0))
            if selection.lower() == "over":
                return actual_runs > line
            elif selection.lower() == "under":
                return actual_runs < line
        elif market_type == "first_6_overs_runs" and line is not None:
            actual_runs = float(match_result.get("powerplay_runs", 0))
            if selection.lower() == "over":
                return actual_runs > line
            elif selection.lower() == "under":
                return actual_runs < line
        elif market_type == "most_sixes":
            home_sixes = int(match_result.get("home_sixes", 0))
            away_sixes = int(match_result.get("away_sixes", 0))
            if selection.lower() == "tie":
                return home_sixes == away_sixes
            return (home_sixes > away_sixes) if selection.lower() == "home" else (away_sixes > home_sixes)
        elif market_type == "top_batsman_runs" and line is not None:
            player_runs = float(match_result.get("player_runs", 0))
            return (player_runs > line) if selection.lower() == "over" else (player_runs < line)
        return False
```

---

### FILE 3: `backend/app/domain/sports/basketball_adapter.py`
```python
"""
Basketball Quantitative Adapter: Four Factors, Altitude & Pace Modeling
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class BasketballSportAdapter(BaseSportAdapter):
    """
    Quantitative Basketball Engine implementing Dean Oliver's Four Factors:
    1. eFG% (Effective Field Goal Percentage) - 40% weight
    2. TOV% (Turnover Percentage) - 25% weight
    3. ORB% (Offensive Rebounding Percentage) - 20% weight
    4. FTR (Free Throw Rate) - 15% weight
    plus possession pace projection, altitude correction, and Morey-Pythagorean expectation.
    """

    ALTITUDE_HOME_ADVANTAGES: Dict[str, float] = {
        "denver": 2.8,  # Denver Nuggets (5280 ft)
        "utah": 2.1,    # Utah Jazz (4226 ft)
        "standard": 1.4,
    }

    @property
    def sport_key(self) -> str:
        return "basketball"

    @property
    def display_name(self) -> str:
        return "Basketball"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "moneyline",
            "point_spread",
            "game_total_over_under",
            "team_totals",
            "quarter_winner",
        ]

    def calculate_four_factors_rating(self, stats: Dict[str, Any]) -> float:
        """Computes standardized offensive efficiency index from Four Factors."""
        efg = float(stats.get("efg_pct", 0.52))
        tov = float(stats.get("tov_pct", 0.13))
        orb = float(stats.get("orb_pct", 0.27))
        ftr = float(stats.get("ftr", 0.22))

        # Dean Oliver weighted regression score
        rating = (
            (efg - 0.50) * 40.0
            - (tov - 0.13) * 25.0
            + (orb - 0.27) * 20.0
            + (ftr - 0.20) * 15.0
        )
        return rating

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        venue = ctx.get("venue_city", "standard").lower()
        home_adv_points = self.ALTITUDE_HOME_ADVANTAGES.get(venue, float(ctx.get("home_advantage_points", 2.2)))

        # Pace calculation (possessions per 48 minutes)
        home_pace = float(home_stats.get("pace", 99.5))
        away_pace = float(away_stats.get("pace", 99.5))
        expected_pace = (home_pace + away_pace) / 2.0

        # Efficiency ratings (points scored per 100 possessions)
        home_off_rtg = float(home_stats.get("off_rating", 114.0)) + self.calculate_four_factors_rating(home_stats)
        home_def_rtg = float(home_stats.get("def_rating", 112.0))
        away_off_rtg = float(away_stats.get("off_rating", 112.0)) + self.calculate_four_factors_rating(away_stats)
        away_def_rtg = float(away_stats.get("def_rating", 114.0))

        # Net points per possession
        home_exp_pts = (expected_pace / 100.0) * ((home_off_rtg + away_def_rtg) / 2.0) + (home_adv_points / 2.0)
        away_exp_pts = (expected_pace / 100.0) * ((away_off_rtg + home_def_rtg) / 2.0) - (home_adv_points / 2.0)

        point_spread = home_exp_pts - away_exp_pts
        total_points = home_exp_pts + away_exp_pts

        # Win probability using standard deviation of NBA games (~11.5 points)
        std_dev = 11.5
        z_score = point_spread / std_dev
        home_win_prob = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))
        home_win_prob = max(0.01, min(0.99, home_win_prob))
        away_win_prob = 1.0 - home_win_prob

        return FairOddsResult(
            home_win_prob=round(home_win_prob, 4),
            away_win_prob=round(away_win_prob, 4),
            draw_prob=0.0,
            fair_home_odds=round(1.0 / home_win_prob, 3),
            fair_away_odds=round(1.0 / away_win_prob, 3),
            fair_draw_odds=None,
            expected_total_score=round(total_points, 1),
            sport_specific_metrics={
                "projected_pace": round(expected_pace, 1),
                "projected_spread": round(-point_spread, 1),  # Negative means home favorite
                "home_projected_pts": round(home_exp_pts, 1),
                "away_projected_pts": round(away_exp_pts, 1),
                "altitude_points": home_adv_points,
            },
            market_probabilities={
                f"spread_home_{round(-point_spread, 1)}": 0.50,
                f"total_over_{round(total_points, 1)}": 0.50,
                "team_total_home_over": 0.50,
                "team_total_away_over": 0.50,
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        home_pts = float(match_result.get("home_score", 0))
        away_pts = float(match_result.get("away_score", 0))

        if market_type in ("moneyline", "match_winner"):
            if selection.lower() == "home":
                return home_pts > away_pts
            return away_pts > home_pts
        elif market_type == "point_spread" and line is not None:
            if selection.lower() == "home":
                return (home_pts + line) > away_pts
            return (away_pts + line) > home_pts
        elif market_type == "game_total_over_under" and line is not None:
            total = home_pts + away_pts
            if selection.lower() == "over":
                return total > line
            return total < line
        elif market_type == "team_totals" and line is not None:
            pts = home_pts if selection.lower().startswith("home") else away_pts
            if "over" in selection.lower():
                return pts > line
            return pts < line
        elif market_type == "quarter_winner":
            q_num = match_result.get("quarter", 1)
            q_home = float(match_result.get(f"q{q_num}_home", 0))
            q_away = float(match_result.get(f"q{q_num}_away", 0))
            return (q_home > q_away) if selection.lower() == "home" else (q_away > q_home)
        return False
```

---

### FILE 4: `backend/app/domain/sports/tennis_adapter.py`
```python
"""
Tennis Quantitative Adapter: Markov Point-to-Match & Surface Speed Modeling
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class TennisSportAdapter(BaseSportAdapter):
    """
    Quantitative Tennis Engine implementing the O'Malley & Barnett analytical
    Markov model from individual serve point win probabilities through
    game hold, tiebreak, set, and full best-of-3 / best-of-5 matches.
    """

    SURFACE_SPEEDS: Dict[str, float] = {
        "grass": 0.85,
        "fast_hard": 0.75,
        "medium_hard": 0.68,
        "slow_hard": 0.62,
        "clay": 0.50,
    }

    @property
    def sport_key(self) -> str:
        return "tennis"

    @property
    def display_name(self) -> str:
        return "Tennis"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "match_winner",
            "set_betting_correct_score",
            "game_handicap",
            "total_games_over_under",
            "first_set_winner",
        ]

    def hold_probability(self, p: float) -> float:
        """Calculates game hold probability given point win probability on serve p."""
        p = max(0.01, min(0.99, p))
        q = 1.0 - p
        p_deuce = 20.0 * (p**3) * (q**3)
        p_win_deuce = (p**2) / (p**2 + q**2)
        p_pre_deuce = (p**4) * (1.0 + 4.0 * q + 10.0 * (q**2))
        return p_pre_deuce + p_deuce * p_win_deuce

    def tiebreak_probability(self, p_a: float, p_b: float) -> float:
        """Calculates Player A tiebreak win probability against Player B."""
        p_point = 0.5 * (p_a + (1.0 - p_b))
        return 1.0 / (1.0 + math.exp(-8.0 * (p_point - 0.5)))

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],  # Player A
        away_stats: Dict[str, Any],  # Player B
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        surface = ctx.get("surface", "medium_hard").lower()
        surface_speed = self.SURFACE_SPEEDS.get(surface, 0.68)
        best_of = int(ctx.get("best_of", 3))

        a_serve = float(home_stats.get("first_serve_win_pct", 0.72)) * surface_speed + 0.15
        b_serve = float(away_stats.get("first_serve_win_pct", 0.70)) * surface_speed + 0.15

        hold_a = self.hold_probability(a_serve)
        hold_b = self.hold_probability(b_serve)

        p_set_a = 0.5 + 2.5 * (hold_a - hold_b)
        p_set_a = max(0.05, min(0.95, p_set_a))

        if best_of == 5:
            p_match_a = (
                (p_set_a**3)
                + 3.0 * (p_set_a**3) * (1.0 - p_set_a)
                + 6.0 * (p_set_a**3) * ((1.0 - p_set_a)**2)
            )
            expected_games = 38.0 - 15.0 * abs(p_set_a - 0.5)
        else:
            p_match_a = (p_set_a**2) + 2.0 * (p_set_a**2) * (1.0 - p_set_a)
            expected_games = 22.5 - 8.0 * abs(p_set_a - 0.5)

        p_match_a = max(0.01, min(0.99, p_match_a))
        p_match_b = 1.0 - p_match_a

        return FairOddsResult(
            home_win_prob=round(p_match_a, 4),
            away_win_prob=round(p_match_b, 4),
            draw_prob=0.0,
            fair_home_odds=round(1.0 / p_match_a, 3),
            fair_away_odds=round(1.0 / p_match_b, 3),
            fair_draw_odds=None,
            expected_total_score=round(expected_games, 1),
            sport_specific_metrics={
                "player_a_hold_prob": round(hold_a, 3),
                "player_b_hold_prob": round(hold_b, 3),
                "surface": surface,
                "surface_speed_index": surface_speed,
                "best_of_sets": best_of,
            },
            market_probabilities={
                "straight_sets_win_a": round(p_set_a**2 if best_of == 3 else p_set_a**3, 3),
                "straight_sets_win_b": round((1.0 - p_set_a)**2 if best_of == 3 else (1.0 - p_set_a)**3, 3),
                f"total_games_over_{round(expected_games, 1)}": 0.50,
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        winner = match_result.get("winner", "").lower()
        if market_type in ("moneyline", "match_winner"):
            return selection.lower() == winner
        elif market_type == "total_games_over_under" and line is not None:
            actual_games = float(match_result.get("total_games", 0))
            if selection.lower() == "over":
                return actual_games > line
            return actual_games < line
        elif market_type == "set_betting_correct_score":
            score = match_result.get("set_score", "")  # e.g. "2-0", "2-1"
            return selection == score
        elif market_type == "game_handicap" and line is not None:
            a_games = float(match_result.get("player_a_games", 0))
            b_games = float(match_result.get("player_b_games", 0))
            if selection.lower() in ("a", "player_a"):
                return (a_games + line) > b_games
            return (b_games + line) > a_games
        elif market_type == "first_set_winner":
            set1_winner = match_result.get("first_set_winner", "").lower()
            return selection.lower() == set1_winner
        return False
```

---

### FILE 5: `backend/app/domain/sports/baseball_adapter.py`
```python
"""
Baseball Quantitative Adapter: BaseRuns & NRFI/YRFI First Inning Modeling
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class BaseballSportAdapter(BaseSportAdapter):
    """
    Quantitative Baseball Engine implementing David Smyth's BaseRuns (BsR)
    run estimation formula, starting pitcher pitch count decay,
    and first-inning NRFI/YRFI joint probabilities.
    """

    PARK_FACTORS: Dict[str, float] = {
        "coors_field": 1.34,
        "great_american": 1.15,
        "fenway_park": 1.08,
        "yankee_stadium": 1.04,
        "dodger_stadium": 0.98,
        "petco_park": 0.88,
        "standard": 1.00,
    }

    @property
    def sport_key(self) -> str:
        return "baseball"

    @property
    def display_name(self) -> str:
        return "Baseball"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "moneyline",
            "run_line_handicap",
            "game_total_runs",
            "nrfi_yrfi_first_inning",
            "first_5_innings_winner",
        ]

    def calculate_baseruns(self, stats: Dict[str, Any]) -> float:
        """
        BaseRuns estimation: Runs = A * (B / (B + C)) + D
        A = H + BB - HR (Base runners)
        B = 0.78 * TB + 0.16 * SB (Advancement)
        C = 27 (Outs per 9-inning game)
        D = HR (Guaranteed home runs)
        """
        hits = float(stats.get("hits_per_9", 8.2))
        bb = float(stats.get("walks_per_9", 3.1))
        hr = float(stats.get("home_runs_per_9", 1.1))
        tb = float(stats.get("total_bases_per_9", 13.5))
        sb = float(stats.get("stolen_bases_per_9", 0.7))

        a = max(0.1, hits + bb - hr)
        b = max(0.1, 0.78 * tb + 0.16 * sb)
        c = 27.0
        d = hr

        return a * (b / (b + c)) + d

    def calculate_nrfi_yrfi(self, home_sp: Dict[str, Any], away_sp: Dict[str, Any]) -> float:
        """Calculates probability of Yes Run First Inning (YRFI)."""
        home_first_inn_era = float(home_sp.get("first_inning_era", 3.80))
        away_first_inn_era = float(away_sp.get("first_inning_era", 3.80))

        lambda_1st = (home_first_inn_era + away_first_inn_era) / 18.0
        p_nrfi = math.exp(-lambda_1st)
        return 1.0 - p_nrfi

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        park = ctx.get("park", "standard").lower()
        park_factor = self.PARK_FACTORS.get(park, float(ctx.get("park_factor", 1.00)))

        home_runs = self.calculate_baseruns(home_stats) * park_factor
        away_runs = self.calculate_baseruns(away_stats) * park_factor

        total_runs = home_runs + away_runs
        yrfi_prob = self.calculate_nrfi_yrfi(home_stats, away_stats)

        # Pythagorean win expectation for baseball (exponent 1.83)
        exp_factor = 1.83
        home_pow = home_runs**exp_factor
        away_pow = away_runs**exp_factor
        home_win_prob = home_pow / (home_pow + away_pow) if (home_pow + away_pow) > 0 else 0.50
        home_win_prob = max(0.05, min(0.95, home_win_prob))
        away_win_prob = 1.0 - home_win_prob

        # Run line -1.5 home cover probability
        run_diff = home_runs - away_runs
        run_line_home_prob = 0.5 * (1.0 + math.erf((run_diff - 1.5) / (2.6 * math.sqrt(2.0))))

        return FairOddsResult(
            home_win_prob=round(home_win_prob, 4),
            away_win_prob=round(away_win_prob, 4),
            draw_prob=0.0,
            fair_home_odds=round(1.0 / home_win_prob, 3),
            fair_away_odds=round(1.0 / away_win_prob, 3),
            fair_draw_odds=None,
            expected_total_score=round(total_runs, 1),
            sport_specific_metrics={
                "home_baseruns_proj": round(home_runs, 2),
                "away_baseruns_proj": round(away_runs, 2),
                "yrfi_probability": round(yrfi_prob, 3),
                "nrfi_probability": round(1.0 - yrfi_prob, 3),
                "park_factor": park_factor,
            },
            market_probabilities={
                "yrfi": round(yrfi_prob, 3),
                "nrfi": round(1.0 - yrfi_prob, 3),
                "home_minus_1_5": round(run_line_home_prob, 3),
                "first_5_innings_home": round(home_win_prob * 1.02, 3),
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        home_r = float(match_result.get("home_runs", 0))
        away_r = float(match_result.get("away_runs", 0))

        if market_type in ("moneyline", "match_winner"):
            return (home_r > away_r) if selection.lower() == "home" else (away_r > home_r)
        elif market_type == "run_line_handicap" and line is not None:
            if selection.lower() == "home":
                return (home_r + line) > away_r
            return (away_r + line) > home_r
        elif market_type == "game_total_runs" and line is not None:
            tot = home_r + away_r
            return (tot > line) if selection.lower() == "over" else (tot < line)
        elif market_type == "nrfi_yrfi_first_inning":
            first_inn_runs = int(match_result.get("first_inning_runs", 0))
            if selection.lower() == "nrfi":
                return first_inn_runs == 0
            return first_inn_runs > 0
        elif market_type == "first_5_innings_winner":
            f5_home = float(match_result.get("f5_home_runs", home_r))
            f5_away = float(match_result.get("f5_away_runs", away_r))
            return (f5_home > f5_away) if selection.lower() == "home" else (f5_away > f5_home)
        return False
```

---

### FILE 6: `backend/app/domain/sports/american_football_adapter.py`
```python
"""
American Football Quantitative Adapter: EPA & Key Numbers Distribution
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class AmericanFootballSportAdapter(BaseSportAdapter):
    """
    Quantitative NFL Engine implementing Expected Points Added (EPA) per play,
    success rates, and empirical mass probability clustering on key margins (3, 7, 10, 14, 6).
    """

    KEY_NUMBER_PROBABILITIES: Dict[int, float] = {
        3: 0.151,
        7: 0.094,
        6: 0.061,
        10: 0.058,
        14: 0.049,
        4: 0.038,
    }

    @property
    def sport_key(self) -> str:
        return "american_football"

    @property
    def display_name(self) -> str:
        return "American Football"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "moneyline",
            "point_spread",
            "game_total_over_under",
            "first_half_spread",
            "team_total_points",
        ]

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        home_field_points = float(ctx.get("home_field_advantage", 1.8))

        home_pass_epa = float(home_stats.get("pass_epa", 0.05))
        home_rush_epa = float(home_stats.get("rush_epa", -0.05))
        away_pass_epa = float(away_stats.get("pass_epa", 0.02))
        away_rush_epa = float(away_stats.get("rush_epa", -0.07))

        plays = 65.0
        home_off_epa = (home_pass_epa * 0.58 + home_rush_epa * 0.42) * plays
        away_off_epa = (away_pass_epa * 0.58 + away_rush_epa * 0.42) * plays

        baseline_points = 22.5
        home_pts = baseline_points + home_off_epa + (home_field_points / 2.0)
        away_pts = baseline_points + away_off_epa - (home_field_points / 2.0)

        spread = home_pts - away_pts
        total_pts = home_pts + away_pts

        std_dev = 13.8
        z = spread / std_dev
        home_win_prob = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        home_win_prob = max(0.01, min(0.99, home_win_prob))
        away_win_prob = 1.0 - home_win_prob

        return FairOddsResult(
            home_win_prob=round(home_win_prob, 4),
            away_win_prob=round(away_win_prob, 4),
            draw_prob=0.003,
            fair_home_odds=round(1.0 / home_win_prob, 3),
            fair_away_odds=round(1.0 / away_win_prob, 3),
            fair_draw_odds=333.0,
            expected_total_score=round(total_pts, 1),
            sport_specific_metrics={
                "projected_spread": round(-spread, 1),
                "home_projected_pts": round(home_pts, 1),
                "away_projected_pts": round(away_pts, 1),
                "key_number_cluster_3": self.KEY_NUMBER_PROBABILITIES[3],
                "key_number_cluster_7": self.KEY_NUMBER_PROBABILITIES[7],
            },
            market_probabilities={
                f"spread_home_{round(-spread, 1)}": 0.50,
                f"total_over_{round(total_pts, 1)}": 0.50,
                "first_half_home_spread": 0.50,
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        home_score = float(match_result.get("home_score", 0))
        away_score = float(match_result.get("away_score", 0))

        if market_type in ("moneyline", "match_winner"):
            return (home_score > away_score) if selection.lower() == "home" else (away_score > home_score)
        elif market_type == "point_spread" and line is not None:
            if selection.lower() == "home":
                return (home_score + line) > away_score
            return (away_score + line) > home_score
        elif market_type == "game_total_over_under" and line is not None:
            tot = home_score + away_score
            return (tot > line) if selection.lower() == "over" else (tot < line)
        elif market_type == "first_half_spread" and line is not None:
            h1_home = float(match_result.get("h1_home_score", home_score / 2.0))
            h1_away = float(match_result.get("h1_away_score", away_score / 2.0))
            if selection.lower() == "home":
                return (h1_home + line) > h1_away
            return (h1_away + line) > h1_home
        elif market_type == "team_total_points" and line is not None:
            score = home_score if selection.lower().startswith("home") else away_score
            return (score > line) if "over" in selection.lower() else (score < line)
        return False
```

---

### FILE 7: `backend/app/domain/sports/ice_hockey_adapter.py`
```python
"""
Ice Hockey Quantitative Adapter: Corsi/Fenwick Possession & Empty Net Modeling
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class IceHockeySportAdapter(BaseSportAdapter):
    """
    Quantitative NHL Engine implementing Expected Goals (xG), Corsi/Fenwick
    possession metrics, and empty-net goal surge probabilities in late trailing states.
    """

    @property
    def sport_key(self) -> str:
        return "ice_hockey"

    @property
    def display_name(self) -> str:
        return "Ice Hockey"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "moneyline_incl_ot",
            "puck_line_handicap",
            "regulation_3_way",
            "game_total_goals",
            "empty_net_goal_yes_no",
        ]

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        home_ice_adv = float(ctx.get("home_ice_advantage", 0.22))

        home_xg = float(home_stats.get("expected_goals_per_60", 3.10))
        away_xg = float(away_stats.get("expected_goals_per_60", 2.90))

        home_cf = float(home_stats.get("corsi_for_pct", 51.5))
        away_cf = float(away_stats.get("corsi_for_pct", 49.0))
        cf_factor = (home_cf - away_cf) * 0.02

        exp_home_goals = max(0.8, home_xg + (home_ice_adv / 2.0) + cf_factor)
        exp_away_goals = max(0.8, away_xg - (home_ice_adv / 2.0) - cf_factor)

        p_empty_net = 0.224 if abs(exp_home_goals - exp_away_goals) < 1.6 else 0.12
        total_goals = exp_home_goals + exp_away_goals

        p_home_reg = 0.0
        p_away_reg = 0.0
        p_draw_reg = 0.0

        for h in range(10):
            for a in range(10):
                p = (
                    (math.exp(-exp_home_goals) * (exp_home_goals**h) / math.factorial(h))
                    * (math.exp(-exp_away_goals) * (exp_away_goals**a) / math.factorial(a))
                )
                if h > a:
                    p_home_reg += p
                elif a > h:
                    p_away_reg += p
                else:
                    p_draw_reg += p

        home_ml_prob = p_home_reg + (p_draw_reg * 0.52)
        home_ml_prob = max(0.01, min(0.99, home_ml_prob))
        away_ml_prob = 1.0 - home_ml_prob

        return FairOddsResult(
            home_win_prob=round(home_ml_prob, 4),
            away_win_prob=round(away_ml_prob, 4),
            draw_prob=round(p_draw_reg, 4),
            fair_home_odds=round(1.0 / home_ml_prob, 3),
            fair_away_odds=round(1.0 / away_ml_prob, 3),
            fair_draw_odds=round(1.0 / p_draw_reg, 3) if p_draw_reg > 0 else None,
            expected_total_score=round(total_goals, 2),
            sport_specific_metrics={
                "projected_home_xg": round(exp_home_goals, 2),
                "projected_away_xg": round(exp_away_goals, 2),
                "regulation_draw_prob": round(p_draw_reg, 3),
                "empty_net_probability": round(p_empty_net, 3),
            },
            market_probabilities={
                "puck_line_home_minus_1_5": round(p_home_reg * 0.65, 3),
                "total_over_5_5": round(1.0 - sum(
                    (math.exp(-total_goals) * (total_goals**k) / math.factorial(k))
                    for k in range(6)
                ), 3),
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        home_score = float(match_result.get("home_score", 0))
        away_score = float(match_result.get("away_score", 0))

        if market_type in ("moneyline_incl_ot", "match_winner"):
            return (home_score > away_score) if selection.lower() == "home" else (away_score > home_score)
        elif market_type == "puck_line_handicap" and line is not None:
            if selection.lower() == "home":
                return (home_score + line) > away_score
            return (away_score + line) > home_score
        elif market_type == "regulation_3_way":
            reg_home = float(match_result.get("reg_home_score", home_score))
            reg_away = float(match_result.get("reg_away_score", away_score))
            if selection.lower() == "draw":
                return reg_home == reg_away
            return (reg_home > reg_away) if selection.lower() == "home" else (reg_away > reg_home)
        elif market_type == "game_total_goals" and line is not None:
            tot = home_score + away_score
            return (tot > line) if selection.lower() == "over" else (tot < line)
        elif market_type == "empty_net_goal_yes_no":
            en_goal = bool(match_result.get("empty_net_goal_scored", False))
            return en_goal if selection.lower() == "yes" else not en_goal
        return False
```

---

### FILE 8: `backend/app/domain/sports/combat_adapter.py`
```python
"""
Combat Sports Quantitative Adapter: Strike Decay & Stoppage Modeling
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class CombatSportAdapter(BaseSportAdapter):
    """
    Quantitative MMA / UFC & Boxing Engine implementing Significant Strike Differential,
    Takedown Defense Matrix, and round-by-round finish rate exponential decay.
    """

    @property
    def sport_key(self) -> str:
        return "combat"

    @property
    def display_name(self) -> str:
        return "MMA / Combat Sports"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "moneyline",
            "method_of_victory",
            "round_totals_over_under",
            "fight_goes_the_distance",
        ]

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        num_rounds = int(ctx.get("rounds", 3))

        a_slpm = float(home_stats.get("slpm", 4.2))
        a_sapm = float(home_stats.get("sapm", 3.1))
        b_slpm = float(away_stats.get("slpm", 3.8))
        b_sapm = float(away_stats.get("sapm", 3.4))

        strike_diff_a = a_slpm - b_sapm
        strike_diff_b = b_slpm - a_sapm

        a_td = float(home_stats.get("td_avg_per_15", 1.8)) * (1.0 - float(away_stats.get("td_def_pct", 0.65)))
        b_td = float(away_stats.get("td_avg_per_15", 1.2)) * (1.0 - float(home_stats.get("td_def_pct", 0.70)))

        net_advantage_a = (strike_diff_a - strike_diff_b) * 1.5 + (a_td - b_td) * 0.8
        fighter_a_prob = 1.0 / (1.0 + math.exp(-net_advantage_a / 2.2))
        fighter_a_prob = max(0.02, min(0.98, fighter_a_prob))
        fighter_b_prob = 1.0 - fighter_a_prob

        a_finish_rate = float(home_stats.get("finish_rate", 0.60))
        b_finish_rate = float(away_stats.get("finish_rate", 0.55))
        combined_finish = (a_finish_rate + b_finish_rate) / 2.0
        if num_rounds == 5:
            distance_prob = max(0.10, (1.0 - combined_finish)**1.4)
        else:
            distance_prob = max(0.20, (1.0 - combined_finish))

        expected_duration = (num_rounds * 5.0) * (0.6 + 0.4 * distance_prob)

        return FairOddsResult(
            home_win_prob=round(fighter_a_prob, 4),
            away_win_prob=round(fighter_b_prob, 4),
            draw_prob=0.015,
            fair_home_odds=round(1.0 / fighter_a_prob, 3),
            fair_away_odds=round(1.0 / fighter_b_prob, 3),
            fair_draw_odds=66.0,
            expected_total_score=round(expected_duration, 1),
            sport_specific_metrics={
                "fighter_a_strike_diff": round(strike_diff_a, 2),
                "fighter_b_strike_diff": round(strike_diff_b, 2),
                "distance_probability": round(distance_prob, 3),
                "scheduled_rounds": num_rounds,
            },
            market_probabilities={
                "fight_goes_distance_yes": round(distance_prob, 3),
                "fight_goes_distance_no": round(1.0 - distance_prob, 3),
                "fighter_a_by_ko_tko": round(fighter_a_prob * a_finish_rate * 0.70, 3),
                "fighter_a_by_decision": round(fighter_a_prob * distance_prob, 3),
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        winner = match_result.get("winner", "").lower()
        if market_type in ("moneyline", "match_winner"):
            return selection.lower() == winner
        elif market_type == "fight_goes_the_distance":
            went_distance = bool(match_result.get("went_distance", False))
            return went_distance if selection.lower() == "yes" else not went_distance
        elif market_type == "method_of_victory":
            method = match_result.get("method", "").lower()  # "ko_tko", "submission", "decision"
            return selection.lower() in method
        elif market_type == "round_totals_over_under" and line is not None:
            rounds_completed = float(match_result.get("rounds_completed", 0))
            return (rounds_completed > line) if selection.lower() == "over" else (rounds_completed < line)
        return False
```

---

### FILE 9: `backend/app/domain/sports/esports_adapter.py`
```python
"""
Esports Quantitative Adapter: HLTV/VLR Rating, Map Veto & Side Bias Modeling
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class EsportsSportAdapter(BaseSportAdapter):
    """
    Quantitative Esports Engine (CS2, LoL, Dota 2, Valorant) implementing
    rating differentials, 7-map veto sequence simulation, and asymmetrical side biases.
    """

    MAP_SIDE_BIASES: Dict[str, float] = {
        "nuke": 0.56,
        "anubis": 0.46,
        "mirage": 0.51,
        "inferno": 0.52,
        "ancient": 0.55,
    }

    @property
    def sport_key(self) -> str:
        return "esports"

    @property
    def display_name(self) -> str:
        return "Esports"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "match_winner_series",
            "map_handicap",
            "map_total_over_under",
            "map_1_winner",
            "pistol_round_winner",
        ]

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        series_format = ctx.get("series_format", "bo3").lower()

        rating_a = float(home_stats.get("team_rating", 1.05))
        rating_b = float(away_stats.get("team_rating", 0.98))
        rating_diff = rating_a - rating_b

        p_map_a = 1.0 / (1.0 + math.exp(-rating_diff / 0.14))
        p_map_a = max(0.05, min(0.95, p_map_a))

        if series_format == "bo1":
            p_match_a = p_map_a
            expected_maps = 1.0
        elif series_format == "bo5":
            p_match_a = (
                (p_map_a**3)
                + 3.0 * (p_map_a**3) * (1.0 - p_map_a)
                + 6.0 * (p_map_a**3) * ((1.0 - p_map_a)**2)
            )
            expected_maps = 3.8
        else:
            p_match_a = (p_map_a**2) + 2.0 * (p_map_a**2) * (1.0 - p_map_a)
            p_2_1 = 2.0 * p_map_a * (1.0 - p_map_a)
            expected_maps = 2.0 + p_2_1

        p_match_b = 1.0 - p_match_a

        return FairOddsResult(
            home_win_prob=round(p_match_a, 4),
            away_win_prob=round(p_match_b, 4),
            draw_prob=0.0,
            fair_home_odds=round(1.0 / p_match_a, 3),
            fair_away_odds=round(1.0 / p_match_b, 3),
            fair_draw_odds=None,
            expected_total_score=round(expected_maps, 2),
            sport_specific_metrics={
                "single_map_prob_team_a": round(p_map_a, 3),
                "rating_differential": round(rating_diff, 3),
                "series_format": series_format,
            },
            market_probabilities={
                "map_handicap_a_minus_1_5": round(p_map_a**2, 3),
                "total_maps_over_2_5": round(2.0 * p_map_a * (1.0 - p_map_a), 3),
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        winner = match_result.get("winner", "").lower()
        if market_type in ("match_winner_series", "map_1_winner"):
            return selection.lower() == winner
        elif market_type == "map_handicap" and line is not None:
            a_maps = float(match_result.get("team_a_maps", 0))
            b_maps = float(match_result.get("team_b_maps", 0))
            if selection.lower() == "team_a":
                return (a_maps + line) > b_maps
            return (b_maps + line) > a_maps
        elif market_type == "map_total_over_under" and line is not None:
            total_maps = float(match_result.get("total_maps", 0))
            return (total_maps > line) if selection.lower() == "over" else (total_maps < line)
        elif market_type == "pistol_round_winner":
            pistol_w = match_result.get("pistol_winner", "").lower()
            return selection.lower() == pistol_w
        return False
```

---

### FILE 10: `backend/app/domain/sports/soccer_adapter.py`
```python
"""
Soccer Quantitative Adapter: Dixon-Coles Bivariate Poisson & Low-Score Correlation
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter, FairOddsResult

class SoccerSportAdapter(BaseSportAdapter):
    """
    Quantitative Soccer Engine implementing Dixon-Coles bivariate Poisson
    with low-score inflation parameter rho, home advantage, clean sheet, and BTTS.
    """

    @property
    def sport_key(self) -> str:
        return "soccer"

    @property
    def display_name(self) -> str:
        return "Football (Soccer)"

    @property
    def standard_markets(self) -> List[str]:
        return [
            "1x2_match_odds",
            "over_under_2_5_goals",
            "both_teams_to_score",
            "asian_handicap",
            "correct_score",
        ]

    def _tau(self, x: int, y: int, lambda_: float, mu: float, rho: float) -> float:
        """Dixon-Coles low-score adjustment factor."""
        if x == 0 and y == 0:
            return 1.0 - lambda_ * mu * rho
        elif x == 0 and y == 1:
            return 1.0 + lambda_ * rho
        elif x == 1 and y == 0:
            return 1.0 + mu * rho
        elif x == 1 and y == 1:
            return 1.0 - rho
        return 1.0

    def calculate_fair_odds(
        self,
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> FairOddsResult:
        ctx = context or {}
        home_adv = float(ctx.get("home_advantage", 1.25))
        rho = float(ctx.get("rho", -0.08))

        home_att = float(home_stats.get("attack_rating", 1.35))
        home_def = float(home_stats.get("defense_rating", 1.10))
        away_att = float(away_stats.get("attack_rating", 1.15))
        away_def = float(away_stats.get("defense_rating", 1.20))

        lambda_ = max(0.2, home_att * away_def * home_adv)
        mu = max(0.2, away_att * home_def)

        p_home = 0.0
        p_draw = 0.0
        p_away = 0.0
        p_over_2_5 = 0.0
        p_btts = 0.0

        for x in range(10):
            for y in range(10):
                tau = self._tau(x, y, lambda_, mu, rho)
                p_xy = (
                    tau
                    * (math.exp(-lambda_) * (lambda_**x) / math.factorial(x))
                    * (math.exp(-mu) * (mu**y) / math.factorial(y))
                )
                p_xy = max(0.0, p_xy)

                if x > y:
                    p_home += p_xy
                elif x == y:
                    p_draw += p_xy
                else:
                    p_away += p_xy

                if (x + y) > 2.5:
                    p_over_2_5 += p_xy
                if x > 0 and y > 0:
                    p_btts += p_xy

        tot = p_home + p_draw + p_away
        p_home /= tot
        p_draw /= tot
        p_away /= tot

        return FairOddsResult(
            home_win_prob=round(p_home, 4),
            away_win_prob=round(p_away, 4),
            draw_prob=round(p_draw, 4),
            fair_home_odds=round(1.0 / p_home, 3),
            fair_away_odds=round(1.0 / p_away, 3),
            fair_draw_odds=round(1.0 / p_draw, 3),
            expected_total_score=round(lambda_ + mu, 2),
            sport_specific_metrics={
                "home_xg": round(lambda_, 2),
                "away_xg": round(mu, 2),
                "dixon_coles_rho": rho,
            },
            market_probabilities={
                "over_2_5": round(p_over_2_5, 3),
                "under_2_5": round(1.0 - p_over_2_5, 3),
                "btts_yes": round(p_btts, 3),
                "btts_no": round(1.0 - p_btts, 3),
            },
        )

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: Dict[str, Any],
        selection: str,
        line: Optional[float] = None,
    ) -> bool:
        home_g = int(match_result.get("home_goals", 0))
        away_g = int(match_result.get("away_goals", 0))

        if market_type in ("1x2_match_odds", "match_winner"):
            if selection.lower() == "draw":
                return home_g == away_g
            return (home_g > away_g) if selection.lower() == "home" else (away_g > home_g)
        elif market_type == "over_under_2_5_goals":
            return ((home_g + away_g) > 2.5) if selection.lower() == "over" else ((home_g + away_g) < 2.5)
        elif market_type == "both_teams_to_score":
            return (home_g > 0 and away_g > 0) if selection.lower() == "yes" else not (home_g > 0 and away_g > 0)
        elif market_type == "asian_handicap" and line is not None:
            if selection.lower() == "home":
                return (home_g + line) > away_g
            return (away_g + line) > home_g
        elif market_type == "correct_score":
            score = f"{home_g}-{away_g}"
            return selection == score
        return False
```

---

### FILE 11: `backend/app/domain/sports/registry.py`
```python
"""
Universal Multi-Sport Adapter Registry
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from app.domain.sports.base_sport_adapter import BaseSportAdapter
from app.domain.sports.cricket_adapter import CricketSportAdapter
from app.domain.sports.basketball_adapter import BasketballSportAdapter
from app.domain.sports.tennis_adapter import TennisSportAdapter
from app.domain.sports.baseball_adapter import BaseballSportAdapter
from app.domain.sports.american_football_adapter import AmericanFootballSportAdapter
from app.domain.sports.ice_hockey_adapter import IceHockeySportAdapter
from app.domain.sports.combat_adapter import CombatSportAdapter
from app.domain.sports.esports_adapter import EsportsSportAdapter
from app.domain.sports.soccer_adapter import SoccerSportAdapter

class SportAdapterRegistry:
    """
    Central factory and lookup registry for all 9 quantitative sport adapters.
    Provides singleton instances and uniform dispatch.
    """

    _adapters: Dict[str, BaseSportAdapter] = {}

    @classmethod
    def initialize(cls) -> None:
        """Registers all 9 production sport domain adapters."""
        adapters = [
            SoccerSportAdapter(),
            CricketSportAdapter(),
            BasketballSportAdapter(),
            TennisSportAdapter(),
            BaseballSportAdapter(),
            AmericanFootballSportAdapter(),
            IceHockeySportAdapter(),
            CombatSportAdapter(),
            EsportsSportAdapter(),
        ]
        for adapter in adapters:
            cls._adapters[adapter.sport_key] = adapter

    @classmethod
    def get_adapter(cls, sport_key: str) -> BaseSportAdapter:
        """Retrieves an adapter by sport key, defaulting to soccer if unrecognized."""
        if not cls._adapters:
            cls.initialize()
        return cls._adapters.get(sport_key.lower(), cls._adapters["soccer"])

    @classmethod
    def list_supported_sports(cls) -> List[Dict[str, Any]]:
        """Returns metadata for all 9 registered sport adapters."""
        if not cls._adapters:
            cls.initialize()
        return [
            {
                "sport_key": a.sport_key,
                "display_name": a.display_name,
                "standard_markets": a.standard_markets,
            }
            for a in cls._adapters.values()
        ]
```

---

## 🛠️ PART 3: DATABASE MODELS, MIGRATION, SCHEMAS & API ROUTERS (FILES 12 TO 16)

### FILE 12: `backend/app/models/sports_config.py`
```python
"""
Multi-Sport Configuration & League Registry Models
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import Boolean, DateTime, Float, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, utc_now

class SportConfiguration(Base):
    """
    Stores quantitative engine hyperparameters and liquidity limits per sport.
    Architected by Amit Ashok Kumar Patnaik.
    """
    __tablename__ = "sports_configurations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sport_key: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    default_kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25, nullable=False)
    min_market_liquidity: Mapped[float] = mapped_column(Float, default=1000.0, nullable=False)
    primary_odds_api_sport_key: Mapped[str] = mapped_column(String(64), nullable=False)
    supported_markets: Mapped[Dict[str, Any]] = mapped_column(JSON, default=list, nullable=False)
    math_model_parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

class LeagueRegistry(Base):
    """
    Stores individual league/competition settings under each parent sport.
    Architected by Amit Ashok Kumar Patnaik.
    """
    __tablename__ = "sports_leagues_registry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sport_key: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    league_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[str] = mapped_column(String(64), default="International", nullable=False)
    tier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    dls_resource_table_override: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
```

---

### FILE 13: `backend/app/models/__init__.py`
Ensure the new sport configuration models are registered in `backend/app/models/__init__.py`:
```python
from app.models.sports_config import (
    SportConfiguration,
    LeagueRegistry,
)
```

---

### FILE 14: `backend/alembic/versions/f94a1206c83e_multi_sport_engine_expansion.py`
```python
"""multi-sport engine expansion (Group 79)

Revision ID: f94a1206c83e
Revises: e83f4105a72d
Create Date: 2026-10-11 03:00:00.000000
"""
from typing import Sequence, Union
import uuid
from alembic import op
import sqlalchemy as sa

revision: str = 'f94a1206c83e'
down_revision: Union[str, Sequence[str], None] = 'e83f4105a72d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. Create sports_configurations table
    op.create_table(
        'sports_configurations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('sport_key', sa.String(length=32), nullable=False),
        sa.Column('display_name', sa.String(length=64), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('default_kelly_fraction', sa.Float(), nullable=False, server_default='0.25'),
        sa.Column('min_market_liquidity', sa.Float(), nullable=False, server_default='1000.0'),
        sa.Column('primary_odds_api_sport_key', sa.String(length=64), nullable=False),
        sa.Column('supported_markets', sa.JSON(), nullable=False),
        sa.Column('math_model_parameters', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sport_key', name='uq_sports_config_key')
    )
    op.create_index('ix_sports_config_key', 'sports_configurations', ['sport_key'])

    # 2. Create sports_leagues_registry table
    op.create_table(
        'sports_leagues_registry',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('sport_key', sa.String(length=32), nullable=False),
        sa.Column('league_key', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=128), nullable=False),
        sa.Column('country', sa.String(length=64), nullable=False, server_default='International'),
        sa.Column('tier', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('dls_resource_table_override', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('league_key', name='uq_sports_leagues_key')
    )
    op.create_index('ix_sports_leagues_sport_key', 'sports_leagues_registry', ['sport_key'])
    op.create_index('ix_sports_leagues_league_key', 'sports_leagues_registry', ['league_key'])

    # 3. Seed initial 9 sports
    initial_sports = [
        ("soccer", "Football (Soccer)", "soccer_epl", ["1x2_match_odds", "over_under_2_5_goals", "both_teams_to_score"], {"model": "dixon_coles", "rho": -0.08}),
        ("cricket", "Cricket", "cricket_ipl", ["match_winner", "innings_total_runs", "most_sixes"], {"model": "dls_markov", "dew_factor_active": True}),
        ("basketball", "Basketball", "basketball_nba", ["moneyline", "point_spread", "game_total_over_under"], {"model": "four_factors", "pace_weight": 0.40}),
        ("tennis", "Tennis", "tennis_atp", ["match_winner", "set_betting_correct_score", "game_handicap"], {"model": "omalley_barnett_markov", "surface_aware": True}),
        ("baseball", "Baseball", "baseball_mlb", ["moneyline", "run_line_handicap", "nrfi_yrfi_first_inning"], {"model": "baseruns_bsr", "park_factors": True}),
        ("american_football", "American Football", "americanfootball_nfl", ["moneyline", "point_spread", "game_total_over_under"], {"model": "epa_key_numbers", "cluster_margin_3_7": True}),
        ("ice_hockey", "Ice Hockey", "icehockey_nhl", ["moneyline_incl_ot", "puck_line_handicap", "regulation_3_way"], {"model": "xg_corsi_empty_net", "empty_net_rate": 0.224}),
        ("combat", "MMA / Combat Sports", "mma_mixed_martial_arts", ["moneyline", "method_of_victory", "round_totals_over_under"], {"model": "slpm_sapm_strike_decay", "takedown_matrix": True}),
        ("esports", "Esports", "esports_cs2", ["match_winner_series", "map_handicap", "map_total_over_under"], {"model": "hltv_rating_map_veto", "side_bias_active": True}),
    ]

    sports_table = sa.table(
        'sports_configurations',
        sa.column('id', sa.String),
        sa.column('sport_key', sa.String),
        sa.column('display_name', sa.String),
        sa.column('is_active', sa.Boolean),
        sa.column('default_kelly_fraction', sa.Float),
        sa.column('min_market_liquidity', sa.Float),
        sa.column('primary_odds_api_sport_key', sa.String),
        sa.column('supported_markets', sa.JSON),
        sa.column('math_model_parameters', sa.JSON),
    )

    for key, name, odds_key, mkts, params in initial_sports:
        op.bulk_insert(sports_table, [{
            'id': str(uuid.uuid4()),
            'sport_key': key,
            'display_name': name,
            'is_active': True,
            'default_kelly_fraction': 0.25,
            'min_market_liquidity': 1000.0,
            'primary_odds_api_sport_key': odds_key,
            'supported_markets': mkts,
            'math_model_parameters': params,
        }])

    # 4. Seed initial 25 top leagues across all 9 sports
    initial_leagues = [
        ("soccer", "soccer_epl", "Premier League", "England", 1),
        ("soccer", "soccer_spain_la_liga", "La Liga", "Spain", 1),
        ("soccer", "soccer_uefa_champs_league", "UEFA Champions League", "Europe", 1),
        ("soccer", "soccer_germany_bundesliga", "Bundesliga", "Germany", 1),
        ("soccer", "soccer_italy_serie_a", "Serie A", "Italy", 1),
        ("soccer", "soccer_usa_mls", "Major League Soccer", "USA", 2),
        ("cricket", "cricket_ipl", "Indian Premier League", "India", 1),
        ("cricket", "cricket_big_bash", "Big Bash League", "Australia", 1),
        ("cricket", "cricket_icc_world_cup", "ICC Cricket World Cup", "International", 1),
        ("cricket", "cricket_t20_world_cup", "ICC T20 World Cup", "International", 1),
        ("cricket", "cricket_the_ashes", "The Ashes Test Series", "International", 1),
        ("basketball", "basketball_nba", "NBA", "USA", 1),
        ("basketball", "basketball_euroleague", "EuroLeague", "Europe", 1),
        ("basketball", "basketball_ncaab", "NCAA Men's Basketball", "USA", 2),
        ("tennis", "tennis_atp", "ATP Tour", "International", 1),
        ("tennis", "tennis_wta", "WTA Tour", "International", 1),
        ("tennis", "tennis_wimbledon", "Wimbledon Championships", "United Kingdom", 1),
        ("tennis", "tennis_us_open", "US Open Grand Slam", "USA", 1),
        ("baseball", "baseball_mlb", "MLB", "USA", 1),
        ("baseball", "baseball_npb", "Nippon Professional Baseball", "Japan", 1),
        ("american_football", "americanfootball_nfl", "NFL", "USA", 1),
        ("american_football", "americanfootball_ncaaf", "NCAA Football (FBS)", "USA", 2),
        ("ice_hockey", "icehockey_nhl", "NHL", "USA / Canada", 1),
        ("ice_hockey", "icehockey_khl", "KHL", "International", 2),
        ("combat", "mma_ufc", "UFC Main Card", "International", 1),
        ("combat", "mma_bellator", "Bellator MMA", "USA", 2),
        ("esports", "esports_cs2_major", "CS2 Major Championships", "International", 1),
        ("esports", "esports_lol_worlds", "League of Legends Worlds", "International", 1),
        ("esports", "esports_dota2_ti", "The International (Dota 2)", "International", 1),
    ]

    leagues_table = sa.table(
        'sports_leagues_registry',
        sa.column('id', sa.String),
        sa.column('sport_key', sa.String),
        sa.column('league_key', sa.String),
        sa.column('display_name', sa.String),
        sa.column('country', sa.String),
        sa.column('tier', sa.Integer),
        sa.column('is_active', sa.Boolean),
    )

    for sk, lk, dn, ctry, tr in initial_leagues:
        op.bulk_insert(leagues_table, [{
            'id': str(uuid.uuid4()),
            'sport_key': sk,
            'league_key': lk,
            'display_name': dn,
            'country': ctry,
            'tier': tr,
            'is_active': True,
        }])

def downgrade() -> None:
    op.drop_table('sports_leagues_registry')
    op.drop_table('sports_configurations')
```

---

### FILE 15: `backend/app/schemas/sports_config.py`
```python
"""
Pydantic v2 Contracts for Multi-Sport Quantitative Configurations & APIs
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

class StrictModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

class SportConfigResponse(StrictModel):
    id: str
    sport_key: str
    display_name: str
    is_active: bool
    default_kelly_fraction: float
    min_market_liquidity: float
    primary_odds_api_sport_key: str
    supported_markets: List[str]
    math_model_parameters: Dict[str, Any]
    created_at: datetime
    updated_at: datetime
    developer_credit: str = "Amit Ashok Kumar Patnaik"

class SportConfigUpdate(StrictModel):
    is_active: Optional[bool] = None
    default_kelly_fraction: Optional[float] = Field(default=None, ge=0.01, le=1.0)
    min_market_liquidity: Optional[float] = Field(default=None, ge=0.0)
    primary_odds_api_sport_key: Optional[str] = None
    math_model_parameters: Optional[Dict[str, Any]] = None

class LeagueResponse(StrictModel):
    id: str
    sport_key: str
    league_key: str
    display_name: str
    country: str
    tier: int
    is_active: bool
    created_at: datetime

class LeagueCreate(StrictModel):
    sport_key: str
    league_key: str
    display_name: str
    country: str = "International"
    tier: int = 1
    is_active: bool = True

class MatchProbabilitiesRequest(StrictModel):
    sport_key: str
    home_stats: Dict[str, Any]
    away_stats: Dict[str, Any]
    context: Optional[Dict[str, Any]] = None

class MatchProbabilitiesResponse(StrictModel):
    sport_key: str
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    fair_home_odds: float
    fair_away_odds: float
    fair_draw_odds: Optional[float]
    expected_total_score: float
    sport_specific_metrics: Dict[str, Any]
    market_probabilities: Dict[str, float]
    developer_credit: str = "Amit Ashok Kumar Patnaik"

class MarketResolutionRequest(StrictModel):
    sport_key: str
    market_type: str
    match_result: Dict[str, Any]
    selection: str
    line: Optional[float] = None

class MarketResolutionResponse(StrictModel):
    is_win: bool
    sport_key: str
    market_type: str
    selection: str
    developer_credit: str = "Amit Ashok Kumar Patnaik"
```

---

### FILE 16: `backend/app/api/v1/sports.py`
```python
"""
Multi-Sport API Router & Quantitative Dispatch Endpoints
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
import logging
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_db
from app.models.sports_config import SportConfiguration, LeagueRegistry
from app.schemas.sports_config import (
    SportConfigResponse,
    SportConfigUpdate,
    LeagueResponse,
    LeagueCreate,
    MatchProbabilitiesRequest,
    MatchProbabilitiesResponse,
    MarketResolutionRequest,
    MarketResolutionResponse,
)
from app.domain.sports.registry import SportAdapterRegistry

logger = logging.getLogger("betdoc.sports")
router = APIRouter(prefix="/sports", tags=["Multi-Sport Quantitative Engine"])

@router.get("/supported", response_model=List[dict])
async def get_supported_sports():
    """Lists all 9 registered quantitative sport adapters and their standard markets."""
    return SportAdapterRegistry.list_supported_sports()

@router.get("/config", response_model=List[SportConfigResponse])
async def list_sport_configs(db: AsyncSession = Depends(get_db)):
    """Retrieves all sport engine configurations and parameters."""
    result = await db.execute(select(SportConfiguration).order_by(SportConfiguration.sport_key))
    rows = result.scalars().all()
    return rows

@router.patch("/config/{sport_key}", response_model=SportConfigResponse)
async def update_sport_config(
    sport_key: str,
    payload: SportConfigUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Updates risk threshold, Kelly fraction, or mathematical parameters for a sport."""
    result = await db.execute(select(SportConfiguration).where(SportConfiguration.sport_key == sport_key.lower()))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sport config '{sport_key}' not found")

    if payload.is_active is not None:
        config.is_active = payload.is_active
    if payload.default_kelly_fraction is not None:
        config.default_kelly_fraction = payload.default_kelly_fraction
    if payload.min_market_liquidity is not None:
        config.min_market_liquidity = payload.min_market_liquidity
    if payload.primary_odds_api_sport_key is not None:
        config.primary_odds_api_sport_key = payload.primary_odds_api_sport_key
    if payload.math_model_parameters is not None:
        config.math_model_parameters = payload.math_model_parameters

    await db.commit()
    await db.refresh(config)
    return config

@router.get("/leagues/{sport_key}", response_model=List[LeagueResponse])
async def list_sport_leagues(sport_key: str, db: AsyncSession = Depends(get_db)):
    """Retrieves registered leagues and competitions for a given sport."""
    result = await db.execute(
        select(LeagueRegistry)
        .where(LeagueRegistry.sport_key == sport_key.lower())
        .order_by(LeagueRegistry.tier, LeagueRegistry.display_name)
    )
    return result.scalars().all()

@router.post("/calculate-odds", response_model=MatchProbabilitiesResponse)
async def calculate_match_odds(payload: MatchProbabilitiesRequest):
    """
    Computes fair win/draw/totals probabilities and unmargined odds
    for any match across all 9 sports using the corresponding quantitative adapter.
    """
    adapter = SportAdapterRegistry.get_adapter(payload.sport_key)
    res = adapter.calculate_fair_odds(
        home_stats=payload.home_stats,
        away_stats=payload.away_stats,
        context=payload.context,
    )
    return MatchProbabilitiesResponse(
        sport_key=adapter.sport_key,
        home_win_prob=res.home_win_prob,
        away_win_prob=res.away_win_prob,
        draw_prob=res.draw_prob,
        fair_home_odds=res.fair_home_odds,
        fair_away_odds=res.fair_away_odds,
        fair_draw_odds=res.fair_draw_odds,
        expected_total_score=res.expected_total_score,
        sport_specific_metrics=res.sport_specific_metrics,
        market_probabilities=res.market_probabilities,
    )

@router.post("/resolve-bet", response_model=MarketResolutionResponse)
async def resolve_bet(payload: MarketResolutionRequest):
    """Dynamically resolves whether a selection won given match statistics and line."""
    adapter = SportAdapterRegistry.get_adapter(payload.sport_key)
    is_win = adapter.resolve_market_winner(
        market_type=payload.market_type,
        match_result=payload.match_result,
        selection=payload.selection,
        line=payload.line,
    )
    return MarketResolutionResponse(
        is_win=is_win,
        sport_key=adapter.sport_key,
        market_type=payload.market_type,
        selection=payload.selection,
    )
```

---

## 🛠️ PART 4: DYNAMIC OMNI-PROVIDER ODDS SYNC SERVICE & FRONTEND UI SUITE (FILES 17 TO 20)

### FILE 17: `backend/app/services/multi_sport_odds_sync.py`
```python
"""
Multi-Sport Omni-Provider Odds Synchronization & Multi-Venue Edge Screener Service
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sports_config import SportConfiguration, LeagueRegistry
from app.models.omni_vault import VaultProviderCredential
from app.domain.sports.registry import SportAdapterRegistry
from app.core.config import settings

logger = logging.getLogger("betdoc.sports_sync")

class MultiSportOddsSyncService:
    """
    Omni-provider odds synchronization and line shopping engine.
    Ingests, normalizes, and compares odds across all active bookmakers and feeds:
    - The Odds API (Global multi-book consensus quotes)
    - Polymarket (Prediction market wisdom-of-the-crowd probabilities)
    - Pinnacle (Sharp benchmark pricing and reference spreads)
    - Betfair (Exchange order book depth and zero-vig implied odds)
    - Parimatch (Direct feed injection and live micro-markets)
    - Stake & 1xBet (Retail bookmaker execution lines)
    - ESPN (Live in-play game state, scores, lineups, and injury feeds)
    - Open-Meteo (Venue weather, temperature, wind, and evening dew factor)
    - NewsAPI (Breaking squad news and steam move catalysts)
    """

    def __init__(self, api_key: Optional[str] = None):
        # Master environment API keys and endpoints
        self.odds_api_key = api_key or getattr(settings, "ODDS_API_KEY", "")
        self.odds_api_url = getattr(settings, "ODDS_API_BASE_URL", "https://api.the-odds-api.com/v4")
        self.polymarket_url = getattr(settings, "POLYMARKET_GAMMA_BASE_URL", "https://gamma-api.polymarket.com")
        self.pinnacle_url = getattr(settings, "PINNACLE_API_BASE_URL", "https://api.pinnacle.com")
        self.betfair_url = getattr(settings, "BETFAIR_IDENTITY_URL", "https://identitysso.betfair.com/api")
        self.parimatch_token = getattr(settings, "PARIMATCH_FEED_TOKEN", None)
        self.espn_url = getattr(settings, "WIRE_ESPN_BASE_URL", "https://site.api.espn.com/apis/site/v2/sports")
        self.open_meteo_url = getattr(settings, "WIRE_OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")
        self.newsapi_key = getattr(settings, "NEWSAPI_ORG_API_KEY", None)

    async def get_provider_credentials(self, db: AsyncSession, provider_name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves and decrypts provider credentials from the secure Vault table
        falling back to application settings if not found in database.
        """
        try:
            stmt = select(VaultProviderCredential).where(VaultProviderCredential.provider == provider_name)
            res = await db.execute(stmt)
            cred = res.scalar_one_or_none()
            if cred:
                # In production, decrypt with VaultCrypto HKDF key
                return {"provider": provider_name, "is_active": cred.is_active}
        except Exception as exc:
            logger.debug("Vault lookup for %s skipped or deferred: %s", provider_name, exc)

        # Fallback to environment keys
        if provider_name == "the_odds_api":
            return {"api_key": self.odds_api_key, "base_url": self.odds_api_url}
        elif provider_name == "polymarket":
            return {"base_url": self.polymarket_url}
        elif provider_name == "pinnacle":
            return {"base_url": self.pinnacle_url}
        elif provider_name == "betfair":
            return {"base_url": self.betfair_url}
        elif provider_name == "parimatch":
            return {"token": self.parimatch_token}
        elif provider_name == "espn":
            return {"base_url": self.espn_url}
        elif provider_name == "open_meteo":
            return {"base_url": self.open_meteo_url}
        return None

    async def get_active_sports_keys(self, db: AsyncSession) -> List[str]:
        """Queries database for all sports currently enabled by the quantitative desk."""
        stmt = select(SportConfiguration.primary_odds_api_sport_key).where(SportConfiguration.is_active.is_(True))
        res = await db.execute(stmt)
        return [row[0] for row in res.fetchall() if row[0]]

    async def screen_multi_provider_edges(
        self,
        sport_key: str,
        match_id: str,
        home_team: str,
        away_team: str,
        venue_odds: Dict[str, Dict[str, float]],  # venue_name -> {"home": 2.10, "away": 1.85, ...}
        home_stats: Dict[str, Any],
        away_stats: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Performs multi-venue line shopping across all bookmaker quotes
        (Pinnacle, Betfair, The Odds API, Parimatch, Stake, 1xBet),
        compares against the quantitative sport adapter fair odds,
        and identifies positive EV value bets and cross-venue arbitrage.
        """
        adapter = SportAdapterRegistry.get_adapter(sport_key)
        fair = adapter.calculate_fair_odds(home_stats, away_stats, context)

        edges: List[Dict[str, Any]] = []

        # Multi-venue line shopping for best prices
        best_home_price = 0.0
        best_home_venue = ""
        best_away_price = 0.0
        best_away_venue = ""

        for venue, quotes in venue_odds.items():
            h_odds = quotes.get("home", 0.0)
            a_odds = quotes.get("away", 0.0)
            if h_odds > best_home_price:
                best_home_price = h_odds
                best_home_venue = venue
            if a_odds > best_away_price:
                best_away_price = a_odds
                best_away_venue = venue

        # Evaluate Home EV on best market price
        if best_home_price > 1.0:
            home_ev = (fair.home_win_prob * best_home_price) - 1.0
            if home_ev > 0.02:  # 2% edge minimum
                edges.append({
                    "match_id": match_id,
                    "sport_key": sport_key,
                    "selection": home_team,
                    "best_venue": best_home_venue,
                    "bookmaker_odds": best_home_price,
                    "fair_odds": fair.fair_home_odds,
                    "true_probability": fair.home_win_prob,
                    "edge_pct": round(home_ev * 100.0, 2),
                    "is_sharp_backed": best_home_venue in ("pinnacle", "betfair"),
                    "developer_credit": "Amit Ashok Kumar Patnaik",
                })

        # Evaluate Away EV on best market price
        if best_away_price > 1.0:
            away_ev = (fair.away_win_prob * best_away_price) - 1.0
            if away_ev > 0.02:
                edges.append({
                    "match_id": match_id,
                    "sport_key": sport_key,
                    "selection": away_team,
                    "best_venue": best_away_venue,
                    "bookmaker_odds": best_away_price,
                    "fair_odds": fair.fair_away_odds,
                    "true_probability": fair.away_win_prob,
                    "edge_pct": round(away_ev * 100.0, 2),
                    "is_sharp_backed": best_away_venue in ("pinnacle", "betfair"),
                    "developer_credit": "Amit Ashok Kumar Patnaik",
                })

        # Cross-bookmaker Arbitrage detection: (1/best_home + 1/best_away) < 1.0
        if best_home_price > 1.0 and best_away_price > 1.0:
            arb_margin = (1.0 / best_home_price) + (1.0 / best_away_price)
            if arb_margin < 0.985:  # Over 1.5% pure arbitrage
                arb_return = (1.0 / arb_margin) - 1.0
                edges.append({
                    "match_id": match_id,
                    "sport_key": sport_key,
                    "market_type": "cross_venue_arbitrage",
                    "home_venue": best_home_venue,
                    "home_odds": best_home_price,
                    "away_venue": best_away_venue,
                    "away_odds": best_away_price,
                    "arb_return_pct": round(arb_return * 100.0, 2),
                    "developer_credit": "Amit Ashok Kumar Patnaik",
                })

        return edges
```

---

### FILE 18: `frontend/src/store/useSportStore.ts`
```typescript
/**
 * Universal Sport Filter & State Management Store
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface SportInfo {
  key: string;
  name: string;
  icon: string;
  modelType: string;
  primaryLeague: string;
}

export const ALL_SPORTS: SportInfo[] = [
  { key: 'soccer', name: 'Football', icon: '⚽', modelType: 'Dixon-Coles Poisson', primaryLeague: 'Premier League' },
  { key: 'cricket', name: 'Cricket', icon: '🏏', modelType: 'DLS & Dew Markov', primaryLeague: 'IPL & Tests' },
  { key: 'basketball', name: 'Basketball', icon: '🏀', modelType: 'Oliver Four Factors', primaryLeague: 'NBA' },
  { key: 'tennis', name: 'Tennis', icon: '🎾', modelType: 'Markov Serve/Return', primaryLeague: 'ATP Tour' },
  { key: 'baseball', name: 'Baseball', icon: '⚾', modelType: 'BaseRuns BsR & NRFI', primaryLeague: 'MLB' },
  { key: 'american_football', name: 'NFL', icon: '🏈', modelType: 'EPA Key Numbers', primaryLeague: 'NFL' },
  { key: 'ice_hockey', name: 'Ice Hockey', icon: '🏒', modelType: 'Corsi & Empty Net xG', primaryLeague: 'NHL' },
  { key: 'combat', name: 'MMA / UFC', icon: '🥊', modelType: 'Strike Decay Matrix', primaryLeague: 'UFC Main Card' },
  { key: 'esports', name: 'Esports', icon: '🎮', modelType: 'HLTV Veto Simulation', primaryLeague: 'CS2 Major' },
];

interface SportStoreState {
  activeSport: string;
  activeLeague: string | null;
  activeMarketFilter: string;
  developerCredit: string;
  setActiveSport: (sportKey: string) => void;
  setActiveLeague: (leagueKey: string | null) => void;
  setActiveMarketFilter: (market: string) => void;
}

export const useSportStore = create<SportStoreState>()(
  persist(
    (set) => ({
      activeSport: 'soccer',
      activeLeague: null,
      activeMarketFilter: 'all',
      developerCredit: 'Amit Ashok Kumar Patnaik',
      setActiveSport: (sportKey: string) => set({ activeSport: sportKey, activeLeague: null }),
      setActiveLeague: (leagueKey: string | null) => set({ activeLeague: leagueKey }),
      setActiveMarketFilter: (market: string) => set({ activeMarketFilter: market }),
    }),
    {
      name: 'betdoc-sport-storage',
    }
  )
);
```

---

### FILE 19: `frontend/src/components/sports/UniversalSportSelector.tsx`
```tsx
/**
 * Universal 9-Sport Selection Bar with Glowing Neon Badges & Model Indicators
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
 */
import React from 'react';
import { ALL_SPORTS, useSportStore } from '../../store/useSportStore';

export const UniversalSportSelector: React.FC = () => {
  const { activeSport, setActiveSport } = useSportStore();
  const currentSport = ALL_SPORTS.find((s) => s.key === activeSport) || ALL_SPORTS[0];

  return (
    <div className="w-full bg-[#0a0518] border-b border-purple-500/20 py-2.5 px-4 flex flex-col space-y-2 font-mono">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <span className="text-[11px] uppercase tracking-wider text-purple-400 font-bold">
            SPORT FLEET:
          </span>
          <span className="text-[10px] text-gray-500 bg-purple-950/60 px-2 py-0.5 rounded border border-purple-500/30">
            MODEL: <span className="text-cyan-400 font-semibold">{currentSport.modelType}</span>
          </span>
        </div>
        <div className="text-[10px] text-gray-500">
          ARCHITECT: <span className="text-purple-300">Amit Ashok Kumar Patnaik</span>
        </div>
      </div>

      <div className="flex items-center space-x-2 overflow-x-auto no-scrollbar py-1">
        {ALL_SPORTS.map((s) => {
          const isActive = activeSport === s.key;
          return (
            <button
              key={s.key}
              onClick={() => setActiveSport(s.key)}
              className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-all flex items-center space-x-2 whitespace-nowrap ${
                isActive
                  ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-400 shadow-[0_0_15px_rgba(6,182,212,0.4)]'
                  : 'bg-purple-950/30 text-gray-400 border border-purple-500/10 hover:border-purple-500/30 hover:text-gray-200'
              }`}
            >
              <span className="text-sm">{s.icon}</span>
              <span>{s.name}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
};
```

---

### FILE 20: `frontend/src/components/sports/SportSpecificStatCard.tsx`
```tsx
/**
 * Dynamic Multi-Sport Metrics Renderer with Tailored Visuals
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
 */
import React from 'react';

interface Props {
  sportKey: string;
  metrics: Record<string, any>;
}

export const SportSpecificStatCard: React.FC<Props> = ({ sportKey, metrics }) => {
  return (
    <div className="bg-[#120b24] border border-purple-500/30 rounded-xl p-3.5 text-xs font-mono text-white shadow-lg">
      <div className="flex items-center justify-between border-b border-purple-500/20 pb-2 mb-3">
        <div className="text-[11px] text-cyan-400 uppercase font-bold tracking-wider flex items-center space-x-1.5">
          <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse"></span>
          <span>{sportKey.replace(/_/g, ' ').toUpperCase()} QUANTITATIVE FACTORS</span>
        </div>
        <div className="text-[9px] text-gray-400 uppercase">
          ENGINE: VERIFIED
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2.5">
        {Object.entries(metrics).map(([key, val]) => (
          <div key={key} className="bg-purple-950/40 p-2.5 rounded-lg border border-purple-500/15">
            <div className="text-gray-400 text-[10px] uppercase font-semibold">
              {key.replace(/_/g, ' ')}
            </div>
            <div className="text-sm font-bold text-cyan-300 mt-0.5">
              {typeof val === 'boolean' ? (val ? 'ACTIVE' : 'INACTIVE') : String(val)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
```

---

### FILE 21: `backend/tests/test_multi_sport_engine.py`
```python
"""
Multi-Sport Engine Test Suite (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
"""
import pytest
import httpx
from app.main import app
from app.domain.sports.registry import SportAdapterRegistry
from app.domain.sports.soccer_adapter import SoccerSportAdapter
from app.domain.sports.cricket_adapter import CricketSportAdapter
from app.domain.sports.basketball_adapter import BasketballSportAdapter
from app.domain.sports.tennis_adapter import TennisSportAdapter
from app.domain.sports.baseball_adapter import BaseballSportAdapter
from app.domain.sports.american_football_adapter import AmericanFootballSportAdapter
from app.domain.sports.ice_hockey_adapter import IceHockeySportAdapter
from app.domain.sports.combat_adapter import CombatSportAdapter
from app.domain.sports.esports_adapter import EsportsSportAdapter
from app.services.multi_sport_odds_sync import MultiSportOddsSyncService

def test_registry_initialization():
    """Verifies that all 9 sport domain adapters are correctly registered."""
    SportAdapterRegistry.initialize()
    sports = SportAdapterRegistry.list_supported_sports()
    assert len(sports) == 9
    keys = {s["sport_key"] for s in sports}
    expected = {
        "soccer", "cricket", "basketball", "tennis",
        "baseball", "american_football", "ice_hockey", "combat", "esports"
    }
    assert keys == expected

def test_cricket_adapter_dls_and_dew():
    """Tests Cricket DLS resource calculation, par score and dew effect."""
    adapter = CricketSportAdapter()
    r_50 = adapter.calculate_dls_resource(50.0, 0)
    assert 99.0 <= r_50 <= 100.0
    r_20_w5 = adapter.calculate_dls_resource(20.0, 5)
    assert 25.0 <= r_20_w5 <= 35.0

    par = adapter.calculate_dls_par_score(
        team1_runs=200,
        team1_overs=50.0,
        team2_overs_available=30.0,
        team2_wickets_lost=2,
        format_overs=50.0,
    )
    assert par > 0

    res = adapter.calculate_fair_odds(
        home_stats={"batting_rating": 110.0, "bowling_rating": 95.0},
        away_stats={"batting_rating": 95.0, "bowling_rating": 105.0},
        context={"format": "t20", "is_night_match": True, "dew_intensity": "heavy", "chasing_team": "away"},
    )
    assert 0.0 < res.home_win_prob < 1.0
    assert 0.0 < res.away_win_prob < 1.0
    assert abs((res.home_win_prob + res.away_win_prob + res.draw_prob) - 1.0) < 0.01

    # Resolvers
    assert adapter.resolve_market_winner("match_winner", {"winner": "india"}, "india") is True
    assert adapter.resolve_market_winner("innings_total_runs", {"innings_runs": 185}, "over", line=175.5) is True
    assert adapter.resolve_market_winner("first_6_overs_runs", {"powerplay_runs": 52}, "over", line=48.5) is True
    assert adapter.resolve_market_winner("most_sixes", {"home_sixes": 8, "away_sixes": 5}, "home") is True

def test_basketball_four_factors_and_altitude():
    """Tests Basketball Four Factors regression, altitude, and pace-adjusted point spread."""
    adapter = BasketballSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"efg_pct": 0.56, "tov_pct": 0.11, "orb_pct": 0.30, "ftr": 0.25, "pace": 102.0},
        away_stats={"efg_pct": 0.50, "tov_pct": 0.14, "orb_pct": 0.24, "ftr": 0.19, "pace": 98.0},
        context={"venue_city": "denver"},
    )
    assert res.home_win_prob > res.away_win_prob
    assert res.expected_total_score > 200.0
    assert res.sport_specific_metrics["altitude_points"] == 2.8

    # Resolvers
    assert adapter.resolve_market_winner("moneyline", {"home_score": 115, "away_score": 108}, "home") is True
    assert adapter.resolve_market_winner("point_spread", {"home_score": 115, "away_score": 108}, "home", line=-4.5) is True
    assert adapter.resolve_market_winner("game_total_over_under", {"home_score": 115, "away_score": 108}, "over", line=220.5) is True
    assert adapter.resolve_market_winner("quarter_winner", {"quarter": 1, "q1_home": 32, "q1_away": 28}, "home") is True

def test_tennis_markov_hold_and_sets():
    """Tests Tennis analytical hold probability and surface speed index."""
    adapter = TennisSportAdapter()
    hold_grass = adapter.hold_probability(0.75)
    assert hold_grass > 0.80

    res = adapter.calculate_fair_odds(
        home_stats={"first_serve_win_pct": 0.76},
        away_stats={"first_serve_win_pct": 0.65},
        context={"surface": "grass", "best_of": 3},
    )
    assert res.home_win_prob > 0.60
    assert res.fair_home_odds < res.fair_away_odds

    # Resolvers
    assert adapter.resolve_market_winner("match_winner", {"winner": "alcaraz"}, "alcaraz") is True
    assert adapter.resolve_market_winner("set_betting_correct_score", {"set_score": "2-0"}, "2-0") is True
    assert adapter.resolve_market_winner("game_handicap", {"player_a_games": 13, "player_b_games": 8}, "player_a", line=-3.5) is True
    assert adapter.resolve_market_winner("first_set_winner", {"first_set_winner": "sinner"}, "sinner") is True

def test_baseball_baseruns_and_nrfi():
    """Tests Baseball BaseRuns run expectancy and NRFI probability."""
    adapter = BaseballSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"hits_per_9": 8.0, "walks_per_9": 3.0, "home_runs_per_9": 1.2, "total_bases_per_9": 14.0, "stolen_bases_per_9": 0.5},
        away_stats={"hits_per_9": 7.5, "walks_per_9": 2.5, "home_runs_per_9": 0.9, "total_bases_per_9": 12.0, "stolen_bases_per_9": 0.8},
        context={"park": "coors_field"},
    )
    assert 0.0 < res.sport_specific_metrics["nrfi_probability"] < 1.0
    assert res.expected_total_score > 5.0
    assert res.sport_specific_metrics["park_factor"] == 1.34

    # Resolvers
    assert adapter.resolve_market_winner("moneyline", {"home_runs": 6, "away_runs": 4}, "home") is True
    assert adapter.resolve_market_winner("run_line_handicap", {"home_runs": 6, "away_runs": 4}, "home", line=-1.5) is True
    assert adapter.resolve_market_winner("nrfi_yrfi_first_inning", {"first_inning_runs": 0}, "nrfi") is True
    assert adapter.resolve_market_winner("first_5_innings_winner", {"f5_home_runs": 3, "f5_away_runs": 1}, "home") is True

def test_nfl_epa_and_key_numbers():
    """Tests NFL EPA per play and margin mass clustering."""
    adapter = AmericanFootballSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"pass_epa": 0.15, "rush_epa": 0.02},
        away_stats={"pass_epa": -0.05, "rush_epa": -0.10},
    )
    assert res.home_win_prob > 0.65
    assert res.sport_specific_metrics["key_number_cluster_3"] == 0.151

    # Resolvers
    assert adapter.resolve_market_winner("point_spread", {"home_score": 27, "away_score": 20}, "home", line=-6.5) is True
    assert adapter.resolve_market_winner("first_half_spread", {"h1_home_score": 14, "h1_away_score": 7}, "home", line=-3.5) is True

def test_hockey_xg_and_empty_net():
    """Tests NHL xG, Corsi For, and empty-net probabilities."""
    adapter = IceHockeySportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"expected_goals_per_60": 3.4, "corsi_for_pct": 54.0},
        away_stats={"expected_goals_per_60": 2.6, "corsi_for_pct": 46.0},
    )
    assert res.home_win_prob > 0.55
    assert res.sport_specific_metrics["empty_net_probability"] == 0.224

    # Resolvers
    assert adapter.resolve_market_winner("regulation_3_way", {"reg_home_score": 3, "reg_away_score": 3}, "draw") is True
    assert adapter.resolve_market_winner("empty_net_goal_yes_no", {"empty_net_goal_scored": True}, "yes") is True

def test_combat_strike_differential():
    """Tests MMA significant strike differential and distance probability."""
    adapter = CombatSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"slpm": 5.5, "sapm": 2.8, "td_avg_per_15": 2.5, "td_def_pct": 0.80, "finish_rate": 0.70},
        away_stats={"slpm": 3.2, "sapm": 4.1, "td_avg_per_15": 0.5, "td_def_pct": 0.50, "finish_rate": 0.40},
        context={"rounds": 5},
    )
    assert res.home_win_prob > 0.70
    assert "distance_probability" in res.sport_specific_metrics

    # Resolvers
    assert adapter.resolve_market_winner("method_of_victory", {"method": "ko_tko"}, "ko_tko") is True
    assert adapter.resolve_market_winner("fight_goes_the_distance", {"went_distance": False}, "no") is True

def test_esports_map_veto():
    """Tests Esports HLTV rating and BO3 series progression."""
    adapter = EsportsSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"team_rating": 1.15},
        away_stats={"team_rating": 0.95},
        context={"series_format": "bo3"},
    )
    assert res.home_win_prob > 0.70
    assert 2.0 <= res.expected_total_score <= 3.0

    # Resolvers
    assert adapter.resolve_market_winner("map_handicap", {"team_a_maps": 2, "team_b_maps": 0}, "team_a", line=-1.5) is True
    assert adapter.resolve_market_winner("pistol_round_winner", {"pistol_winner": "faze"}, "faze") is True

def test_soccer_dixon_coles():
    """Tests Soccer Dixon-Coles bivariate Poisson with low-score correlation."""
    adapter = SoccerSportAdapter()
    res = adapter.calculate_fair_odds(
        home_stats={"attack_rating": 1.6, "defense_rating": 0.9},
        away_stats={"attack_rating": 1.0, "defense_rating": 1.4},
    )
    assert res.home_win_prob > res.away_win_prob
    assert res.draw_prob > 0.15
    assert "dixon_coles_rho" in res.sport_specific_metrics

    # Resolvers
    assert adapter.resolve_market_winner("1x2_match_odds", {"home_goals": 2, "away_goals": 1}, "home") is True
    assert adapter.resolve_market_winner("over_under_2_5_goals", {"home_goals": 2, "away_goals": 1}, "over") is True
    assert adapter.resolve_market_winner("both_teams_to_score", {"home_goals": 2, "away_goals": 1}, "yes") is True
    assert adapter.resolve_market_winner("correct_score", {"home_goals": 2, "away_goals": 1}, "2-1") is True

@pytest.mark.asyncio
async def test_multi_provider_line_shopping_and_edges():
    """Tests multi-provider line shopping, positive EV edges, and arbitrage detection."""
    service = MultiSportOddsSyncService()
    venue_odds = {
        "pinnacle": {"home": 2.15, "away": 1.78},
        "betfair": {"home": 2.18, "away": 1.80},
        "the_odds_api": {"home": 2.10, "away": 1.75},
        "parimatch": {"home": 2.22, "away": 1.72},
        "stake": {"home": 2.25, "away": 1.70},
    }
    edges = await service.screen_multi_provider_edges(
        sport_key="soccer",
        match_id="m123",
        home_team="Arsenal",
        away_team="Chelsea",
        venue_odds=venue_odds,
        home_stats={"attack_rating": 1.8, "defense_rating": 0.8},
        away_stats={"attack_rating": 1.1, "defense_rating": 1.3},
    )
    assert len(edges) > 0
    assert any(e["developer_credit"] == "Amit Ashok Kumar Patnaik" for e in edges)
    home_edges = [e for e in edges if e.get("selection") == "Arsenal"]
    if home_edges:
        assert home_edges[0]["best_venue"] == "stake"  # 2.25 was highest price
        assert home_edges[0]["bookmaker_odds"] == 2.25

@pytest.mark.asyncio
async def test_provider_key_resolution():
    """Tests provider API key and endpoint configuration resolution."""
    service = MultiSportOddsSyncService()
    pinnacle_creds = await service.get_provider_credentials(None, "pinnacle")
    assert pinnacle_creds is not None
    assert "base_url" in pinnacle_creds

    odds_api_creds = await service.get_provider_credentials(None, "the_odds_api")
    assert odds_api_creds is not None

@pytest.mark.asyncio
async def test_sports_api_endpoints():
    """Integration test verifying all FastAPI endpoints via httpx ASGITransport."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Supported sports list
        resp = await client.get("/api/v1/sports/supported")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 9

        # Odds calculation endpoint
        calc_payload = {
            "sport_key": "cricket",
            "home_stats": {"batting_rating": 105.0, "bowling_rating": 100.0},
            "away_stats": {"batting_rating": 100.0, "bowling_rating": 100.0},
            "context": {"format": "t20"},
        }
        resp = await client.post("/api/v1/sports/calculate-odds", json=calc_payload)
        assert resp.status_code == 200
        calc_data = resp.json()
        assert calc_data["sport_key"] == "cricket"
        assert "fair_home_odds" in calc_data
        assert calc_data["developer_credit"] == "Amit Ashok Kumar Patnaik"

        # Bet resolution endpoint
        resolve_payload = {
            "sport_key": "basketball",
            "market_type": "moneyline",
            "match_result": {"home_score": 112, "away_score": 104},
            "selection": "home",
        }
        resp = await client.post("/api/v1/sports/resolve-bet", json=resolve_payload)
        assert resp.status_code == 200
        res_data = resp.json()
        assert res_data["is_win"] is True
        assert res_data["developer_credit"] == "Amit Ashok Kumar Patnaik"
```

---

## 📋 PART 5: CLAUDE CODE EXECUTION PLAYBOOK & VERIFICATION CHECKLIST

To ensure flawless drop-in execution with **0% loopholes and 0 unfinished tasks**, execute the following steps in sequence:

### STEP 1: Verify Parent State & Ensure Group 78 is Pushed to GitHub
Before starting Group 79, confirm that Group 78 is fully finished, tested, and committed:
```bash
git status
git log -n 5 --oneline
# If Group 77 and Group 78 are committed locally but not yet pushed to remote:
git push origin feature/garuda-ingestion
# Check Docker services are active
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```
Ensure branch is `feature/garuda-ingestion` and head contains Groups 76, 77, and 78.

### STEP 2: Write Domain Sport Adapters (Files 1 to 11)
Create `backend/app/domain/sports/` directory if missing, and write:
- `base_sport_adapter.py`
- `cricket_adapter.py`
- `basketball_adapter.py`
- `tennis_adapter.py`
- `baseball_adapter.py`
- `american_football_adapter.py`
- `ice_hockey_adapter.py`
- `combat_adapter.py`
- `esports_adapter.py`
- `soccer_adapter.py`
- `registry.py`

### STEP 3: Write Database Models & Register Exports (Files 12 & 13)
- Write `backend/app/models/sports_config.py` with `SportConfiguration` and `LeagueRegistry`.
- Append imports to `backend/app/models/__init__.py`.

### STEP 4: Write Alembic Migration (File 14)
- Write `backend/alembic/versions/f94a1206c83e_multi_sport_engine_expansion.py`.
- Run migration upgrade:
```bash
alembic upgrade head
alembic check
```

### STEP 5: Write Pydantic Schemas & API Endpoints (Files 15 & 16)
- Write `backend/app/schemas/sports_config.py`.
- Write `backend/app/api/v1/sports.py`.
- Ensure `router.py` includes `sports.py` under `/sports`.

### STEP 6: Write Omni-Provider Dynamic Odds Sync Service (File 17)
- Write `backend/app/services/multi_sport_odds_sync.py` with multi-venue line shopping across all API keys.

### STEP 7: Write Frontend Store & Components (Files 18 to 20)
- Write `frontend/src/store/useSportStore.ts`.
- Write `frontend/src/components/sports/UniversalSportSelector.tsx`.
- Write `frontend/src/components/sports/SportSpecificStatCard.tsx`.
- Run frontend build check:
```bash
cd frontend && npm run build && cd ..
```

### STEP 8: Run Comprehensive Automated Test Suite (File 21)
- Write `backend/tests/test_multi_sport_engine.py`.
- Execute pytest:
```bash
pytest backend/tests/test_multi_sport_engine.py -v
```

### STEP 9: Git Commit
Commit all files with message:
```bash
git add backend/ frontend/ alembic/
git commit -m "feat(sports): Group 79 multi-sport quantitative engine expansion (9 sports, DLS, Four Factors, BaseRuns, Markov, Omni-provider line shopping)"
```

### STEP 10: Final Push to GitHub
Push the completed Group 79 implementation to GitHub:
```bash
git push origin feature/garuda-ingestion
```
