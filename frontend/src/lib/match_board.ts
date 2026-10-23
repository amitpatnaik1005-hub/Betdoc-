/**
 * The Oracle's match board (backend `GET /oracle/board`, Group 103, points b.4 and b.11). Developed for Amit Ashok
 * Kumar Patnaik.
 *
 * The workbench board regrouped as a sportsbook lists it: sports (Cricket and Football first), countries and leagues,
 * time windows, a market column per row, and each fixture's markets by category. Every price is a feed quote, every
 * probability Ashoka's de-vigged consensus, every projected score the Dixon-Coles fit to that market. A price's drift
 * is what this browser saw it do between two polls of the board, never a reconstructed history.
 */
import { apiClient } from "../api/client";
import type { FixtureCard, MarketCard, SelectionCard } from "./backtest_and_parlay";
import { useResource } from "./resource";

export type BoardWindow = "live" | "3h" | "12h" | "upcoming" | "all";
export type MarketColumn = "winner" | "total" | "handicap";

export type ContextStatus = "SCHEDULED" | "IN_PLAY" | "PAUSED" | "FINISHED" | "POSTPONED" | "SUSPENDED" | "CANCELLED" | "UNKNOWN";
export interface Standing { position: number; points: number | null; played: number | null }

/** Group 112: what Football-Data.org or Sportmonks say about the fixture, where one paired it. */
export interface FixtureContext {
  provider: string;
  provider_id: string;
  competition: string | null;
  kickoff: { check: "CONFIRMED" | "MOVED"; provider_kickoff: string; difference_minutes: number };
  status: ContextStatus;
  score: { home: number; away: number } | null;
  standings: { home: Standing | null; away: Standing | null };
  provider_updated_at: string | null;
  checked_at: string;
}

export interface BoardFixture extends FixtureCard {
  country: string | null;
  league_name: string | null;
  market_count: number;
  selection_count: number;
  best_ev: number | null;
  /** Group 112: "stored" = the Odds API board as bought (no EV, nothing to add), "live" = the live market's prices. */
  priced_from: "live" | "stored";
  prices_as_of: string | null;
  prices_age_minutes: number | null;
  context: FixtureContext | null;
}

export const PROVIDER_LABEL: Record<string, string> = { "football-data.org": "Football-Data.org", sportmonks: "Sportmonks" };

