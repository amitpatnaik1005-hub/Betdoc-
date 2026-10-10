/**
 * The manual parlay workbench and the in-play stop-loss shield (backend `app/api/v1/manual_parlay.py`, `inplay_ws.py`,
 * Group 77). Backtests run through the Lab (`lib/lab.ts`), which Group 77 extended.
 *
 * BOOKMAKER_THEMES dresses the workbench as the account the user bets from. The palette is presentation only:
 * every margin shown is measured from that book's live prices, never a fixed figure.
 */
import { useEffect, useRef, useState } from "react";
import { apiClient, readToken, wsUrl } from "../api/client";
import { useResource } from "./resource";

export type Skin = "parimatch" | "one_xbet" | "stake" | "betdoc";
export type Tier = "PERFECT" | "EXTRAORDINARY" | "BRILLIANT" | "GOOD" | "AVERAGE" | "POOR";
export type PillarStatus = "PASS" | "FAIL" | "UNVERIFIED" | "ADVISORY";

export interface BookmakerTheme {
  label: string;
  book: string | null; // the canonical book it prices at; null: the best of the retail books
  primary: string;
  surface: string;
  onPrimary: string;
  text: string;
  muted: string;
  card: string;
  font: string;
  couponStyle: string; // how its betslip reads
}

export const BOOKMAKER_THEMES: Record<Skin, BookmakerTheme> = {
  parimatch: { label: "Parimatch", book: "parimatch", primary: "#FFEE00", surface: "#121212", onPrimary: "#121212", text: "#FAFAFA", muted: "#A3A3A3", card: "#1E1E1E",
               font: "'Inter', system-ui, sans-serif", couponStyle: "high-contrast yellow coupon, bold totals" },
  one_xbet: { label: "1xBet", book: "1xbet", primary: "#00A3E0", surface: "#163963", onPrimary: "#FFFFFF", text: "#F1F5F9", muted: "#A9C1DA", card: "#1D4A7D",
              font: "'Roboto', system-ui, sans-serif", couponStyle: "navy betslip, accumulator badge" },
  stake: { label: "Stake", book: "stake", primary: "#00E701", surface: "#1A2C38", onPrimary: "#0B1620", text: "#E8EEF2", muted: "#8DA2B3", card: "#213743",
           font: "'Inter', system-ui, sans-serif", couponStyle: "minimal slate slip" },
  betdoc: { label: "BetDoc", book: null, primary: "#10B981", surface: "#030712", onPrimary: "#030712", text: "#F9FAFB", muted: "#9CA3AF", card: "#111827",
            font: "'JetBrains Mono', ui-monospace, monospace", couponStyle: "quant ledger: emerald and amber, monospace" },
};

export const TIER_STYLE: Record<Tier, { color: string; glow: string }> = {
  PERFECT: { color: "#FACC15", glow: "0 0 24px rgba(250, 204, 21, 0.65)" },
  EXTRAORDINARY: { color: "#A78BFA", glow: "0 0 20px rgba(167, 139, 250, 0.55)" },
  BRILLIANT: { color: "#F59E0B", glow: "0 0 18px rgba(245, 158, 11, 0.5)" },
  GOOD: { color: "#10B981", glow: "0 0 14px rgba(16, 185, 129, 0.4)" },
  AVERAGE: { color: "#94A3B8", glow: "none" },
  POOR: { color: "#F43F5E", glow: "none" },
};

// the structures a manual slip may take, by leg count
export const STRUCTURES: { kind: string; label: string; legs: [number, number]; lines: string }[] = [
  { kind: "SINGLE", label: "Single", legs: [1, 1], lines: "1 bet" },
  { kind: "DOUBLE", label: "Double", legs: [2, 2], lines: "1 bet" },
  { kind: "TREBLE", label: "Treble", legs: [3, 3], lines: "1 bet" },
  { kind: "ACCUMULATOR", label: "Accumulator", legs: [4, 20], lines: "1 bet" },
  { kind: "TRIXIE", label: "Trixie", legs: [3, 3], lines: "4 bets" },
  { kind: "PATENT", label: "Patent", legs: [3, 3], lines: "7 bets" },
  { kind: "YANKEE", label: "Yankee", legs: [4, 4], lines: "11 bets" },
  { kind: "CANADIAN", label: "Canadian", legs: [5, 5], lines: "26 bets" },
  { kind: "HEINZ", label: "Heinz", legs: [6, 6], lines: "57 bets" },
  { kind: "SUPER_HEINZ", label: "Super Heinz", legs: [7, 7], lines: "120 bets" },
  { kind: "GOLIATH", label: "Goliath", legs: [8, 8], lines: "247 bets" },
];

export interface SelectionCard {
  leg_id: string;
  selection: string;
  label: string;
  fair_probability: number;
  models: Record<string, number>;
  ev: number | null;
  prices: Record<string, number>;
  best: { book: string; odds: number } | null;
  steam: boolean | null;
}

export interface Margins {
  by_book: Record<string, number>;
  best_price: number | null;
  best_books: Record<string, string>;
}

export interface MarketCard {
  market: string;
  kind: string;
  selections: SelectionCard[];
  margins: Margins;
}

export interface FixtureCard {
  fixture_id: string;
  home: string;
  away: string;
  sport: string | null;
  sport_key: string | null;
  league: string | null;
  kickoff: string | null;
  in_play: boolean;
  minutes_since_kickoff: number | null;
  max_stake_inr: Record<string, string> | null;
  markets: MarketCard[];
}

