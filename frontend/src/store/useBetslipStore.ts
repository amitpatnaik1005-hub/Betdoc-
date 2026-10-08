/**
 * The slide-over betslip: one Aryabhata signal, staked and sent through the CFO ledger's two-phase
 * execution (`POST /omni/execute-trade`).
 *
 * Idempotency: every logical order carries a fresh uuid4. Loading a signal or changing the stake
 * starts a new order (new key). A definitive refusal also mints a new key so "Try again" is a new
 * order. A request that died in transit keeps its key: resending it can only come back as a
 * duplicate, never as a second bet.
 */
import { create } from "zustand";
import { ApiError, apiClient } from "../api/client";
import { type ExecutionReceipt, REFUSALS } from "../lib/cfo";
import { invalidate } from "../lib/resource";
import type { ArenaSignal } from "./useArenaStore";
import { newIdempotencyKey } from "./useExecutionStore";
import { toast } from "./useToastStore";

export type SlipPhase = "editing" | "submitting" | "executed" | "unknown" | "failed";

interface SlipError {
  message: string;
  reason: string | null;
  /** True when the request may have reached the server: the order's fate is unknown. */
  inFlight: boolean;
}

interface BetslipState {
  open: boolean;
  signal: ArenaSignal | null;
  stake: string;
  idempotencyKey: string;
  phase: SlipPhase;
  receipt: ExecutionReceipt | null;
  error: SlipError | null;
  load: (signal: ArenaSignal) => void;
  setStake: (raw: string) => void;
  close: () => void;
  /** Send the order at `odds` (the live price the slip shows). */
  confirm: (odds: number, expiresAt: string | null) => Promise<void>;
}

/** "1234.5" -> 1234.5; anything not a positive amount with at most 2 decimals -> null. */
export function parseStake(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) && value > 0 ? value : null;
}

export const useBetslipStore = create<BetslipState>()((set, get) => ({
  open: false,
  signal: null,
  stake: "",
  idempotencyKey: newIdempotencyKey(),
  phase: "editing",
  receipt: null,
  error: null,

  load: (signal) => {
    if (get().phase === "submitting") return; // never swap the order out from under an in-flight request
    set({
      open: true,
      signal,
      // Whole rupees, rounded down: the slip never starts above what the engine recommended
      stake: signal.kelly_stake_inr >= 1 ? String(Math.floor(signal.kelly_stake_inr)) : "",
      idempotencyKey: newIdempotencyKey(),
      phase: "editing",
      receipt: null,
      error: null,
    });
  },

  setStake: (raw) => {
    if (get().phase === "submitting") return;
    set({ stake: raw.replace(/[^\d.]/g, ""), idempotencyKey: newIdempotencyKey(), phase: "editing", error: null, receipt: null });
  },

  close: () => {
    if (get().phase === "submitting") return;
    set({ open: false });
  },

  confirm: async (odds, expiresAt) => {
    const { signal, stake, idempotencyKey, phase } = get();
    const amount = parseStake(stake);
    if (!signal || amount === null || phase === "submitting" || phase === "executed" || phase === "unknown") return;
    set({ phase: "submitting", error: null });
    try {
      const receipt = await apiClient.post<ExecutionReceipt>("/omni/execute-trade", {
        idempotency_key: idempotencyKey,
        fixture_id: signal.fixture_id,
        market: signal.market_type,
        selection: signal.selection,
        bookmaker_id: signal.bookmaker_id,
        // Decimal-safe strings: the ledger refuses sub-paisa stakes and odds beyond 4 places
        odds: odds.toFixed(4).replace(/0+$/, "").replace(/\.$/, ""),
        stake_inr: amount.toFixed(2),
        true_prob: signal.true_prob.toFixed(10),
        signal_id: signal.signal_id,
        signal_expires_at: expiresAt,
      });
      set({ phase: receipt.status === "EXECUTED" ? "executed" : "unknown", receipt });
      invalidate("cfo");
      if (receipt.status === "EXECUTED") toast.success("Bet placed", `${signal.home_team} v ${signal.away_team} · ${amount.toFixed(2)} @ ${odds.toFixed(2)}`);
      else toast.warning("Placement unconfirmed", "The stake is held in exposure until the bookmaker confirms.");
    } catch (err: unknown) {
      const api = err instanceof ApiError ? err : null;
      const inFlight = api === null || api.status === 0 || (api.status >= 500 && api.reason === null);
      set({
        phase: "failed",
        error: {
          message: api?.message ?? "Unexpected error",
          reason: api?.reason ?? null,
          inFlight,
        },
        // A definitive refusal ends this order; a lost response keeps its key (a resend is a duplicate, not a second bet)
        idempotencyKey: inFlight ? idempotencyKey : newIdempotencyKey(),
      });
      invalidate("cfo");
      toast.error(api?.reason ? REFUSALS[api.reason] ?? "Order refused" : "Order not confirmed", api?.message);
    }
  },
}));
