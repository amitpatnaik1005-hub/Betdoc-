import { create } from "zustand";
import { COMMANDER_LIST, resolveCommander } from "../config/commanders.config";
import type { CommanderId, CommanderProfile, CommanderStatus } from "../config/commanders.config";

interface CommanderState {
  activeCommander: CommanderProfile;
  statuses: Readonly<Record<CommanderId, CommanderStatus>>;
  syncCommander: (pathname: string) => void;
  setCommanderStatus: (id: CommanderId, status: CommanderStatus) => void;
}

const initialPathname = (): string => (typeof window !== "undefined" ? window.location.pathname : "/");

const initialStatuses = Object.fromEntries(COMMANDER_LIST.map((c) => [c.id, c.status])) as Record<CommanderId, CommanderStatus>;

export const useCommanderStore = create<CommanderState>()((set, get) => ({
  // Correct on the very first paint: derived from the URL at store creation.
  activeCommander: resolveCommander(initialPathname()),
  statuses: initialStatuses,

  syncCommander: (pathname) => {
    const next = resolveCommander(pathname);
    if (get().activeCommander.id !== next.id) set({ activeCommander: next });
  },

  setCommanderStatus: (id, status) =>
    set((state) => (state.statuses[id] === status ? state : { statuses: { ...state.statuses, [id]: status } })),
}));