export interface Board {
  sports: Record<string, number>;
  sport: string | null;
  fixtures: FixtureCard[];
  steam_readable: boolean;
  as_of: string;
}

export interface PillarRow {
  number: number;
  key: string;
  title: string;
  status: PillarStatus;
  credit: number;
  reason: string;
}

export interface Inspection {
  audit: {
    id: string;
    slip_id: string;
    kind: string;
    bookmaker: string | null;
    total_odds: string | null;
    stake_inr: string;
    joint_ev: number | null;
    joint_probability: number | null;
    is_vetted: boolean;
    pillars_passed: number;
    rejection_reasons: string[];
    slip: { legs: { fixture: string; label: string; prices: Record<string, number> }[] };
  };
  rating: { score: number; tier: Tier; capped: boolean; breakdown: PillarRow[]; advice: string[]; warnings: string[] };
  margins: { legs: (Margins & { leg_id: string; selection: string; account_margin: number | null })[]; account_book: string; parlay_overround: number | null };
  skin: Skin;
  book: string | null;
  stop_loss: { recommended_pct: number; min_pct: number; max_pct: number; floor_inr_at_twin_stake: string; basis: string };
  developer_credit: string;
}

export interface CashoutTicket {
  bookmaker: string;
  label: string;
  mode: "MANUAL";
  booking_code: string | null;
  stake_inr: string;
  floor_inr: string;
  value_inr: string;
  value_source: "offer" | "fair_value";
  reason: string;
  instructions: string[];
  caveats: string[];
  issued_at: string;
}

export interface ShieldFrame {
  type: "shield";
  monitor_id: string;
  bet_id: string;
  bookmaker: string;
  booking_code: string | null;
  stake_inr: string;
  stop_loss_pct: number | null;
  floor_inr: string | null;
  initial_win_prob: number;
  current_win_prob: number;
  fair_value_inr: string | null;
  cashout_offer_inr: string | null;
  last_advice: string | null;
  is_active: boolean;
  pullout_reason: string | null;
  at: string | null;
  ticket: CashoutTicket | null;
}

export interface Shield {
  id: string;
  bet_id: string;
  is_active: boolean;
  pullout_reason: string | null;
  stop_loss_pct: number | null;
  detail: { salvaged_inr?: string; cashout_ticket?: CashoutTicket };
  bet?: { stake_inr: string; status: string; bookmaker: string; booking_code: string | null; placed_odds: string | null };
  frame: ShieldFrame;
}

export interface Accounts {
  accounts: Record<string, { accounts: number; balances: Record<string, string>; reserved: Record<string, string>; last_updated: string | null }>;
  visible: boolean;
  message?: string;
}

export const useBoard = (sport: string | null) =>
  useResource(`parlay:board:${sport ?? "all"}`, () => apiClient.get<Board>("/manual-parlay/board", sport ? { sport } : undefined), { intervalMs: 30_000 });
export const useAccounts = () => useResource("parlay:accounts", () => apiClient.get<Accounts>("/manual-parlay/accounts"), { intervalMs: 300_000 });
export const useShields = () => useResource("parlay:shields", () => apiClient.get<{ shields: Shield[] }>("/manual-parlay/live-shields", { active_only: false }), { intervalMs: 15_000 });
export const inspectParlay = (body: { leg_ids: string[]; kind: string | null; skin: Skin; bankroll_inr?: string }) => apiClient.post<Inspection>("/manual-parlay/inspect", body);
export const submitParlay = (body: { audit_id: string; skin: Skin; stake_inr: string; placed_odds?: string; booking_code?: string; stop_loss_pct: number }) =>
  apiClient.post<{ bet_id: string; shield: { armed: boolean; reason?: string; message?: string } }>("/manual-parlay/submit", body);
export const emergencyCashout = (shieldId: string, amount?: string) =>
  apiClient.post<{ recorded: boolean; ticket?: CashoutTicket; salvaged_inr?: string }>(`/manual-parlay/emergency-cashout/${shieldId}`, amount ? { amount_inr: amount } : {});

/** Live shield frames from `/ws/inplay-shield`, by monitor id; reconnects with backoff, pings every 30 s. */
export function useShieldStream(): { frames: Record<string, ShieldFrame>; connected: boolean } {
  const [frames, setFrames] = useState<Record<string, ShieldFrame>>({});
  const [connected, setConnected] = useState(false);
  const retry = useRef(0);
  useEffect(() => {
    let socket: WebSocket | null = null;
    let ping: ReturnType<typeof setInterval> | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;
    const open = () => {
      const token = readToken();
      if (!token || closed) return;
      socket = new WebSocket(`${wsUrl("/ws/inplay-shield")}?token=${encodeURIComponent(token)}`);
      socket.onopen = () => {
        retry.current = 0;
        setConnected(true);
        ping = setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 30_000);
      };
      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(String(event.data)) as ShieldFrame | { type: "pong" };
          if (data.type === "shield") setFrames((prev) => ({ ...prev, [data.monitor_id]: data }));
        } catch {
          /* a malformed frame is dropped; the next tick replaces it */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (ping) clearInterval(ping);
        if (closed) return;
        retry.current = Math.min(retry.current + 1, 6);
        timer = setTimeout(open, 1000 * 2 ** retry.current);
      };
    };
    open();
    return () => {
      closed = true;
      if (ping) clearInterval(ping);
      if (timer) clearTimeout(timer);
      socket?.close();
    };
  }, []);
  return { frames, connected };
}
