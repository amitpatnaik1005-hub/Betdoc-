import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

/** A clickable card under a Scout answer (Group 100): it opens a BetDoc page. */
export interface ScoutAction {
  type: "LOAD_PARLAY" | "NAVIGATE" | "OPEN_CONSULTANT" | "VIEW_PREDICTION";
  label: string;
  route: string;
}

/** Which engine wrote an answer: the BetDoc-only refusal, the local model, or the rule engine on the live book. */
export type ScoutSource = "guardrail" | "ollama" | "quant_nlg";

export interface ScoutMessage {
  id: string;
  role: "user" | "scout";
  content: string;
  context?: string;
  actions?: ScoutAction[];
  source?: ScoutSource;
}

interface ScoutState {
  messages: ScoutMessage[];
  append: (message: ScoutMessage) => void;
  clear: () => void;
}

const MAX_MESSAGES = 60;

/** Scout Oracle conversation for this browser session (the server keeps the durable history). */
export const useScoutStore = create<ScoutState>()(
  persist(
    (set) => ({
      messages: [],
      append: (message) => set((s) => ({ messages: [...s.messages, message].slice(-MAX_MESSAGES) })),
      clear: () => set({ messages: [] }),
    }),
    { name: "betdoc-scout-v2", storage: createJSONStorage(() => sessionStorage) },
  ),
);
