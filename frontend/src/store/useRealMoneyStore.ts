/**
 * Real-Money Execution & Bankroll Store (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Coordinates execution modals, clipboard slips, and bankroll mutations with toasts.
 */

import { create } from "zustand";
import { ApiError } from "../api/client";
import {
  confirmTicketPlacement,
  createRealTicket,
  depositRealCash,
  settleRealTicket,
  withdrawRealCash,
  type ConfirmPlacementInput,
  type CreateRealTicketInput,
  type RealBankroll,
  type RealBetTicket,
  type SettleRealTicketInput,
} from "../lib/real_money";
import { formatINR } from "../utils/formatCurrency";
import { toast } from "./useToastStore";

interface RealMoneyState {
  activeTicket: RealBetTicket | null;
  isExecutionModalOpen: boolean;
  isDepositModalOpen: boolean;
  isWithdrawModalOpen: boolean;
  isTaxCertModalOpen: boolean;
  isLoading: boolean;
  error: string | null;
  developerCredit: string;

  openExecutionModal: (ticket: RealBetTicket) => void;
  closeExecutionModal: () => void;
  openDepositModal: () => void;
  closeDepositModal: () => void;
  openWithdrawModal: () => void;
  closeWithdrawModal: () => void;
  openTaxCertModal: () => void;
  closeTaxCertModal: () => void;

  createAndOpenTicket: (input: CreateRealTicketInput) => Promise<RealBetTicket | null>;
  confirmPlacement: (input: ConfirmPlacementInput) => Promise<RealBetTicket | null>;
  settleTicket: (input: SettleRealTicketInput) => Promise<RealBetTicket | null>;
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
  activeTicket: null,
  isExecutionModalOpen: false,
  isDepositModalOpen: false,
  isWithdrawModalOpen: false,
  isTaxCertModalOpen: false,
  isLoading: false,
  error: null,
  developerCredit: "Amit Ashok Kumar Patnaik",

  openExecutionModal: (ticket) => set({ activeTicket: ticket, isExecutionModalOpen: true }),
  closeExecutionModal: () => set({ isExecutionModalOpen: false }),

  openDepositModal: () => set({ isDepositModalOpen: true }),
  closeDepositModal: () => set({ isDepositModalOpen: false }),

  openWithdrawModal: () => set({ isWithdrawModalOpen: true }),
  closeWithdrawModal: () => set({ isWithdrawModalOpen: false }),

  openTaxCertModal: () => set({ isTaxCertModalOpen: true }),
  closeTaxCertModal: () => set({ isTaxCertModalOpen: false }),

  createAndOpenTicket: async (input) => {
    set({ isLoading: true, error: null });
    try {
      const ticket = await createRealTicket(input);
      set({ activeTicket: ticket, isExecutionModalOpen: true });
      toast.success(
        "Parimatch Slip Built",
        `ASHVATTHAMA sized ${formatINR(ticket.recommended_stake)} (Kelly) at floor ${ticket.minimum_profitable_odds.toFixed(2)}.`
      );
      return ticket;
    } catch (err) {
      const msg = explain(err);
      set({ error: msg });
      toast.error("Execution Ticket Refused", msg);
      return null;
    } finally {
      set({ isLoading: false });
    }
  },

  confirmPlacement: async (input) => {
    set({ isLoading: true, error: null });
    try {
      const ticket = await confirmTicketPlacement(input);
      set({ activeTicket: ticket });
      toast.success(
        "Real Stake Locked",
        `Confirmed ${formatINR(ticket.placed_stake)} on ${ticket.event_name} @ ${ticket.placed_odds?.toFixed(2) ?? ticket.target_odds.toFixed(2)}.`
      );
      return ticket;
    } catch (err) {
      const msg = explain(err);
      set({ error: msg });
      toast.error("Placement Confirmation Failed", msg);
      return null;
    } finally {
      set({ isLoading: false });
    }
  },

  settleTicket: async (input) => {
    set({ isLoading: true, error: null });
    try {
      const ticket = await settleRealTicket(input);
      set({ activeTicket: ticket });
      if (ticket.status === "WON" || ticket.status === "HALF_WON") {
        toast.success(
          `Ticket Settled (${ticket.status})`,
          `Payout: ${formatINR(ticket.settlement_payout)} | Section 115BBJ 30% TDS: -${formatINR(ticket.tds_deducted)} | Net: +${formatINR(ticket.net_profit)}`
        );
      } else if (ticket.status === "LOST" || ticket.status === "HALF_LOST") {
        toast.warning(
          `Ticket Settled (${ticket.status})`,
          `Stake forfeited: -${formatINR(Math.abs(ticket.net_profit))}`
        );
      } else {
        toast.info(`Ticket Settled (${ticket.status})`, `Stake returned to liquid cash balance.`);
      }
      return ticket;
    } catch (err) {
      const msg = explain(err);
      set({ error: msg });
      toast.error("Settlement Refused", msg);
      return null;
    } finally {
      set({ isLoading: false });
    }
  },

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
