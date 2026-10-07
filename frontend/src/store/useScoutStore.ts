import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export interface ScoutMessage {
  id: string;
  role: "user" | "scout";
  content: string;
  context?: string;
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
