/**
 * The real-money vault's bankroll store (Group 95; execution tickets retired with the Live Desk in Group 97).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc.
 *
 * Deposit and withdrawal records and the Section 115BBJ certificate, shown in The Vault.
 */

import { create } from "zustand";
import { ApiError } from "../api/client";
import { depositRealCash, withdrawRealCash, type RealBankroll } from "../lib/real_money";
import { formatINR } from "../utils/formatCurrency";
import { toast } from "./useToastStore";

interface RealMoneyState {
  isDepositModalOpen: boolean;
  isWithdrawModalOpen: boolean;
  isTaxCertModalOpen: boolean;
  isLoading: boolean;
  error: string | null;
  developerCredit: string;

  openDepositModal: () => void;
  closeDepositModal: () => void;
  openWithdrawModal: () => void;
  closeWithdrawModal: () => void;
  openTaxCertModal: () => void;
  closeTaxCertModal: () => void;

  deposit: (amount: number, reference?: string, notes?: string) => Promise<RealBankroll | null>;
  withdraw: (amount: number, notes?: string) => Promise<RealBankroll | null>;
}

const explain = (err: unknown): string => {
  if (err instanceof ApiError) {
    if (err.detail && typeof err.detail === "object") {
      const msg = (err.detail as Record<string, unknown>).message;
      if (typeof msg === "string") return msg;
    }
    return err.message || "Request failed";
  }
  return err instanceof Error ? err.message : "Request failed";
};

export const useRealMoneyStore = create<RealMoneyState>((set) => ({
  isDepositModalOpen: false,
  isWithdrawModalOpen: false,
  isTaxCertModalOpen: false,
  isLoading: false,
  error: null,
  developerCredit: "Amit Ashok Kumar Patnaik",

  openDepositModal: () => set({ isDepositModalOpen: true }),
  closeDepositModal: () => set({ isDepositModalOpen: false }),

  openWithdrawModal: () => set({ isWithdrawModalOpen: true }),
  closeWithdrawModal: () => set({ isWithdrawModalOpen: false }),

  openTaxCertModal: () => set({ isTaxCertModalOpen: true }),
  closeTaxCertModal: () => set({ isTaxCertModalOpen: false }),

  deposit: async (amount, reference, notes) => {
    set({ isLoading: true, error: null });
    try {
      const bankroll = await depositRealCash({ amount, currency: "INR", reference, notes });
      set({ isDepositModalOpen: false });
      toast.success("Deposit Recorded", `Credited ${formatINR(amount)} to liquid cash vault.`);
      return bankroll;
    } catch (err) {
      const msg = explain(err);
      set({ error: msg });
      toast.error("Deposit Failed", msg);
      return null;
    } finally {
      set({ isLoading: false });
    }
  },

  withdraw: async (amount, notes) => {
    set({ isLoading: true, error: null });
    try {
      const bankroll = await withdrawRealCash({ amount, notes });
      set({ isWithdrawModalOpen: false });
      toast.success("Withdrawal Recorded", `Withdrew ${formatINR(amount)} from liquid cash vault.`);
      return bankroll;
    } catch (err) {
      const msg = explain(err);
      set({ error: msg });
      toast.error("Withdrawal Failed", msg);
      return null;
    } finally {
      set({ isLoading: false });
    }
  },
}));
