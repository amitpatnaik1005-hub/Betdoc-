import { create } from 'zustand';

interface UIState {
  isLeftCollapsed: boolean;
  isRightCollapsed: boolean;
  toggleLeft: () => void;
  toggleRight: () => void;
}

export const useUIStore = create<UIState>((set) => ({
  isLeftCollapsed: false,
  isRightCollapsed: false,
  toggleLeft: () => set((s) => ({ isLeftCollapsed: !s.isLeftCollapsed })),
  toggleRight: () => set((s) => ({ isRightCollapsed: !s.isRightCollapsed })),
}));
