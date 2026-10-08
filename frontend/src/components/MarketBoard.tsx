import { memo, useEffect, useRef, useState, type JSX } from "react";
import { useShallow } from "zustand/react/shallow";
import { type Selection, useExecutionStore } from "../store/useExecutionStore";
import { type MarketTick, useMarketStore } from "../store/useMarketStore";
import { EmptyState, LiveDot, Pill } from "../ui/kit";

const COLUMNS: readonly Selection[] = ["HOME", "DRAW", "AWAY"];
const FLASH_MS = 600;

const CELL_BASE =
  "w-[4.5rem] rounded-xl py-1.5 text-center font-mono text-sm font-semibold tabular-nums ring-1 ring-inset transition-colors duration-300";
const CELL_DEFAULT =
  "cursor-pointer bg-white text-stone-800 ring-stone-900/10 hover:ring-[var(--accent)] dark:bg-white/[0.04] dark:text-stone-100 dark:ring-white/10";
// Price moves use the validated diverging pair plus an arrow glyph, so direction is never colour-alone.
const CELL_UP = "cursor-pointer bg-[var(--viz-positive)] text-white ring-transparent";
const CELL_DOWN = "cursor-pointer bg-[var(--viz-negative)] text-white ring-transparent";

// --------------------------------------------------------------------------- //
// OddsCell: re-renders only when its own tick object (or its selected flag) changes
// --------------------------------------------------------------------------- //
interface OddsCellProps {
  matchId: string;
  label: string;
  selection: Selection;
  tick: MarketTick | undefined;
}

const OddsCell = memo(function OddsCell({ matchId, label, selection, tick }: OddsCellProps): JSX.Element {
  const setDraft = useExecutionStore((s) => s.setDraft);
  // Boolean selector: flips only for the two cells whose selection state changes.
  const isSelected = useExecutionStore((s) => s.draftMatchId === matchId && s.draftSelection === selection);

  const previousOdds = useRef<number | undefined>(tick?.odds);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  const odds: number | undefined = tick?.odds;

  useEffect(() => {
    if (odds === undefined) {
      setFlash(null);
      return;
    }
    const prev: number | undefined = previousOdds.current;
    previousOdds.current = odds; // only updated when a tick exists
    if (prev === undefined || prev === odds) return;
    setFlash(odds > prev ? "up" : "down");
    const timer: number = window.setTimeout(() => setFlash(null), FLASH_MS);
    // Strict cleanup: clears on the next price change AND on unmount.
    return () => window.clearTimeout(timer);
  }, [odds]);

  if (!tick) {
    return (
      <td className="px-2 py-2 text-center">
        <div className={`${CELL_BASE} mx-auto bg-stone-50 text-stone-300 ring-stone-900/5 dark:bg-white/[0.02] dark:text-stone-600 dark:ring-white/5`}>—</div>
      </td>
    );
  }

  if (tick.isSuspended) {
    return (
      <td className="px-2 py-2 text-center">
        <button type="button" disabled aria-label={`${selection} suspended`} className={`${CELL_BASE} mx-auto cursor-not-allowed bg-stone-100 text-stone-400 ring-stone-900/10 dark:bg-white/[0.06] dark:text-stone-500`}>
          <span className="material-symbols-outlined text-[14px]">lock</span>
        </button>
      </td>
    );
  }

  const stateClass: string = flash === "up" ? CELL_UP : flash === "down" ? CELL_DOWN : CELL_DEFAULT;

  return (
    <td className="px-2 py-2 text-center">
      <button
        type="button"
        onClick={() =>
          setDraft({ matchId, selection, odds: tick.odds, trueProbability: tick.trueProbability, label, source: "Live feed" })
        }
        title={`Fair probability ${(tick.trueProbability * 100).toFixed(1)}% · click to load the betslip`}
        className={`${CELL_BASE} mx-auto ${stateClass} ${isSelected ? "ring-2 ring-[var(--accent)]" : ""}`}
      >
        {flash === "up" ? "▲ " : flash === "down" ? "▼ " : ""}
        {tick.odds.toFixed(2)}
      </button>
    </td>
  );
});

// --------------------------------------------------------------------------- //
// MatchRow: subscribes ONLY to its own match object
// --------------------------------------------------------------------------- //
const MatchRow = memo(function MatchRow({ matchId }: { matchId: string }): JSX.Element | null {
  const match = useMarketStore((s) => s.matches[matchId]);
  if (!match) return null;
  const label = `${match.homeTeam} v ${match.awayTeam}`;

  return (
    <tr className="border-b border-stone-900/[0.05] last:border-0 dark:border-white/[0.05]">
      <td className="px-4 py-2">
        <div className="truncate font-medium text-stone-800 dark:text-stone-100">
          {match.homeTeam} <span className="text-stone-400">v</span> {match.awayTeam}
        </div>
        <div className="truncate font-mono text-[10px] text-stone-400 dark:text-stone-500">{match.matchId}</div>
      </td>
      {COLUMNS.map((sel) => (
        <OddsCell key={sel} matchId={matchId} label={label} selection={sel} tick={match.selections[sel]} />
      ))}
    </tr>
  );
});

// --------------------------------------------------------------------------- //
// MarketBoard: subscribes ONLY to the sorted key list (shallow-compared).
// The socket itself is owned by the shell (services/realtime), so it stays live across sections.
// --------------------------------------------------------------------------- //
export default function MarketBoard(): JSX.Element {
  const isConnected = useMarketStore((s) => s.isConnected);
  const isReconnecting = useMarketStore((s) => s.isReconnecting);
  const connectionError = useMarketStore((s) => s.connectionError);
  const matchIds: string[] = useMarketStore(useShallow((s) => Object.keys(s.matches).sort()));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="material-symbols-outlined text-[18px] text-accent">candlestick_chart</span>
          <h2 className="text-[11px] font-semibold text-stone-700 dark:text-stone-200">Live market board</h2>
        </div>
        <div className="flex items-center gap-2">
          {isConnected && (
            <Pill tone="good">
              <LiveDot active /> Streaming
            </Pill>
          )}
          {isReconnecting && <Pill tone="warning" icon="sync">Reconnecting</Pill>}
          {connectionError && <Pill tone="critical" icon="error">{connectionError}</Pill>}
          {!isConnected && !isReconnecting && !connectionError && <Pill tone="neutral" icon="cloud_off">Offline</Pill>}
        </div>
      </div>

      {/* Stale prices are dimmed and unclickable while the feed is down. */}
      <div
        className={`overflow-x-auto rounded-2xl bg-white ring-1 ring-inset ring-stone-900/[0.06] transition-opacity dark:bg-[#1c1917] dark:ring-white/[0.08] ${
          isConnected || matchIds.length === 0 ? "" : "pointer-events-none opacity-50"
        }`}
      >
        {matchIds.length === 0 ? (
          <EmptyState
            icon="stream"
            title={isConnected ? "Waiting for the first tick" : "Live tick feed not connected"}
            detail="Prices stream here as the Omni fleet (Control Panel, Fleet Command) ingests Polymarket and The Odds API. Click any price to add it to your bet slip."
          />
        ) : (
          <table className="w-full min-w-[480px] text-left text-sm">
            <thead>
              <tr className="border-b border-stone-900/[0.06] dark:border-white/[0.06]">
                <th className="px-4 py-2.5 text-xs font-semibold text-stone-400">Match</th>
                {COLUMNS.map((c) => (
                  <th key={c} className="w-24 px-2 py-2.5 text-center text-xs font-semibold text-stone-400">{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matchIds.map((id) => (
                <MatchRow key={id} matchId={id} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
