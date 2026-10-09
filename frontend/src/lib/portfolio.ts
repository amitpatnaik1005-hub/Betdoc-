/**
 * Live portfolio, hedging and arbitrage (backend `app/services/portfolio_manager.py`, Group 64).
 *
 * The portfolio streams over `/ws/portfolio` five times a second (every open book marked against
 * the live books, with its balanced and free-bet hedges); the arbitrage scan arrives on the same
 * socket whenever it changes. While the socket is not open, `GET /omni/portfolio` is polled.
 *
 * Money stays a string end to end here (the backend's exact Decimal text) until it is displayed.
 */
import { useEffect, useMemo, useState } from "react";
import { apiClient } from "../api/client";
import { OmniSocket, type OmniSocketStatus } from "../services/OmniGateway";
import { subscribeChannel } from "../services/realtime";
import { useResource } from "./resource";

export const PORTFOLIO_PATH = "/ws/portfolio";

export interface PricedOffer {
  selection: string;
  bookmaker_id: string;
  odds: string;
  true_odds: string; // after commission
  rupee_odds: string; // after commission and the FX haircut
  commission: string;
  currency: string;
}

export interface PlanLeg extends PricedOffer {
  stake_inr: string;
  stake_ccy: string;
}

export interface HedgePlan {
  kind: "free_bet" | "balanced" | "partial";
  fraction: string;
  anchor: string;
  target: string;
  hedge_stake_inr: string;
  legs: PlanLeg[];
  after: Record<string, string>;
  worst_after: string;
  best_after: string;
  locks_profit: boolean;
}

export interface BookBet {
  id: string;
  selection: string;
  bookmaker_id: string;
  stake_inr: string;
  requested_stake_inr: string | null;
  odds: string;
  status: string;
  unconfirmed: boolean;
  strategy: "arbitrage" | "hedge" | null;
  group_id: string | null;
  currency: string;
}

export interface MarketBook {
  market_key: string;
  fixture_id: string;
  market: string;
  home: string;
  away: string;
  commence_time: string | null;
  in_play: boolean;
  outcomes: string[];
  bets: BookBet[];
  staked_inr: string;
  live: Record<string, PricedOffer>;
  profits: Record<string, string> | null;
  worst_case: string | null;
  best_case: string | null;
  hedge: { anchor: string; balanced: HedgePlan; free_bet: HedgePlan } | null;
  cash_out: string | null;
  profitable: boolean;
  blocked: string | null;
  notes: string[];
}

export interface PortfolioPayload {
  type: "portfolio";
  seq: number;
  user_id: string;
  ts: string;
  totals: {
    open_bets: number;
    markets: number;
    staked_inr: string;
    cash_out: string | null;
    worst_case: string | null;
    best_case: string | null;
    profitable_hedges: number;
  };
  markets: MarketBook[];
  fx_missing: string[];
}

export interface ArbLeg extends PricedOffer {
  stake_inr: string;
  stake_ccy: string;
  payout_inr: string;
}

export interface ArbOpportunity {
  id: string;
  fixture_id: string;
  market: string;
  home: string;
  away: string;
  commence_time: string | null;
  booksum: string;
  margin_pct: string;
  roi_pct: string;
  total_stake_inr: string;
  guaranteed_profit_inr: string;
  legs: ArbLeg[];
  detected_at: string;
}

export type LegStatus = "FILLED" | "PARTIAL" | "UNCONFIRMED" | "FAILED" | "ABORTED" | "SKIPPED";

export interface LegReceipt {
  selection: string;
  bookmaker_id: string;
  odds: number;
  min_acceptable_odds: number | null;
  planned_stake_inr: number;
  requested_stake_inr: number | null;
  filled_stake_inr: number | null;
  matched_odds: number | null;
  status: LegStatus;
  reason: string | null;
  message: string | null;
  ledger_id: string | null;
  remote_bet_id: string | null;
}

export interface MultiLegReceipt {
  group_id: string;
  strategy: "arbitrage" | "hedge";
  status: "COMPLETE" | "LEGGED" | "ABORTED";
  message: string;
  legs: LegReceipt[];
  outcome_profits: Record<string, number>;
  worst_case: number;
  best_case: number;
  total_staked_inr: number;
  execution_mode: "paper" | "live";
}

