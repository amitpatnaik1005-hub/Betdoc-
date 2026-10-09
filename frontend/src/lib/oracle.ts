/** ASHOKA, the Oracle (backend `app/api/v1/oracle.py`, Group 69): vetted slips, odds shopping, cashout advice, the user's P&L. */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export type SlipKind = "SINGLE" | "DOUBLE" | "TREBLE" | "ACCUMULATOR" | "TRIXIE" | "YANKEE" | "CANADIAN" | "HEINZ";
export const SYSTEM_KINDS: readonly SlipKind[] = ["TRIXIE", "YANKEE", "CANADIAN", "HEINZ"];

export interface SlipLeg {
  leg_id: string;
  fixture_id: string;
  fixture: string;
  home: string;
  away: string;
  league: string | null;
  sport_key: string | null;
  kickoff: string | null;
  market: string;
  selection: string;
  label: string;
  fair_probability: number;
  models: Record<string, number>;
  ev: number | null;
  prices: Record<string, number>;
  price_age_seconds: Record<string, number>;
  rationale: string;
}

export interface BookLegLine {
  fixture: string;
  market: string;
  search_code: string;
  bookmaker: string;
  odds: string | null;
  fair_probability: number | null;
}

export interface BookView {
  bookmaker: string;
  label: string;
  available: boolean;
  odds: string | null;
  payout_inr: string | null;
  legs: BookLegLine[];
  missing: string[];
  note: string | null;
}

export interface Comparison {
  best: string | null;
  best_odds: string | null;
  parimatch_odds: string | null;
  onexbet_odds: string | null;
  difference_inr: string | null;
  difference_pct: string | null;
  recommended: string | null;
  recommendation: string | null;
}

export interface Simulation {
  paths: number;
  joint_probability: number;
  joint_probability_se: number;
  full_win_probability: number;
  probability_band: [number, number];
  joint_ev: number;
  joint_ev_se: number;
  analytic_ev: number | null;
  std_return: number;
  p_total_loss: number;
  var_95: number;
  cvar_95: number;
  kelly_fraction: number;
}

export interface Slip {
  slip_id: string;
  kind: SlipKind;
  title: string;
  tier: "VETTED" | "VALUE";
  badge: string;
  legs: SlipLeg[];
  book: string;
  odds: number;
  cross_league: boolean;
  leagues: string[];
  stake_fraction: number;
  odds_age_seconds: number;
  simulation: Simulation;
  verdict: { tier: string; checks: Record<string, boolean>; reasons: string[] };
  stake_inr: string;
  reference_stake_inr: string;
  expected_profit_inr: string;
  books: BookView[];
  comparison: Comparison | null;
  quick_copy: string;
  search: string;
  multiple: boolean;
  rechecked_at?: string;
}

export interface SlipsPayload {
  generated_at: string;
  age_seconds: number;
  read: { fixtures: number; markets: number; books: number; legs: number };
  scanned: number;
  simulated: number;
  legs_considered: number;
  rejected: Record<string, number>;
  thresholds: { min_joint_ev: number; min_joint_probability: number; max_quote_age_seconds: number; paths: number };
  slips: Slip[];
  bankroll_inr: string | null;
}

export interface PeriodStats {
  pnl_inr: string;
  staked_inr: string;
  returned_inr: string;
  bets: number;
  won: number;
  lost: number;
  void: number;
  win_rate: number | null;
  roi: number | null;
  tax_inr?: string;
  net_after_tax_inr?: string;
}

export interface Scorecard {
  generated_at: string;
  timezone: string;
  periods: { today: PeriodStats; week: PeriodStats; month: PeriodStats; all_time: PeriodStats };
  pending: { bets: number; staked_inr: string };
  streak: { kind: "W" | "L" | null; count: number; label: string };
  tax_rate: number | null;
  cached: boolean;
}

export type LegResult = "PENDING" | "WON" | "HALF_WON" | "VOID" | "HALF_LOST" | "LOST";
export type BetStatus = LegResult | "CASHED_OUT";

export interface PlacedLeg {
  id: string;
  position: number;
  fixture_id: string;
  home: string;
  away: string;
  league: string | null;
  kickoff: string | null;
  market: string;
  selection: string;
  odds: string;
  fair_probability: number | null;
  result: LegResult;
  score: string | null;
  match_status: "UPCOMING" | "LIVE" | "AWAITING_RESULT" | "FT" | "UNKNOWN";
}

export interface PlacedBet {
  id: string;
  slip_id: string | null;
  source: string;
  bookmaker: string;
  bookmaker_name: string | null;
  structure: SlipKind;
  stake_inr: string;
  unit_stake_inr: string | null;
  placed_odds: string | null;
  placed_at: string | null;
  status: BetStatus;
  return_inr: string | null;
  pnl_inr: string | null;
  settled_at: string | null;
  notes: string | null;
  legs: PlacedLeg[];
}

