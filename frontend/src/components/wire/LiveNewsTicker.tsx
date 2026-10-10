/**
 * VIDUR's live ticker: the latest scored headlines with their tactical impact. Developed for Amit Ashok Kumar Patnaik.
 * The strip loops at a reading pace and pauses under the pointer; reduced motion shows a static row.
 */
import { motion, useReducedMotion } from "framer-motion";
import { useState } from "react";
import { type Impact, type NewsItem } from "../../lib/the_wire";

const BADGE: Record<Impact, string> = {
  CRITICAL: "bg-rose-500/15 text-rose-600 ring-rose-500/30 dark:text-rose-300",
  HIGH: "bg-amber-500/15 text-amber-700 ring-amber-500/30 dark:text-amber-300",
  MEDIUM: "bg-sky-500/15 text-sky-700 ring-sky-500/30 dark:text-sky-300",
  LOW: "bg-stone-500/10 text-stone-500 ring-stone-500/20 dark:text-stone-400",
};

export const LiveNewsTicker = ({ items, live }: { items: NewsItem[]; live: boolean }) => {
  const reduce = useReducedMotion();
  const [paused, setPaused] = useState(false);
  if (items.length === 0) return null;
  const row = items.slice(0, 20);
  const strip = (key: string) => (
    <div key={key} className="flex shrink-0 items-center gap-8 pr-8">
      {row.map((n) => {
        const impact = n.tactical_impact ?? "LOW";
        return (
          <a key={`${key}-${n.id}`} href={n.url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-2 whitespace-nowrap text-xs text-stone-600 hover:underline dark:text-stone-300">
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ring-1 ${BADGE[impact]}`}>{impact}</span>
            <span className="font-semibold text-stone-400">{n.source}</span>
            <span>{n.title}</span>
          </a>
        );
      })}
    </div>
  );
  return (
    <div className="lg:col-span-12 flex items-center overflow-hidden rounded-2xl bg-stone-50 ring-1 ring-stone-900/5 dark:bg-white/[0.03] dark:ring-white/10"
         onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <div className="z-10 flex shrink-0 items-center gap-2 border-r border-stone-900/5 bg-stone-100 px-3 py-2 font-mono text-[11px] font-bold uppercase tracking-wider text-stone-600 dark:border-white/10 dark:bg-white/[0.05] dark:text-stone-300">
        <span className={`h-2 w-2 rounded-full ${live ? "bg-emerald-500 animate-pulse" : "bg-stone-400"}`} />
        Vidur wire
      </div>
      <div className="min-w-0 flex-1 overflow-hidden py-2">
        {reduce ? (
          <div className="flex overflow-x-auto px-3">{strip("a")}</div>
        ) : (
          <motion.div className="flex w-max" animate={paused ? undefined : { x: ["0%", "-50%"] }} transition={{ repeat: Infinity, ease: "linear", duration: Math.max(30, row.length * 6) }}>
            {strip("a")}
            {strip("b")}
          </motion.div>
        )}
      </div>
    </div>
  );
};