export const LEG_REFUSALS: Record<string, string> = {
  ARB_INCOMPLETE: "Doesn't cover every outcome",
  NO_ARBITRAGE: "No longer an arbitrage after commission and FX",
  PRICE_MOVED: "A price moved",
  PRICE_UNAVAILABLE: "A book stopped quoting",
  MARKET_IN_PLAY: "Market is in play",
  NO_EXECUTION_VENUE: "No venue for that bookmaker",
  HEDGE_CHANGED: "The hedge changed",
  NO_HEDGE: "No hedge available",
  NOTHING_TO_HEDGE: "Nothing to hedge",
  UNCONFIRMED_BET: "A bet here is unconfirmed",
};

export const n = (value: string | number | null | undefined): number => (value === null || value === undefined ? 0 : Number(value));

/** The book's stream, with REST polling whenever the socket is not open. */
export function useLivePortfolio() {
  const [status, setStatus] = useState<OmniSocketStatus>("idle");
  const [live, setLive] = useState<PortfolioPayload | null>(null);
  const [arbs, setArbs] = useState<{ ts: string | null; arbs: ArbOpportunity[] } | null>(null);
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const socketOpen = status === "open";
  const fallback = useResource(socketOpen ? null : "cfo:portfolio", () => apiClient.get<PortfolioPayload>("/omni/portfolio"), { intervalMs: 3_000 });
  const scan = useResource(socketOpen ? null : "cfo:arbitrage", () => apiClient.get<{ ts: string | null; arbs: ArbOpportunity[] }>("/omni/arbitrage"), { intervalMs: 5_000 });

  useEffect(() => {
    const offStatus = OmniSocket.channel(PORTFOLIO_PATH).onStatus(setStatus);
    const off = subscribeChannel(PORTFOLIO_PATH, (data) => {
      if (data === null || typeof data !== "object") return;
      const frame = data as { type?: string };
      if (frame.type === "portfolio") {
        setLive(data as PortfolioPayload);
        setReceivedAt(Date.now());
      } else if (frame.type === "arbitrage") {
        setArbs(data as { ts: string | null; arbs: ArbOpportunity[] });
      }
    });
    return () => {
      off();
      offStatus();
    };
  }, []);

  const portfolio = socketOpen && live ? live : (fallback.data ?? live);
  return {
    status,
    portfolio,
    arbs: (socketOpen ? arbs : (scan.data ?? arbs))?.arbs ?? [],
    receivedAt: socketOpen ? receivedAt : fallback.updatedAt,
    error: portfolio ? null : fallback.error,
    loading: !portfolio && fallback.loading,
  };
}

/**
 * The slider between the free bet (0) and the balanced hedge (1). With the same outcomes hedged
 * at both ends, every stake and profit is linear in the target, so this is the backend's own
 * answer to within a paisa; the server re-solves on confirm and refuses if it moved > 2%.
 */
export function interpolatePlan(free: HedgePlan, balanced: HedgePlan, fraction: number): HedgePlan {
  const t = Math.min(1, Math.max(0, fraction));
  if (t <= 0) return free;
  if (t >= 1) return balanced;
  const mix = (a: string | undefined, b: string | undefined) => (n(a) * (1 - t) + n(b) * t).toFixed(2);
  const freeLegs = new Map(free.legs.map((leg) => [leg.selection, leg]));
  const balancedLegs = new Map(balanced.legs.map((leg) => [leg.selection, leg]));
  const selections = [...new Set([...freeLegs.keys(), ...balancedLegs.keys()])];
  const legs = selections.map((sel) => {
    const base = balancedLegs.get(sel) ?? freeLegs.get(sel)!;
    return { ...base, stake_inr: mix(freeLegs.get(sel)?.stake_inr, balancedLegs.get(sel)?.stake_inr), stake_ccy: mix(freeLegs.get(sel)?.stake_ccy, balancedLegs.get(sel)?.stake_ccy) };
  });
  const after = Object.fromEntries(Object.keys(balanced.after).map((o) => [o, mix(free.after[o], balanced.after[o])]));
  const values = Object.values(after).map(Number);
  return {
    ...balanced,
    kind: "partial",
    fraction: t.toFixed(4),
    target: mix(free.target, balanced.target),
    hedge_stake_inr: mix(free.hedge_stake_inr, balanced.hedge_stake_inr),
    legs,
    after,
    worst_after: Math.min(...values).toFixed(2),
    best_after: Math.max(...values).toFixed(2),
    locks_profit: Math.min(...values) > 0,
  };
}

/** Seconds since a timestamp, re-rendered once a second. */
export function useAge(at: number | null | undefined): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);
  return useMemo(() => (at ? Math.max(0, Math.round((now - at) / 1000)) : null), [at, now]);
}
