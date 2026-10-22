/**
 * The match board's tournament accordion (Group 103, point b.11 screenshot 1): countries from the league registry,
 * their leagues with fixture counts, a checkbox per league (none ticked: every league). Developed for Amit Ashok Kumar
 * Patnaik.
 */
import { useState } from "react";
import type { MatchBoard } from "../../../lib/match_board";

interface Props {
  countries: MatchBoard["countries"];
  selected: Set<string>;
  onToggle: (league: string) => void;
  onClear: () => void;
}

export const TournamentFilter = ({ countries, selected, onToggle, onClear }: Props) => {
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const total = countries.reduce((a, c) => a + c.count, 0);
  return (
    <nav aria-label="Tournaments" className="flex flex-col gap-2 rounded-2xl p-3" style={{ background: "var(--wb-card)" }}>
      <div className="flex items-baseline justify-between">
        <h3 className="text-[11px] font-bold uppercase tracking-wider" style={{ color: "var(--wb-muted)" }}>All tournaments</h3>
        {selected.size > 0
          ? <button type="button" onClick={onClear} className="text-[11px] underline">show all ({total})</button>
          : <span className="text-[11px] tabular-nums" style={{ color: "var(--wb-muted)" }}>{total}</span>}
      </div>
      {countries.length === 0 && <p className="text-xs" style={{ color: "var(--wb-muted)" }}>No fixture in this window.</p>}
      <ul className="flex flex-col gap-1">
        {countries.map((c) => {
          const name = c.country ?? "Other leagues";
          const open = !closed.has(name);
          return (
            <li key={name} className="overflow-hidden rounded-xl" style={{ background: "color-mix(in srgb, var(--wb-card) 60%, transparent)" }}>
              <button type="button" aria-expanded={open} onClick={() => setClosed((prev) => { const next = new Set(prev); if (open) next.add(name); else next.delete(name); return next; })}
                      className="flex w-full items-center justify-between px-2.5 py-2 text-xs font-semibold">
                <span title={c.country ? undefined : "Feed keys the league registry does not map to a country"}>{name}</span>
                <span className="flex items-center gap-1 tabular-nums" style={{ color: "var(--wb-muted)" }}>
                  {c.count}
                  <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{open ? "expand_less" : "expand_more"}</span>
                </span>
              </button>
              {open && (
                <ul className="flex flex-col pb-1.5">
                  {c.leagues.map((l) => (
                    <li key={l.league}>
                      <label className="flex cursor-pointer items-center justify-between gap-2 px-2.5 py-1 text-[11px]">
                        <span className="flex min-w-0 items-center gap-2">
                          <input type="checkbox" checked={selected.has(l.league)} onChange={() => onToggle(l.league)} style={{ accentColor: "var(--wb-primary)" }} />
                          <span className="truncate">{l.league}</span>
                        </span>
                        <span className="tabular-nums" style={{ color: "var(--wb-muted)" }}>{l.count}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
};