export const ordinal = (n: number): string => {
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${suffix}`;
};

export interface MatchBoard {
  as_of: string;
  window: BoardWindow;
  sport: string | null;
  sports: { sport: string; count: number; live: number; priority: boolean }[];
  countries: { country: string | null; count: number; leagues: { league: string; count: number }[] }[];
  fixtures: BoardFixture[];
  total: number;
  steam_readable: boolean;
  /** leg_id -> book -> +1 (price rose) / -1 (fell) since this browser's previous poll. */
  drift: Record<string, Record<string, 1 | -1>>;
}

let lastPrices = new Map<string, number>();

function withDrift(board: Omit<MatchBoard, "drift">): MatchBoard {
  const drift: MatchBoard["drift"] = {};
  const now = new Map<string, number>();
  for (const f of board.fixtures) {
    for (const m of f.markets) {
      for (const s of m.selections) {
        for (const [book, price] of Object.entries(s.prices)) {
          const key = `${s.leg_id}@${book}`;
          now.set(key, price);
          const before = lastPrices.get(key);
          if (before !== undefined && Math.abs(price - before) > 1e-9) (drift[s.leg_id] ??= {})[book] = price > before ? 1 : -1;
        }
      }
    }
  }
  // Prices of fixtures outside this view are kept, so switching filters does not reset what was seen
  lastPrices = new Map([...lastPrices, ...now]);
  return { ...board, drift };
}

export const useMatchBoard = (sport: string | null, window: BoardWindow) =>
  useResource(`oracle:board:${sport ?? "all"}:${window}`, async () =>
    withDrift(await apiClient.get<Omit<MatchBoard, "drift">>("/oracle/board", { sport: sport ?? undefined, window })), { intervalMs: 30_000 });

/** A selection's price at a skin's book; BetDoc (no book): the best retail price (a stored board has no live best: its
 * highest stored price is shown, never offered). */
export const priceFor = (s: SelectionCard, book: string | null, stored = false): number | undefined => {
  if (book) return s.prices[book];
  if (s.best) return s.best.odds;
  const storedPrices = stored ? Object.values(s.prices) : [];
  return storedPrices.length ? Math.max(...storedPrices) : undefined;
};

/** How the shown price moved since the previous poll: at the skin's book, or at the book that supplied the best price. */
export const driftFor = (drift: MatchBoard["drift"], s: SelectionCard, book: string | null): 1 | -1 | undefined => {
  const source = book ?? s.best?.book;
  return source ? drift[s.leg_id]?.[source] : undefined;
};

// --------------------------------------------------------------------------- the row's market column
const closestToEven = (markets: MarketCard[], side: string): MarketCard | null =>
  markets
    .map((m) => ({ m, p: m.selections.find((s) => s.selection === side)?.fair_probability }))
    .filter((x): x is { m: MarketCard; p: number } => x.p !== undefined)
    .sort((a, b) => Math.abs(a.p - 0.5) - Math.abs(b.p - 0.5))[0]?.m ?? null;

/** Winner: the match odds; Total / Handicap: the main line (the one priced closest to even). */
export function columnMarket(card: FixtureCard, column: MarketColumn): MarketCard | null {
  if (column === "winner") return card.markets.find((m) => m.kind === "MATCH_ODDS") ?? null;
  if (column === "total") return closestToEven(card.markets.filter((m) => m.kind === "TOTALS"), "OVER");
  return closestToEven(card.markets.filter((m) => m.kind === "ASIAN_HANDICAP"), "HOME");
}

/** The short label a row's odds box shows: 1 / X / 2, O / U with the line, the home handicap. */
export function boxLabel(market: MarketCard, s: SelectionCard): string {
  const line = /(-?\+?\d+(?:\.\d+)?)$/.exec(market.market)?.[1];
  switch (market.kind) {
    case "MATCH_ODDS": return s.selection === "HOME" ? "1" : s.selection === "DRAW" ? "X" : "2";
    case "TOTALS": return `${s.selection === "OVER" ? "O" : "U"} ${line ?? ""}`.trim();
    case "ASIAN_HANDICAP": {
      const home = Number(line ?? 0);
      const value = s.selection === "HOME" ? home : -home;
      return `${s.selection === "HOME" ? "1" : "2"} (${value > 0 ? "+" : ""}${value})`;
    }
    default: return s.selection;
  }
}

// --------------------------------------------------------------------------- the detail's category tabs
export type Category = "all" | "main" | "bet_builder" | "parlays" | "halves" | "total" | "corners" | "goals" | "combo" | "props";

export const CATEGORIES: { id: Category; label: string; note?: string }[] = [
  { id: "all", label: "All" },
  { id: "main", label: "Main" },
  { id: "bet_builder", label: "Bet Builder", note: "Same-match legs: the fortress prices their correlation when you inspect the slip." },
  { id: "parlays", label: "Parlays" },
  { id: "halves", label: "Halves / Innings" },
  { id: "total", label: "Total" },
  { id: "corners", label: "Corners / Overs" },
  { id: "goals", label: "Goals / Runs" },
  { id: "combo", label: "Combo" },
  { id: "props", label: "Cards / Player props" },
];

const KIND_CATEGORY: Record<string, Category> = {
  MATCH_ODDS: "main", DOUBLE_CHANCE: "main", DRAW_NO_BET: "main", ASIAN_HANDICAP: "main", TOTALS: "total", BTTS: "goals",
};

/** The board's markets under a category tab; tabs no feed prices hold nothing (said so in the view, never filled). */
export function marketsIn(card: FixtureCard, category: Category): MarketCard[] {
  if (category === "all" || category === "bet_builder") return card.markets;
  return card.markets.filter((m) => KIND_CATEGORY[m.kind] === category);
}

/** Positive-EV selections (Ashoka's EV at the best retail price) on fixtures not already in the slip, best first:
 * the "Add and win more!" carousel. Other fixtures only, so a booster never correlates with a leg already held. */
export function boosters(fixtures: BoardFixture[], slipFixtureIds: Set<string>, limit = 6) {
  const rows: { card: BoardFixture; market: MarketCard; selection: SelectionCard }[] = [];
  for (const card of fixtures) {
    if (slipFixtureIds.has(card.fixture_id)) continue;
    for (const market of card.markets) {
      for (const s of market.selections) {
        if (s.ev !== null && s.ev > 0 && s.best) rows.push({ card, market, selection: s });
      }
    }
  }
  return rows.sort((a, b) => (b.selection.ev ?? 0) - (a.selection.ev ?? 0)).slice(0, limit);
}
