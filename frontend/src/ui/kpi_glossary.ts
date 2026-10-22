/**
 * Plain-English definitions of the figures BetDoc shows most (Group 99 micro-guides). Developed for Amit Ashok Kumar Patnaik.
 *
 * Every example is worked arithmetic on made-up inputs, never a claim about BetDoc's own results or a bookmaker's
 * actual margin; each one checks out with the formula in its definition.
 */

export interface KpiDefinition {
  code: string;
  name: string;
  shortDesc: string;
  plainEnglish: string;
  example: string;
}

export const KPI_GLOSSARY: Record<string, KpiDefinition> = {
  EV: {
    code: "EV",
    name: "Expected Value (EV %)",
    shortDesc: "How much a price beats the fair chance",
    plainEnglish:
      "The average profit (or loss) per rupee staked if the same bet could be repeated many times: fair probability × odds − 1. Positive EV means the price pays more than the true chance deserves; it is an average, not a promise for any one bet.",
    example: "Fair chance 50% (fair odds 2.00), book offers 2.20: 0.50 × 2.20 − 1 = +10% EV, about ₹100 per ₹1,000 on average.",
  },
  OVERROUND: {
    code: "OVERROUND",
    name: "Bookmaker Overround (margin, vig, juice)",
    shortDesc: "The bookmaker's built-in margin",
    plainEnglish:
      "Add up 1 ÷ odds for every outcome of a market. A fair book adds up to exactly 100%; anything above is the bookmaker's margin, paid by the bettor. BetDoc removes it (de-vigs) before comparing prices with its models.",
    example: "Both sides at 1.90: 1/1.90 + 1/1.90 = 105.26%, a 5.26% overround.",
  },
  SLIP_SCORE: {
    code: "SLIP_SCORE",
    name: "BetDoc Slip Score (0–100)",
    shortDesc: "The 15-pillar fortress score plus badges, in four tiers",
    plainEnglish:
      "The Digital Twin runs every slip through 15 pillars: model consensus, weather, travel, injuries, lineups, market moves, the sharp price, crowd traps, referee, motivation, liquidity, independence of the legs, sizing, the execution gate and the Never-Forget shield. A pass counts 1, an advisory ½, missing evidence ¼ (never a pass), a fail 0; the share of 15 is the fortress score, and one failed pillar holds the slip at 54 at most. Badges add or take points for what the check measured: Vig Crusher +5, Zero-Correlation Shield +8, Score-Synced +10, Positive EV King +10, Same-Match Tax −18, Heavy Juice Trap −12. Tiers: Diamond Fortress 90+, Gold Grade 75+, Silver Caution 55+, Bronze / Danger Trap below.",
    example: "12 pillars pass and 3 lack evidence: (12 + 3 × 0.25) ÷ 15 = 85. Two legs on different fixtures (+8) at a 3.5% margin (+5): 98, Diamond Fortress. Put the second leg on the same match instead: 85 − 18 + 5 = 72, Silver Caution.",
  },
  KELLY_STAKE: {
    code: "KELLY_STAKE",
    name: "Quarter-Kelly stake",
    shortDesc: "How much of the bankroll an edge justifies",
    plainEnglish:
      "The Kelly formula sizes a stake from the edge and the odds: edge ÷ (odds − 1) of the bankroll. BetDoc stakes a quarter of that by default, so a wrong probability or a losing run cannot drain the bankroll, then adjusts it for the models' track record and long shots.",
    example: "₹10,000 bankroll, +6% EV at odds 2.10: 0.06 ÷ 1.10 = 5.45% full Kelly; a quarter is 1.36%, about ₹136.",
  },
  BRIER_SCORE: {
    code: "BRIER_SCORE",
    name: "Brier score (0 to 1, lower is better)",
    shortDesc: "How well-calibrated the probabilities were",
    plainEnglish:
      "For each settled prediction, (forecast probability − what happened)², where what happened is 1 or 0, averaged over many predictions. 0 is perfect; always saying 50% scores 0.25, so a useful model must stay well under that.",
    example: "Forecast 58% and the selection won: (1 − 0.58)² = 0.176.",
  },
  CLV: {
    code: "CLV",
    name: "Closing Line Value (CLV %)",
    shortDesc: "Did you beat the final price?",
    plainEnglish:
      "Compares the odds you took with the odds just before kick-off: your odds ÷ closing odds − 1. Beating the closing line consistently is the strongest long-run sign that bets are well priced, whatever single results do.",
    example: "Took 2.20, closed at 1.95: 2.20 ÷ 1.95 − 1 = +12.8% CLV.",
  },
};
