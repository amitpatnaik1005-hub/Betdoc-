import { create } from 'zustand';

// The execution panel takes 380px; below this width it starts collapsed so sections keep room.
const WIDE_LAYOUT_PX = 1440;
const startsNarrow = typeof window !== 'undefined' && window.innerWidth < WIDE_LAYOUT_PX;

interface UIState {
  isLeftCollapsed: boolean;
  isRightCollapsed: boolean;
  toggleLeft: () => void;
  toggleRight: () => void;
  openRight: () => void;
}

export const useUIStore = create<UIState>((set) => ({
  isLeftCollapsed: false,
  isRightCollapsed: startsNarrow,
  toggleLeft: () => set((s) => ({ isLeftCollapsed: !s.isLeftCollapsed })),
  toggleRight: () => set((s) => ({ isRightCollapsed: !s.isRightCollapsed })),
  openRight: () => set({ isRightCollapsed: false }),
}));