export interface CashoutAdvice {
  bet_id: string;
  advice: "HOLD" | "CASH_OUT" | "HEDGE_LEG";
  fair_value_inr: string;
  potential_payout_inr: string;
  win_probability: number;
  offer_inr: string | null;
  offer_ratio: number | null;
  implied_margin: number | null;
  certainty_equivalent_inr: string | null;
  hedge: { kind: string; book: string; stakes: { outcome: string; stake_inr: string }[]; total_outlay_inr: string; locked_profit_inr: string; instruction: string } | null;
  reasons: string[];
  open_legs: number;
  settled_legs: number;
}

export interface TrendPick {
  id: string;
  title: string;
  pick_type: string;
  legs: { match_id: string; selection: string; odds: number; market?: string | null; label?: string | null; fixture?: string | null; fair_probability?: number | null; book?: string | null }[];
  total_odds: number;
  historical_success_rate: number;
  category: "SHARP_STEAM" | "PUBLIC_TRAP" | "AI_HYBRID" | null;
  true_ev_pct: number | null;
  true_probability: number | null;
  public_share_pct: number | null;
  public_share_source: string | null;
  warning: string | null;
  expires_at: string;
}

export interface TwinRow {
  axis: string;
  segment: string;
  bets: number;
  roi: number;
  pnl_inr: string;
  win_rate: number | null;
}

export interface Twin {
  settled_bets: number;
  segments: Record<string, Record<string, PeriodStats>>;
  strengths: TwinRow[];
  leaks: TwinRow[];
  average_odds: number | null;
  average_stake_inr: string | null;
  min_bets: number;
}

export const BOOK_LABEL: Record<string, string> = { parimatch: "Parimatch", "1xbet": "1xBet", stake: "Stake", pinnacle: "Pinnacle", betfair: "Betfair" };
export const PLACED_BOOKMAKERS = [
  { value: "PARIMATCH", label: "Parimatch", book: "parimatch" },
  { value: "1XBET", label: "1xBet", book: "1xbet" },
  { value: "STAKE", label: "Stake", book: "stake" },
  { value: "PINNACLE", label: "Pinnacle", book: "pinnacle" },
  { value: "BETFAIR", label: "Betfair", book: "betfair" },
  { value: "OTHER", label: "Other", book: "" },
] as const;

export const useSlips = (bankroll: number | null) =>
  useResource(`oracle:slips:${bankroll ?? "auto"}`, () => apiClient.get<SlipsPayload>("/oracle/slips", bankroll ? { bankroll_inr: bankroll } : undefined), { intervalMs: 30_000 });
export const useScorecard = (tax: boolean) => useResource(`oracle:pnl:${tax}`, () => apiClient.get<Scorecard>("/oracle/pnl", tax ? { tax: true } : undefined), { intervalMs: 60_000 });
export const useBets = (which: "active" | "settled") => useResource(`oracle:bets:${which}`, () => apiClient.get<PlacedBet[]>("/oracle/bets", { which }), { intervalMs: 30_000 });
export const useTrending = () => useResource("oracle:trending", () => apiClient.get<TrendPick[]>("/oracle/trending"), { intervalMs: 120_000 });
export const useTwin = () => useResource("oracle:twin", () => apiClient.get<Twin>("/oracle/twin"), { intervalMs: 300_000 });

/** ₹ from a backend decimal string, with sign when asked. */
export const rupees = (value: string | number | null | undefined, signed = false): string => {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  const body = Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 });
  const sign = n < 0 ? "−" : signed && n > 0 ? "+" : "";
  return `${sign}₹${body}`;
};

/** What "I placed this bet" sends for a slip, at the chosen book's prices. */
export const placementLegs = (slip: Slip, book: string) =>
  slip.legs.map((leg) => ({
    fixture_id: leg.fixture_id,
    home: leg.home,
    away: leg.away,
    sport_key: leg.sport_key,
    league: leg.league,
    kickoff: leg.kickoff,
    market: leg.market,
    selection: leg.selection,
    odds: String(leg.prices[book] ?? leg.prices[slip.book] ?? Object.values(leg.prices)[0]),
    fair_probability: leg.fair_probability,
  }));

const signedPct = (v: number, digits = 1): string => `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(digits)}%`;

/** The hero's market pulse, from the same slips the generator shows. */
export const ashokaHeadline = (p: SlipsPayload | undefined): string => {
  if (!p) return "ASHOKA is reading the market.";
  const vetted = p.slips.filter((s) => s.tier === "VETTED");
  const best = [...vetted].sort((a, b) => b.simulation.joint_ev - a.simulation.joint_ev)[0];  // the headline quotes a vetted slip, never a value one
  if (best)
    return `ASHOKA ACTIVE. ${vetted.length} slip${vetted.length === 1 ? "" : "s"} vetted from ${p.scanned} scanned across ${p.read.fixtures} fixtures. Best vetted joint EV ${signedPct(best.simulation.joint_ev)} at ${(best.simulation.joint_probability * 100).toFixed(0)}% likely.`;
  return `ASHOKA ACTIVE. ${p.read.fixtures} fixtures, ${p.scanned} slips scanned: nothing clears the 1000% bar right now.`;
};
