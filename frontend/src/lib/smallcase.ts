/**
 * Live smallcases (Group 107, point b.8): six thematic baskets of independent positive-EV singles, rebuilt from
 * ASHOKA's live market and sized leg by leg by KUMBHA (`/smallcase`). Every figure is the backend's; the baskets are
 * pushed as the market moves (`/ws/smallcases`, `smallcase_live.ts`). Loading a basket puts its legs on the Oracle's shared slip, with their real
 * prices, where the fortress inspects them. Developed for Amit Ashok Kumar Patnaik.
 */
import { apiClient } from "../api/client";
import { type Skin } from "./backtest_and_parlay";
import { useResource } from "./resource";
import { BOARD_KEY, useSmallcaseFeed } from "./smallcase_live";
import { type SlipLeg, useParlaySlipStore } from "../store/useParlaySlipStore";
import { toast } from "../store/useToastStore";

export type BasketTier = "FORTRESS" | "TACTICAL_ALPHA" | "ASYMMETRIC" | "MOMENTUM" | "CHEF_SPECIAL";

export interface Holding {
  leg_id: string;
  fixture_id: string;
  fixture: string;
  label: string;
  market: string;
  market_kind: string;
  sport_key: string;
  sport: string | null;
  league: string | null;
  kickoff: string | null;
  in_play: boolean;
  book: string;
  book_label: string;
  price: number;
  fair_probability: number;
  ev: number;
  prices: Record<string, number>;
  full_kelly: number;
  fraction: number;
  stake_inr: string;
  stake_usd: string | null;
  stop_loss: { pct: number; floor_inr: string };
  also_in?: string[];
}

export interface BasketRisk {
  weights: number[];
  expected_return: number;
  p_profit: number;
  p_lose_all: number;
  var_95: number;
  cvar_95: number;
  std_return: number;
  hhi: number;
  risk_parity_weights: number[];
  var_share: number[];
  paths: number;
  basis: string;
}

export interface ParlayView {
  odds: number;
  win_probability: number;
  ev: number;
}

export interface Basket {
  id: string;
  name: string;
  tier: BasketTier;
  scope: string;
  status: "LIVE" | "EMPTY";
  empty_reason: string | null;
  sports: string[];
  holdings: Holding[];
  stake_inr: string;
  stake_usd?: string | null;
  fraction?: number;
  cap_fraction?: number;
  scaled_to_cap?: boolean;
  expected_profit_inr?: string | null;
  risk: BasketRisk | null;
  parlay: ParlayView | null;
  regime?: string;
  damper?: number;
  halted?: boolean;
}

export interface Board {
  as_of: string;
  computed_at: string;
  scanned: number;
  valued: number;
  eligible: number;
  bankroll_inr: string | null;
  drawdown: number;
  skill_bss: number | null;
  halt_latched: boolean;
  inr_per_usd: string | null;
  refresh_seconds: number;
  policy: { min_ev: number; max_legs: number; safe_min_probability: number; underdog_min_odds: number; european_leagues: string[]; max_basket_fraction: number;
            kelly_multiplier: number };
  baskets: Basket[];
  developer_credit: string;
}

export interface SlipSmallcase {
  holdings: Holding[];
  stake_inr: string;
  stake_usd?: string | null;
  fraction?: number;
  cap_fraction?: number;
  scaled_to_cap?: boolean;
  expected_profit_inr?: string | null;
  risk: BasketRisk | null;
  parlay: ParlayView | null;
  as_built: ParlayView | null;
  excluded: { leg_id: string; label?: string; reason: "NOT_QUOTED" | "UNDER_FAIR" | "SAME_FIXTURE"; message: string }[];
  book: string | null;
  book_label: string;
  bankroll_inr: string | null;
  regime?: string;
  halted?: boolean;
}

export interface ComponentRow {
  key: string;
  kind: "BET_TYPE" | "MATH_MODEL" | "RISK_MODEL" | "STOP_LOSS" | "FORTRESS_PILLAR";
  name: string;
  description: string;
  implementation: string;
  implemented: boolean;
  used: { scope: "BASKET" | "HOLDING" | "INSPECTION" | "ACCOUNT"; role: string } | null;
}

export interface Components {
  components: ComponentRow[];
  by_kind: Record<ComponentRow["kind"], { catalogued: number; implemented: number; blueprint: number; BASKET: number; HOLDING: number; INSPECTION: number; ACCOUNT: number }>;
  totals: { catalogued: number; implemented: number; blueprint: number; BASKET: number; HOLDING: number; INSPECTION: number; ACCOUNT: number; used: number };
  baskets_live: number;
  note: string;
  as_of: string;
}

/** The six baskets: pushed over `/ws/smallcases` as the market moves, polled every 30 s while the socket is down (and every
 * 5 minutes as a safety net while it is up). */
export const useSmallcases = () => {
  const live = useSmallcaseFeed() === "open";
  return useResource(BOARD_KEY, () => apiClient.get<Board>("/smallcase/baskets"), { intervalMs: live ? 300_000 : 30_000 });
};
export const useComponents = (enabled: boolean) =>
  useResource(enabled ? "smallcase:components" : null, () => apiClient.get<Components>("/smallcase/components"), { intervalMs: 300_000 });
export const rebalanceBasket = (id: string) => apiClient.post<Omit<Board, "baskets"> & { basket: Basket }>(`/smallcase/baskets/${id}/rebalance`);
export const slipAsSmallcase = (body: { leg_ids: string[]; skin: Skin }) => apiClient.post<SlipSmallcase>("/smallcase/rebalance-slip", body);

export const TIER_LABEL: Record<BasketTier, { label: string; tone: "accent" | "good" | "warning" | "info" | "serious" }> = {
  FORTRESS: { label: "Fortress", tone: "good" },
  TACTICAL_ALPHA: { label: "Tactical alpha", tone: "info" },
  ASYMMETRIC: { label: "Asymmetric", tone: "serious" },
  MOMENTUM: { label: "Momentum", tone: "warning" },
  CHEF_SPECIAL: { label: "Chef special", tone: "accent" },
};

/** A holding as a leg of the Oracle's shared slip (its real prices at every book). */
export const toSlipLeg = (h: Holding): SlipLeg => ({
  leg_id: h.leg_id, fixture: h.fixture, label: h.label, market: h.market, prices: h.prices, fair_probability: h.fair_probability,
});

/** Put a basket's legs on the Oracle's shared slip, replacing what it held (callers ask first when it held legs). */
export const useLoadBasket = (afterLoad?: () => void) => {
  const legs = useParlaySlipStore((s) => s.legs);
  const clear = useParlaySlipStore((s) => s.clear);
  const addMany = useParlaySlipStore((s) => s.addMany);
  return {
    slipSize: legs.length,
    load: (basket: Basket) => {
      clear();
      addMany(basket.holdings.map(toSlipLeg));
      toast.success(`${basket.name} is on the slip`, `${basket.holdings.length} leg${basket.holdings.length === 1 ? "" : "s"} at their live prices: inspect them in the workbench before placing.`);
      afterLoad?.();
    },
  };
};

export const pct = (p: number | null | undefined, digits = 1, signed = false): string =>
  p === null || p === undefined ? "—" : `${signed && p >= 0 ? "+" : ""}${(p * 100).toFixed(digits)}%`;
