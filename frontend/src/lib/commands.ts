import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import type { PaletteCommand } from "../components/CommandPalette";

/** Navigation entries for every section, shared by the palette. */
export function useNavigateCommands(items: readonly { to: string; label: string; icon: string }[]): PaletteCommand[] {
  const navigate = useNavigate();
  return useMemo(
    () => items.map((i) => ({ id: `nav:${i.to}`, label: i.label, icon: i.icon, group: "Navigate" as const, run: () => navigate(i.to) })),
    [items, navigate],
  );
}
