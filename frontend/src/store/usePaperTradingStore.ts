/**
 * Paper Trading & Mock Betting Sandbox Store (Group 80).
 * Allows forward-testing value bets with dummy capital at real-time odds.
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * Holds the desk's global mode (PAPER, the default, or LIVE) and display currency, kept across reloads. Server state
 * (the account, the bets, the statistics) lives in the resource cache (`lib/paper_trading.ts`), so every panel reads
 * one copy; the actions here place and reset through the API and report through toasts.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import { ApiError } from "../api/client";
import {
  placeOracleSlip,
  placePaperBet,
  resetPaperAccount,
  type CurrencyCode,
  type OracleSlipInput,
  type PaperBet,
  type PaperBetInput,
} from "../lib/paper_trading";
import { formatINR } from "../utils/formatCurrency";
import { toast } from "./useToastStore";

interface PaperTradingState {
  isPaperMode: boolean;
  displayCurrency: CurrencyCode;
  isLoading: boolean;
  error: string | null;
  lastBet: PaperBet | null;
  developerCredit: string;

  setPaperMode: (enabled: boolean) => void;
  setDisplayCurrency: (currency: CurrencyCode) => void;
  placeMockBet: (bet: PaperBetInput) => Promise<PaperBet | null>;
  placeAshokaSlip: (slip: OracleSlipInput) => Promise<PaperBet | null>;
  resetAccount: (initialBalance?: number) => Promise<boolean>;
}

const explain = (err: unknown): string => {
  if (err instanceof ApiError) {
    const message = err.detail && typeof err.detail.message === "string" ? err.detail.message : err.message;
    return message || "Request failed";
  }
  return err instanceof Error ? err.message : "Request failed";
};

const announce = (bet: PaperBet): void => {
  const receipt = bet.execution;
  const slip = receipt?.slippage_pct ? ` (${receipt.slippage_pct > 0 ? "-" : "+"}${Math.abs(receipt.slippage_pct).toFixed(2)}% vs requested)` : "";
  if (!bet.created) toast.info("Already placed", `${bet.event_name} is on the sandbox ledger.`);
  else toast.success("Paper bet filled", `ASHVATTHAMA filled ${formatINR(bet.stake_amount)} on ${bet.target_bookmaker} at ${bet.executed_odds.toFixed(2)}${slip}.`);
};

export const usePaperTradingStore = create<PaperTradingState>()(
  persist(
    (set) => ({
      isPaperMode: true,
      displayCurrency: "INR",
      isLoading: false,
      error: null,
      lastBet: null,
      developerCredit: "Amit Ashok Kumar Patnaik",

      setPaperMode: (enabled) => set({ isPaperMode: enabled }),
      setDisplayCurrency: (currency) => set({ displayCurrency: currency }),

      placeMockBet: async (bet) => {
        set({ isLoading: true, error: null });
        try {
          const placed = await placePaperBet({ target_bookmaker: "parimatch", ...bet });
          set({ lastBet: placed });
          announce(placed);
          return placed;
        } catch (err) {
          const message = explain(err);
          set({ error: message });
          toast.error("Paper bet refused", message);
          return null;
        } finally {
          set({ isLoading: false });
        }
      },

      placeAshokaSlip: async (slip) => {
        set({ isLoading: true, error: null });
        try {
          const placed = await placeOracleSlip({ target_bookmaker: "parimatch", ...slip });
          set({ lastBet: placed });
          announce(placed);
          return placed;
        } catch (err) {
          const message = explain(err);
          set({ error: message });
          toast.error("Paper bet refused", message);
          return null;
        } finally {
          set({ isLoading: false });
        }
      },

      resetAccount: async (initialBalance) => {
        set({ isLoading: true, error: null });
        try {
          const account = await resetPaperAccount(initialBalance);
          set({ lastBet: null });
          toast.success("Sandbox reset", `A fresh epoch at ${formatINR(account.virtual_balance)}.`);
          return true;
        } catch (err) {
          const message = explain(err);
          set({ error: message });
          toast.error("Reset failed", message);
          return false;
        } finally {
          set({ isLoading: false });
        }
      },
    }),
    {
      name: "betdoc-paper-trading-storage",
      partialize: (s) => ({ isPaperMode: s.isPaperMode, displayCurrency: s.displayCurrency }),
    },
  ),
);

export { explain as explainPaperError };
