import { create } from "zustand";
import { ApiError, apiClient } from "../api/client";

/*
 * CONTRACT WARNING: this payload follows the Group 27 spec. The backend
 * PlaceBetRequest (Group 23) currently requires:
 *   idempotency_key, exchange_account_id (UUID), match_id, market_type,
 *   selection, currency, odds, stake, true_probability
 * and has no `exchange_name` field. Until aligned, the backend answers 422.
 */

export type Selection = "HOME" | "AWAY" | "DRAW";
export const SELECTIONS: readonly Selection[] = ["HOME", "AWAY", "DRAW"];

export function isSelection(value: string): value is Selection {
  return (SELECTIONS as readonly string[]).includes(value);
}

export interface ExecutionPayload {
  match_id: string;
  selection: Selection;
  odds: number;
  stake: number;
  exchange_name: string;
  idempotency_key: string;
}

export interface DraftState {
  draftMatchId: string;
  draftSelection: Selection;
  draftOdds: string;
  draftStake: string;
  draftExchangeName: string;
}

interface PlaceBetResponse {
  bet_id: string;
  status: string;
}

interface ExecutionState extends DraftState {
  /** One key per logical order. Reused on retry while the outcome is unknown. */
  draftIdempotencyKey: string;
  isExecuting: boolean;
  lastError: string | null;
  lastSuccess: string | null;
  outcomeUnknown: boolean;
  setDraft: (matchId: string, selection: Selection, odds: number) => void;
  updateDraftField: (field: keyof DraftState, value: string) => void;
  placeBet: (payload: ExecutionPayload) => Promise<void>;
  clearMessages: () => void;
}

const UNRESOLVED_STATUSES: ReadonlySet<string> = new Set(["UNKNOWN", "PENDING_NETWORK"]);

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
  draftExchangeName: "mock",
  draftIdempotencyKey: newIdempotencyKey(),

  // Execution status
  isExecuting: false,
  lastError: null,
  lastSuccess: null,
  outcomeUnknown: false,

  setDraft: (matchId: string, selection: Selection, odds: number): void => {
    if (!Number.isFinite(odds)) return;
    set({
      draftMatchId: matchId,
      draftSelection: selection,
      draftOdds: String(odds),
      draftIdempotencyKey: newIdempotencyKey(), // any edit = new logical order
    });
  },

  updateDraftField: (field: keyof DraftState, value: string): void => {
    if (field === "draftSelection") {
      if (!isSelection(value)) return;
      set({ draftSelection: value, draftIdempotencyKey: newIdempotencyKey() });
      return;
    }
    set({ [field]: value, draftIdempotencyKey: newIdempotencyKey() } as Partial<ExecutionState>);
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

      if (res.status === "ACCEPTED") {
        set({ lastSuccess: `Bet ${res.bet_id} ACCEPTED` });
      } else if (res.status === "REJECTED") {
        set({ lastError: `Bet ${res.bet_id} REJECTED by exchange` });
      } else if (UNRESOLVED_STATUSES.has(res.status)) {
        unknown = true;
        set({
          lastError: `Bet ${res.bet_id} is ${res.status}. Do NOT re-place; awaiting reconciliation.`,
        });
      } else {
        set({ lastSuccess: `Bet ${res.bet_id} ${res.status}` });
      }
    } catch (err: unknown) {
      const message: string = err instanceof Error ? err.message : "Execution failed";
      unknown = !(err instanceof ApiError) || err.status === 0 || err.status >= 500;
      set({
        lastError: unknown
          ? `${message}. Outcome unknown; retrying reuses the same order ID.`
          : message,
      });
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
