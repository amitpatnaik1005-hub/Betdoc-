/**
 * VIDUR's four-column intelligence board (Group 108, point b.14b): sport filters (cricket and football first), the
 * intelligence categories, a search, and fixture-linked items only by default (a story that names no fixture on the
 * live board has no market impact to show). Every row is a `WireIntelligenceCard`. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { formatTime } from "../../lib/format";
import { ago, type BoardCategory, type BoardFilters, type BoardSport, CATEGORY_LABEL, useWireBoard } from "../../lib/neural_bus";
import { Async, Button, EmptyState, Panel } from "../../ui/kit";
import { WireIntelligenceCard } from "./WireIntelligenceCard";

const SPORTS: BoardSport[] = ["ALL", "CRICKET", "FOOTBALL", "TENNIS", "MMA"];
const CATEGORIES: BoardCategory[] = ["ALL", "LINEUP_INJURY", "WEATHER_PITCH", "ODDS_STEAM", "TACTICAL_ROTATION"];

const Chip = ({ active, onClick, children, count }: { active: boolean; onClick: () => void; children: string; count?: number }) => (
  <button type="button" onClick={onClick} aria-pressed={active}
          className={`inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs font-semibold transition-colors duration-200 ${active
            ? "bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900"
            : "bg-stone-100 text-stone-600 hover:bg-stone-200 dark:bg-white/[0.06] dark:text-stone-300 dark:hover:bg-white/[0.1]"}`}>
    {children}
    {count !== undefined && <span className={`font-mono text-[10px] ${active ? "opacity-80" : "text-stone-400"}`}>{count}</span>}
  </button>
);

export const WireIntelligenceBoard = ({ className }: { className?: string }) => {
  const [filters, setFilters] = useState<BoardFilters>({ sport: "ALL", category: "ALL", q: "", linkedOnly: true });
  const [draft, setDraft] = useState("");
  const board = useWireBoard(filters);
  const set = (patch: Partial<BoardFilters>) => setFilters((f) => ({ ...f, ...patch }));
  const counts = board.data?.counts;

  return (
    <Panel title="Intelligence board" icon="view_column" className={className} updatedAt={board.updatedAt}
           subtitle="the item · its source · the measured match and market impact · one click to act"
           actions={<Button size="sm" icon="refresh" busy={board.loading && board.data !== undefined} onClick={() => void board.refresh()}>Refresh</Button>}>
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-2.5 rounded-2xl bg-stone-50 p-3 dark:bg-white/[0.03]">
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Sport">
            <span className="mr-1 text-[10px] font-bold uppercase tracking-wider text-stone-400">Sport</span>
            {SPORTS.map((s) => (
              <Chip key={s} active={filters.sport === s} onClick={() => set({ sport: s })} count={s === "ALL" ? counts?.ALL : counts?.by_sport[s] ?? 0}>{s}</Chip>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Intelligence">
            <span className="mr-1 text-[10px] font-bold uppercase tracking-wider text-stone-400">Intel</span>
            {CATEGORIES.map((c) => (
              <Chip key={c} active={filters.category === c} onClick={() => set({ category: c })} count={c === "ALL" ? undefined : counts?.by_category[c] ?? 0}>
                {c === "ALL" ? "ALL" : CATEGORY_LABEL[c].label.toUpperCase()}
              </Chip>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <form className="relative min-w-0 flex-1 sm:max-w-sm" onSubmit={(e) => { e.preventDefault(); set({ q: draft }); }}>
              <span className="material-symbols-outlined pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[16px] text-stone-400" aria-hidden="true">search</span>
              <input type="search" value={draft} onChange={(e) => { setDraft(e.target.value); if (!e.target.value) set({ q: "" }); }} maxLength={80}
                     placeholder="Search a fixture, team, player or source" aria-label="Search the board"
                     className="h-9 w-full rounded-xl bg-white pl-8 pr-3 text-xs text-stone-800 outline-none ring-1 ring-stone-200 focus:ring-2 focus:ring-[var(--accent)] dark:bg-stone-900 dark:text-stone-100 dark:ring-white/10" />
            </form>
            <label className="flex cursor-pointer items-center gap-2 text-xs text-stone-600 dark:text-stone-300">
              <input type="checkbox" checked={filters.linkedOnly} onChange={(e) => set({ linkedOnly: e.target.checked })} className="size-4 accent-[var(--accent)]" />
              On a tracked fixture only
            </label>
          </div>
        </div>

        <div className="hidden grid-cols-12 gap-3 px-4 text-[10px] font-bold uppercase tracking-wider text-stone-400 md:grid">
          <span className="col-span-4">1 · Breaking intelligence</span>
          <span className="col-span-2">2 · Source & freshness</span>
          <span className="col-span-4">3 · Match & market impact</span>
          <span className="col-span-2">4 · Action</span>
        </div>

        <Async resource={board} skeletonRows={5} isEmpty={(b) => b.items.length === 0}
               empty={<EmptyState icon="newspaper" title="Nothing on the board for these filters"
                                  detail={filters.linkedOnly ? "Only items on a fixture the live board tracks are shown: untick the box to see every stored headline." : "The Wire has stored nothing in this window yet."} />}>
          {(b) => (
            <>
              <ul className="flex flex-col gap-2.5">
                {b.items.map((item) => <WireIntelligenceCard key={item.id} item={item} />)}
              </ul>
              <p className="px-1 text-[11px] leading-relaxed text-stone-500 dark:text-stone-400">
                {b.items.length} of {b.total} shown · {b.tracked_fixtures} fixtures tracked · board read {ago(b.as_of)}
                {b.market_as_of ? ` · prices read at ${formatTime(b.market_as_of)}` : " · the live market is unreadable right now"}.
                Stories from the last {b.policy.hours} hours; steam over {b.policy.steam_window_minutes} minutes of odds ticks; a forecast counts when Π moves
                {" "}{b.policy.weather_min_effect.toFixed(2)} from 1. Tiers: official bodies, then the press at {Math.round(100 * b.policy.press_min_credibility)}% credibility or more.
              </p>
            </>
          )}
        </Async>
      </div>
    </Panel>
  );
};
