import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useNavigate } from "react-router-dom";

export interface PaletteCommand {
  id: string;
  label: string;
  icon: string;
  group: "Navigate" | "Actions";
  hint?: string;
  run: () => void;
}

/** ⌘K / Ctrl+K: jump to any section or fire a real desk action without leaving the keyboard. */
export const CommandPalette = ({ commands }: { commands: PaletteCommand[] }) => {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === "Escape") setOpen(false);
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("betdoc:palette", onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("betdoc:palette", onOpen);
    };
  }, []);

  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
      window.setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? commands.filter((c) => `${c.label} ${c.hint ?? ""} ${c.group}`.toLowerCase().includes(q)) : commands;
  }, [commands, query]);

  const run = (cmd: PaletteCommand | undefined) => {
    if (!cmd) return;
    setOpen(false);
    cmd.run();
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[70] flex items-start justify-center bg-stone-900/30 px-4 pt-[14vh] backdrop-blur-sm dark:bg-black/50"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Command palette"
            initial={{ opacity: 0, y: -12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 420, damping: 34 }}
            className="w-full max-w-xl overflow-hidden rounded-2xl bg-white shadow-2xl ring-1 ring-stone-900/10 dark:bg-[#1c1917] dark:ring-white/10"
          >
            <div className="flex items-center gap-3 px-5">
              <span className="material-symbols-outlined text-stone-400">search</span>
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setIndex(0);
                }}
                onKeyDown={(e) => {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setIndex((i) => Math.min(results.length - 1, i + 1));
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setIndex((i) => Math.max(0, i - 1));
                  } else if (e.key === "Enter") run(results[index]);
                }}
                placeholder="Jump to a section or run an action…"
                className="h-12 flex-1 bg-transparent text-sm text-stone-900 outline-none placeholder:text-stone-400 dark:text-stone-100"
                aria-label="Search commands"
              />
              <kbd className="rounded-lg bg-stone-100 px-1.5 py-0.5 text-[10px] font-semibold text-stone-500 dark:bg-white/10 dark:text-stone-400">ESC</kbd>
            </div>
            <ul className="max-h-[50vh] overflow-y-auto p-2" role="listbox">
              {results.length === 0 && <li className="px-3 py-6 text-center text-sm text-stone-500">No matching command</li>}
              {results.map((cmd, i) => (
                <li key={cmd.id} role="option" aria-selected={i === index}>
                  <button
                    type="button"
                    onMouseEnter={() => setIndex(i)}
                    onClick={() => run(cmd)}
                    className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm ${
                      i === index ? "bg-stone-900/[0.05] dark:bg-white/[0.07]" : ""
                    }`}
                  >
                    <span className="material-symbols-outlined text-[18px] text-accent">{cmd.icon}</span>
                    <span className="flex-1 text-stone-800 dark:text-stone-100">{cmd.label}</span>
                    {cmd.hint && <span className="text-[11px] text-stone-400">{cmd.hint}</span>}
                    <span className="text-xs text-stone-400">{cmd.group}</span>
                  </button>
                </li>
              ))}
            </ul>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
};

/** Navigation entries for every section, shared by the palette. */
export function useNavigateCommands(items: readonly { to: string; label: string; icon: string }[]): PaletteCommand[] {
  const navigate = useNavigate();
  return useMemo(
    () => items.map((i) => ({ id: `nav:${i.to}`, label: i.label, icon: i.icon, group: "Navigate" as const, run: () => navigate(i.to) })),
    [items, navigate],
  );
}
