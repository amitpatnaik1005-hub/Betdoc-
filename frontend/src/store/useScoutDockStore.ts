/**
 * The Scout Oracle dock's width and its unread badge (Group 100). Developed for Amit Ashok Kumar Patnaik.
 *
 * Whether the dock is open stays where it always was, `useUIStore.isRightCollapsed`, so every existing way of opening
 * it ("Ask the Scout", loading a bet, the palette, the phone layout) keeps working. This store only remembers how
 * wide it opens: docked (380px), expanded (600px) or max (half the screen; full screen on a phone).
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import { useUIStore } from "./useUIStore";

export type DockWidth = "docked" | "expanded" | "max";
export type DockMode = DockWidth | "collapsed";

const WIDTHS: readonly DockWidth[] = ["docked", "expanded", "max"];
const isDockWidth = (v: unknown): v is DockWidth => typeof v === "string" && (WIDTHS as readonly string[]).includes(v);

interface ScoutDockState {
  width: DockWidth;
  unreadInsights: number;
  setWidth: (width: DockWidth) => void;
  incrementUnread: () => void;
  clearUnread: () => void;
}

export const useScoutDockStore = create<ScoutDockState>()(
  persist(
    (set) => ({
      width: "docked",
      unreadInsights: 0,
      setWidth: (width) => set({ width: isDockWidth(width) ? width : "docked" }),
      incrementUnread: () => set((s) => ({ unreadInsights: Math.min(s.unreadInsights + 1, 99) })),
      clearUnread: () => set({ unreadInsights: 0 }),
    }),
    {
      name: "betdoc:scout-dock",
      partialize: (s) => ({ width: s.width }),
      merge: (persisted, current) => {
        const saved = (persisted as Partial<ScoutDockState> | undefined)?.width;
        return { ...current, width: isDockWidth(saved) ? saved : "docked" };
      },
    },
  ),
);

/** The dock's mode: collapsed (from the shell) or the remembered width. */
export const useScoutDockMode = (): DockMode => {
  const collapsed = useUIStore((s) => s.isRightCollapsed);
  const width = useScoutDockStore((s) => s.width);
  return collapsed ? "collapsed" : width;
};

/** Open at a width, or collapse; opening clears the unread badge. */
export function setScoutDockMode(mode: DockMode): void {
  if (mode === "collapsed") {
    useUIStore.setState({ isRightCollapsed: true });
    return;
  }
  useScoutDockStore.getState().setWidth(mode);
  useScoutDockStore.getState().clearUnread();
  useUIStore.setState({ isRightCollapsed: false });
}

/** Ctrl/Cmd+J and the palette: collapse an open dock, or reopen it at its remembered width. */
export function toggleScoutDock(): void {
  setScoutDockMode(useUIStore.getState().isRightCollapsed ? useScoutDockStore.getState().width : "collapsed");
}
