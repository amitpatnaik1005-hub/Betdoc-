/**
 * The match board's toolbar (Group 103, point b.11 screenshot 1): the sport carousel (Cricket and Football first, live
 * counts), the time window, the market column the rows show, and the bookmaker skin the board prices at.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import type { ReactNode } from "react";
import { BOOKMAKER_THEMES, type Skin } from "../../../lib/backtest_and_parlay";
import type { BoardWindow, MarketColumn, MatchBoard } from "../../../lib/match_board";

const SPORT_ICON: Record<string, string> = {
  Cricket: "sports_cricket", Football: "sports_soccer", Tennis: "sports_tennis", Basketball: "sports_basketball", "Ice Hockey": "sports_hockey",
  Baseball: "sports_baseball", "American Football": "sports_football", "MMA/UFC": "sports_mma", Esports: "sports_esports",
};

const WINDOWS: { id: BoardWindow; label: string }[] = [
  { id: "live", label: "Live" }, { id: "3h", label: "3H" }, { id: "12h", label: "12H" }, { id: "upcoming", label: "All upcoming" }, { id: "all", label: "All" },
];
const COLUMNS: { id: MarketColumn; label: string }[] = [{ id: "winner", label: "Winner" }, { id: "total", label: "Total" }, { id: "handicap", label: "Handicap" }];

interface Props {
  sports: MatchBoard["sports"];
  total: number;
  sport: string | null;
  onSport: (sport: string | null) => void;
  window: BoardWindow;
  onWindow: (w: BoardWindow) => void;
  column: MarketColumn;
  onColumn: (c: MarketColumn) => void;
  skin: Skin;
  onSkin: (s: Skin) => void;
}

const Group = ({ label, children }: { label: string; children: ReactNode }) => (
  <div role="radiogroup" aria-label={label} className="flex items-center gap-1 rounded-xl p-1" style={{ background: "var(--wb-card)" }}>
    <span className="px-1.5 text-[10px] font-semibold uppercase tracking-wide" style={{ color: "var(--wb-muted)" }}>{label}</span>
    {children}
  </div>
);

const Choice = ({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) => (
  <button type="button" role="radio" aria-checked={active} onClick={onClick}
          className="rounded-lg px-2.5 py-1 text-xs font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--wb-primary)]"
          style={active ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)" } : { color: "var(--wb-muted)" }}>
    {children}
  </button>
);

export const MatchBoardToolbar = ({ sports, total, sport, onSport, window, onWindow, column, onColumn, skin, onSkin }: Props) => (
  <div className="flex flex-col gap-3">
    <div className="flex gap-1.5 overflow-x-auto pb-1" role="tablist" aria-label="Sports">
      {/* Group 131: a sport with no fixture priced has no tab (the one selected keeps its own) */}
      {[{ sport: null as string | null, count: total, live: sports.reduce((a, s) => a + s.live, 0), priority: false }, ...sports.filter((s) => s.count > 0 || s.sport === sport)].map((s) => {
        const active = sport === s.sport;
        return (
          <button key={s.sport ?? "all"} type="button" role="tab" aria-selected={active} onClick={() => onSport(s.sport)}
                  className="inline-flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--wb-primary)]"
                  style={active ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)" } : { background: "var(--wb-card)" }}>
            <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{s.sport ? SPORT_ICON[s.sport] ?? "sports" : "apps"}</span>
            {s.sport ?? "All"}
            <span className="tabular-nums opacity-70">{s.count}</span>
            {s.live > 0 && <span className="rounded-full bg-rose-500 px-1.5 text-[9px] font-bold leading-4 text-white">LIVE {s.live}</span>}
          </button>
        );
      })}
    </div>
    <div className="flex flex-wrap items-center gap-2">
      <Group label="When">{WINDOWS.map((w) => <Choice key={w.id} active={window === w.id} onClick={() => onWindow(w.id)}>{w.label}</Choice>)}</Group>
      <Group label="Market">{COLUMNS.map((c) => <Choice key={c.id} active={column === c.id} onClick={() => onColumn(c.id)}>{c.label}</Choice>)}</Group>
      <Group label="Price at">
        {(Object.keys(BOOKMAKER_THEMES) as Skin[]).map((key) => (
          <Choice key={key} active={skin === key} onClick={() => onSkin(key)}>{BOOKMAKER_THEMES[key].label}</Choice>
        ))}
      </Group>
    </div>
  </div>
);
