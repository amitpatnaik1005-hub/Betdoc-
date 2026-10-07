import { create } from 'zustand';

// The execution panel takes 380px; below this width it starts collapsed so sections keep room.
const WIDE_LAYOUT_PX = 1440;
// Phones: the sidebar becomes an off-canvas drawer and the execution panel a full-screen sheet.
const COMPACT_QUERY = '(max-width: 767px)';

const hasWindow = typeof window !== 'undefined';
const startsNarrow = hasWindow && window.innerWidth < WIDE_LAYOUT_PX;
const compactNow = (): boolean => hasWindow && window.matchMedia(COMPACT_QUERY).matches;

interface UIState {
  isCompact: boolean;
  isLeftCollapsed: boolean;
  isRightCollapsed: boolean;
  toggleLeft: () => void;
  toggleRight: () => void;
  openRight: () => void;
  /** Dismiss the drawer after navigating on a phone; a no-op on wider screens. */
  closeLeftIfCompact: () => void;
}

export const useUIStore = create<UIState>((set, get) => ({
  isCompact: compactNow(),
  isLeftCollapsed: compactNow(),
  isRightCollapsed: startsNarrow,
  toggleLeft: () => set((s) => ({ isLeftCollapsed: !s.isLeftCollapsed })),
  toggleRight: () => set((s) => ({ isRightCollapsed: !s.isRightCollapsed })),
  openRight: () => set({ isRightCollapsed: false }),
  closeLeftIfCompact: () => {
    if (get().isCompact && !get().isLeftCollapsed) set({ isLeftCollapsed: true });
  },
}));

if (hasWindow) {
  // Crossing into phone width closes both overlays so content is never hidden behind them.
  window.matchMedia(COMPACT_QUERY).addEventListener('change', (e) => {
    useUIStore.setState(e.matches ? { isCompact: true, isLeftCollapsed: true, isRightCollapsed: true } : { isCompact: false });
  });
}
