import { create } from "zustand";
import { ApiError, apiClient } from "../api/client";
import { invalidate } from "../lib/resource";
import { toast } from "./useToastStore";
import { useUIStore } from "./useUIStore";

export type Selection = "HOME" | "AWAY" | "DRAW";
export const SELECTIONS: readonly Selection[] = ["HOME", "AWAY", "DRAW"];

export function isSelection(value: string): value is Selection {
  return (SELECTIONS as readonly string[]).includes(value);
}

/** Matches backend PlaceBetRequest. */
export interface ExecutionPayload {
  idempotency_key: string;
  exchange_name: string;
  match_id: string;
  market_type: string;
  selection: Selection;
  currency: string;
  odds: number;
  stake: number;
  true_probability: number;
}

export interface DraftState {
  draftMatchId: string;
  draftSelection: Selection;
  draftOdds: string;
  draftStake: string;
  draftExchangeName: string;
}

/** A bet handed over from another section (Oracle value bet, Arena price click, Command Center tip). */
export interface DraftHandoff {
  matchId: string;
  selection: Selection;
  odds: number;
  /** Model or consensus probability; drives the edge readout and is recorded on the ledger. */
  trueProbability?: number;
  label?: string;
  source?: string;
  stake?: number;
}

interface PlaceBetResponse {
  bet_id: string;
  status: string;
}

interface ExecutionState extends DraftState {
  /** One key per logical order. Reused on retry while the outcome is unknown. */
  draftIdempotencyKey: string;
  draftTrueProbability: number | null;
  draftLabel: string;
  draftSource: string;
  isExecuting: boolean;
  lastError: string | null;
  lastSuccess: string | null;
  outcomeUnknown: boolean;
  setDraft: (handoff: DraftHandoff) => void;
  updateDraftField: (field: keyof DraftState, value: string) => void;
  placeBet: (payload: ExecutionPayload) => Promise<void>;
  clearMessages: () => void;
}

const UNRESOLVED_STATUSES: ReadonlySet<string> = new Set(["UNKNOWN", "PENDING_NETWORK"]);
export const MONEY_SECTIONS = ["arena", "vault", "ledger", "capital", "dashboard", "telemetry", "commanders"];

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export const useExecutionStore = create<ExecutionState>()((set, get) => ({
  // Draft
  draftMatchId: "",
  draftSelection: "HOME",
  draftOdds: "",
  draftStake: "",
  draftExchangeName: "",
  draftIdempotencyKey: newIdempotencyKey(),
  draftTrueProbability: null,
  draftLabel: "",
  draftSource: "",

  // Execution status
  isExecuting: false,
  lastError: null,
  lastSuccess: null,
  outcomeUnknown: false,

  setDraft: ({ matchId, selection, odds, trueProbability, label, source, stake }: DraftHandoff): void => {
    if (!Number.isFinite(odds)) return;
    set({
      draftMatchId: matchId,
      draftSelection: selection,
      draftOdds: odds.toFixed(2),
      draftTrueProbability: trueProbability ?? null,
      draftLabel: label ?? "",
      draftSource: source ?? "",
      ...(stake !== undefined && Number.isFinite(stake) ? { draftStake: String(Math.round(stake)) } : {}),
      draftIdempotencyKey: newIdempotencyKey(), // any edit = new logical order
      lastError: null,
      lastSuccess: null,
    });
    useUIStore.getState().openRight();
    toast.info("Added to your bet slip", label ? `${label} · ${selection} @ ${odds.toFixed(2)}` : undefined);
  },

  updateDraftField: (field: keyof DraftState, value: string): void => {
    if (field === "draftSelection") {
      if (!isSelection(value)) return;
      set({ draftSelection: value, draftIdempotencyKey: newIdempotencyKey() });
      return;
    }
    // A manual price/match edit means the handed-over probability no longer applies.
    const resetsModel = field === "draftMatchId" || field === "draftOdds";
    set({
      [field]: value,
      draftIdempotencyKey: newIdempotencyKey(),
      ...(resetsModel ? { draftTrueProbability: null, draftSource: "" } : {}),
    } as Partial<ExecutionState>);
  },

  clearMessages: (): void => {
    set({ lastError: null, lastSuccess: null, outcomeUnknown: false });
  },

  placeBet: async (payload: ExecutionPayload): Promise<void> => {
    if (get().isExecuting) return;
    get().clearMessages();
    set({ isExecuting: true });

    let unknown: boolean = false;
    try {
      const res = await apiClient.post<PlaceBetResponse>("/execution/place-bet", payload);
      const ref = res.bet_id.slice(0, 8);

      if (res.status === "ACCEPTED") {
        set({ lastSuccess: `Order ${ref} ACCEPTED by ${payload.exchange_name}` });
        toast.success("Order accepted", `${payload.selection} @ ${payload.odds.toFixed(2)} · stake ₹${payload.stake}`);
      } else if (res.status === "REJECTED") {
        set({ lastError: `Order ${ref} REJECTED by the exchange` });
        toast.error("Order rejected by the exchange");
      } else if (UNRESOLVED_STATUSES.has(res.status)) {
        unknown = true;
        set({ lastError: `Order ${ref} is ${res.status}. Do NOT re-place; awaiting reconciliation.` });
        toast.warning("Order outcome unknown", "It stays reserved until reconciled.");
      } else {
        set({ lastSuccess: `Order ${ref} ${res.status}` });
      }
      invalidate(...MONEY_SECTIONS);
    } catch (err: unknown) {
      const message: string = err instanceof Error ? err.message : "Execution failed";
      unknown = !(err instanceof ApiError) || err.status === 0 || err.status >= 500;
      set({
        lastError: unknown ? `${message}. Outcome unknown; retrying reuses the same order ID.` : message,
      });
      if (!unknown) toast.error("Order refused", message);
    } finally {
      set({ isExecuting: false, outcomeUnknown: unknown });
      // Free the key for a new order only when the outcome is definitively known,
      // and only if the draft wasn't edited mid-flight (which already rotated it).
      if (!unknown && get().draftIdempotencyKey === payload.idempotency_key) {
        set({ draftIdempotencyKey: newIdempotencyKey() });
      }
    }
  },
}));
