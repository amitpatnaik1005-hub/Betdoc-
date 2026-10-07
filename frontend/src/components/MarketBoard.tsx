import { memo, useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { useAuthStore } from "../store/useAuthStore";
import { type Selection, useExecutionStore } from "../store/useExecutionStore";
import { type MarketTick, useMarketStore } from "../store/useMarketStore";

const COLUMNS: readonly Selection[] = ["HOME", "DRAW", "AWAY"];
const FLASH_MS = 500;

const CELL_BASE =
  "border w-16 text-center py-1 rounded transition-colors duration-300 font-mono tabular-nums";
const CELL_DEFAULT =
  "bg-gray-800 border-gray-700 hover:border-emerald-500 hover:bg-gray-700 cursor-pointer";
const CELL_UP = "bg-emerald-600 border-emerald-500 text-white cursor-pointer";
const CELL_DOWN = "bg-red-600 border-red-500 text-white cursor-pointer";
const CELL_SELECTED_RING = "ring-1 ring-emerald-400";

function LockIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" className="w-3.5 h-3.5 mx-auto" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M10 1a4.5 4.5 0 00-4.5 4.5V9H5a2 2 0 00-2 2v6a2 2 0 002 2h10a2 2 0 002-2v-6a2 2 0 00-2-2h-.5V5.5A4.5 4.5 0 0010 1zm3 8V5.5a3 3 0 10-6 0V9h6z"
        clipRule="evenodd"
      />
    </svg>
  );
}

// --------------------------------------------------------------------------- //
// OddsCell: re-renders only when its own tick object (or its selected flag) changes
// --------------------------------------------------------------------------- //
interface OddsCellProps {
  matchId: string;
  selection: Selection;
  tick: MarketTick | undefined;
}

const OddsCell = memo(function OddsCell({ matchId, selection, tick }: OddsCellProps): JSX.Element {
  const setDraft = useExecutionStore((s) => s.setDraft);
  // Boolean selector: flips only for the two cells whose selection state changes.
  const isSelected = useExecutionStore(
    (s) => s.draftMatchId === matchId && s.draftSelection === selection,
  );

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
      <td className="px-2 py-1.5">
        <div className={`${CELL_BASE} bg-gray-900 border-gray-800 text-gray-600`}>-</div>
      </td>
    );
  }

  if (tick.isSuspended) {
    return (
      <td className="px-2 py-1.5">
        <button
          type="button"
          disabled
          aria-label={`${selection} suspended`}
          className={`${CELL_BASE} bg-gray-700 border-gray-600 text-gray-400 cursor-not-allowed`}
        >
          <LockIcon />
        </button>
      </td>
    );
  }

  const stateClass: string =
    flash === "up" ? CELL_UP : flash === "down" ? CELL_DOWN : CELL_DEFAULT;

  return (
    <td className="px-2 py-1.5">
      <button
        type="button"
        onClick={() => setDraft(matchId, selection, tick.odds)}
        title={`True prob: ${(tick.trueProbability * 100).toFixed(1)}%`}
        className={`${CELL_BASE} ${stateClass} ${isSelected ? CELL_SELECTED_RING : ""}`}
      >
        {tick.odds.toFixed(2)}
      </button>
    </td>
  );
});

// --------------------------------------------------------------------------- //
// MatchRow: subscribes ONLY to its own match object
// --------------------------------------------------------------------------- //
interface MatchRowProps {
  matchId: string;
}

const MatchRow = memo(function MatchRow({ matchId }: MatchRowProps): JSX.Element | null {
  const match = useMarketStore((s) => s.matches[matchId]);
  if (!match) return null;

  return (
    <tr className="border-b border-gray-800 hover:bg-gray-800/40">
      <td className="px-3 py-1.5">
        <div className="font-medium text-gray-100 truncate">
          {match.homeTeam} <span className="text-gray-500">v</span> {match.awayTeam}
        </div>
        <div className="text-[10px] font-mono text-gray-500 truncate">{match.matchId}</div>
      </td>
      {COLUMNS.map((sel) => (
        <OddsCell key={sel} matchId={matchId} selection={sel} tick={match.selections[sel]} />
      ))}
    </tr>
  );
});

// --------------------------------------------------------------------------- //
// MarketBoard: subscribes ONLY to the sorted key list (shallow-compared)
// --------------------------------------------------------------------------- //
export default function MarketBoard(): JSX.Element {
  const token = useAuthStore((s) => s.token);
  const connect = useMarketStore((s) => s.connect);
  const disconnect = useMarketStore((s) => s.disconnect);
  const isConnected = useMarketStore((s) => s.isConnected);
  const isReconnecting = useMarketStore((s) => s.isReconnecting);
  const connectionError = useMarketStore((s) => s.connectionError);

  const matchIds: string[] = useMarketStore(useShallow((s) => Object.keys(s.matches).sort()));

  useEffect(() => {
    if (!token) return;
    connect(token);
    return () => disconnect();
  }, [token, connect, disconnect]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold tracking-widest uppercase text-gray-300">Market Board</h2>
        <div className="flex items-center gap-2 text-xs font-semibold">
          {isConnected && (
            <span className="flex items-center gap-1.5 text-emerald-400">
              <span className="w-2 h-2 rounded-full bg-emerald-400" /> LIVE
            </span>
          )}
          {isReconnecting && (
            <span className="px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-400 animate-pulse">
              RECONNECTING...
            </span>
          )}
          {connectionError && (
            <span className="px-2 py-0.5 rounded bg-red-500/20 text-red-400">{connectionError}</span>
          )}
        </div>
      </div>

      {/* Stale prices are dimmed and unclickable while the feed is down. */}
      <div
        className={`rounded border border-gray-800 overflow-hidden transition-opacity ${
          isConnected ? "" : "opacity-50 pointer-events-none"
        }`}
      >
        <table className="w-full text-left text-sm text-gray-300">
          <thead className="bg-gray-950 text-[11px] uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2 font-medium">Match</th>
              {COLUMNS.map((c) => (
                <th key={c} className="px-2 py-2 font-medium text-center w-20">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {matchIds.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS.length + 1} className="px-3 py-8 text-center text-gray-500">
                  {isConnected ? "Awaiting market data..." : "Connecting to live feed..."}
                </td>
              </tr>
            ) : (
              matchIds.map((id) => <MatchRow key={id} matchId={id} />)
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
