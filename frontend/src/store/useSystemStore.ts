import { create } from "zustand";
import type { OmniSocketStatus } from "../services/OmniGateway";

/** One entry of the cross-section event bus (`/ws/events`). */
export interface BusEvent {
  type: string;
  at: string;
  section?: string;
  path?: string;
  method?: string;
  status?: number;
  statuses?: Record<string, string>;
}

interface SystemState {
  busStatus: OmniSocketStatus;
  /** Newest first, capped. Feeds the Arena tactical log and the Command Center activity stream. */
  events: BusEvent[];
  /** Mirrors the Control Panel kill switch (max_daily_exposure <= 0). */
  halted: boolean;
  setBusStatus: (status: OmniSocketStatus) => void;
  pushEvent: (event: BusEvent) => void;
  setHalted: (halted: boolean) => void;
  reset: () => void;
}

const MAX_EVENTS = 60;

export const useSystemStore = create<SystemState>()((set) => ({
  busStatus: "idle",
  events: [],
  halted: false,
  setBusStatus: (busStatus) => set((s) => (s.busStatus === busStatus ? s : { busStatus })),
  pushEvent: (event) => set((s) => ({ events: [event, ...s.events].slice(0, MAX_EVENTS) })),
  setHalted: (halted) => set((s) => (s.halted === halted ? s : { halted })),
  reset: () => set({ busStatus: "idle", events: [], halted: false }),
}));
