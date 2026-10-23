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
  /** Group 107: the Infinity / Flexi view, the workspace edge to edge with the sidebar and the dock tucked away. */
  isInfinityView: boolean;
  toggleLeft: () => void;
  toggleRight: () => void;
  openRight: () => void;
  /** Dismiss the drawer after navigating on a phone; a no-op on wider screens. */
  closeLeftIfCompact: () => void;
  toggleInfinityView: () => void;
  setInfinityView: (on: boolean) => void;
}

// The sidebar and dock as they were before the Infinity view: leaving it puts them back exactly so.
let beforeInfinity: { left: boolean; right: boolean } | null = null;

export const useUIStore = create<UIState>((set, get) => ({
  isCompact: compactNow(),
  isLeftCollapsed: true,  // Group 111: the 56px icon rail on desktops, the closed drawer on phones
  isRightCollapsed: startsNarrow,
  isInfinityView: false,
  toggleLeft: () => set((s) => ({ isLeftCollapsed: !s.isLeftCollapsed })),
  toggleRight: () => set((s) => ({ isRightCollapsed: !s.isRightCollapsed })),
  openRight: () => set({ isRightCollapsed: false }),
  closeLeftIfCompact: () => {
    if (get().isCompact && !get().isLeftCollapsed) set({ isLeftCollapsed: true });
  },
  setInfinityView: (on) => {
    const s = get();
    if (on === s.isInfinityView) return;
    if (on) {
      beforeInfinity = { left: s.isLeftCollapsed, right: s.isRightCollapsed };
      set({ isInfinityView: true, isLeftCollapsed: true, isRightCollapsed: true });
    } else {
      const back = beforeInfinity ?? { left: true, right: startsNarrow };
      beforeInfinity = null;
      set({ isInfinityView: false, isLeftCollapsed: back.left, isRightCollapsed: back.right });
    }
  },
  toggleInfinityView: () => get().setInfinityView(!get().isInfinityView),
}));

if (hasWindow) {
  // Crossing into phone width closes both overlays so content is never hidden behind them.
  window.matchMedia(COMPACT_QUERY).addEventListener('change', (e) => {
    useUIStore.setState(e.matches ? { isCompact: true, isLeftCollapsed: true, isRightCollapsed: true } : { isCompact: false, isLeftCollapsed: true });
  });
}
