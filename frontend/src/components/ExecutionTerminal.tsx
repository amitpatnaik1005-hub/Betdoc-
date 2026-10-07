import { useRef, type JSX } from "react";
import { SELECTIONS, type Selection, useExecutionStore } from "../store/useExecutionStore";
import { useMarketStore } from "../store/useMarketStore";

const QUICK_STAKES: readonly number[] = [100, 500, 1000, 5000];

const INPUT_CLASS =
  "w-full rounded px-3 py-2 text-sm bg-gray-900 border border-gray-700 text-gray-100 " +
  "placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500";

const LABEL_CLASS = "block text-xs uppercase tracking-wide text-gray-400 mb-1";

function parseFinite(raw: string): number {
  if (raw.trim() === "") return Number.NaN;
  const n: number = parseFloat(raw);
  return Number.isFinite(n) ? n : Number.NaN;
}

/** Compare at the backend's Numeric(16,4) scale to avoid float noise. */
function sameAt4dp(a: number, b: number): boolean {
  return Math.round(a * 10_000) === Math.round(b * 10_000);
}

export default function ExecutionTerminal(): JSX.Element {
  const draftMatchId = useExecutionStore((s) => s.draftMatchId);
  const draftSelection = useExecutionStore((s) => s.draftSelection);
  const draftOdds = useExecutionStore((s) => s.draftOdds);
  const draftStake = useExecutionStore((s) => s.draftStake);
  const draftExchangeName = useExecutionStore((s) => s.draftExchangeName);
  const updateDraftField = useExecutionStore((s) => s.updateDraftField);

  const isExecuting = useExecutionStore((s) => s.isExecuting);
  const lastError = useExecutionStore((s) => s.lastError);
  const lastSuccess = useExecutionStore((s) => s.lastSuccess);
  const placeBet = useExecutionStore((s) => s.placeBet);

  // Re-renders only when THIS selection's tick object changes.
  const liveTick = useMarketStore((s) => s.matches[draftMatchId]?.selections[draftSelection]);
  const isFeedConnected = useMarketStore((s) => s.isConnected);

  const inFlightRef = useRef<boolean>(false);

  const parsed: number = parseFloat(draftOdds);
  const parsedStake: number = parseFinite(draftStake);
  const parsedOdds: number = parseFinite(draftOdds);

  const priceShifted: boolean =
    liveTick !== undefined && !Number.isNaN(parsed) && !sameAt4dp(parsed, liveTick.odds);

  const isValid: boolean =
    draftMatchId.trim().length > 0 &&
    draftExchangeName.trim().length > 0 &&
    !Number.isNaN(parsedOdds) &&
    !Number.isNaN(parsedStake) &&
    parsedOdds > 1 &&
    parsedStake > 0;

  const handleExecute = async (): Promise<void> => {
    if (inFlightRef.current || isExecuting) return;

    const oddsNum: number = parseFloat(draftOdds);
    const stakeNum: number = parseFloat(draftStake);
    if (draftOdds.trim() === "" || draftStake.trim() === "") return;
    if (Number.isNaN(oddsNum) || Number.isNaN(stakeNum)) return;
    if (!isValid) return;

    inFlightRef.current = true;
    try {
      await placeBet({
        match_id: draftMatchId.trim(),
        selection: draftSelection,
        odds: oddsNum,
        stake: stakeNum,
        exchange_name: draftExchangeName.trim(),
        idempotency_key: useExecutionStore.getState().draftIdempotencyKey,
      });
    } finally {
      inFlightRef.current = false;
    }
  };

  return (
    <div className="h-full bg-gray-800 border-l border-gray-700 p-6 space-y-5">
      <div>
        <h2 className="text-sm font-semibold tracking-widest uppercase text-gray-300">
          Execution Terminal
        </h2>
        <p className="text-xs text-gray-500">Routes through the risk engine before exchange submission.</p>
      </div>

      <div>
        <label htmlFor="match_id" className={LABEL_CLASS}>Match ID</label>
        <input
          id="match_id"
          type="text"
          value={draftMatchId}
          onChange={(e) => updateDraftField("draftMatchId", e.target.value)}
          placeholder="Click a price or type an ID"
          className={INPUT_CLASS}
          spellCheck={false}
          autoComplete="off"
        />
      </div>

      <div>
        <label htmlFor="selection" className={LABEL_CLASS}>Selection</label>
        <select
          id="selection"
          value={draftSelection}
          onChange={(e) => updateDraftField("draftSelection", e.target.value as Selection)}
          className={INPUT_CLASS}
        >
          {SELECTIONS.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor="odds" className={LABEL_CLASS}>Odds</label>
          <input
            id="odds"
            type="number"
            min="0"
            step="any"
            inputMode="decimal"
            value={draftOdds}
            onChange={(e) => updateDraftField("draftOdds", e.target.value)}
            placeholder="2.10"
            className={INPUT_CLASS}
          />
        </div>
        <div>
          <label htmlFor="stake" className={LABEL_CLASS}>Stake</label>
          <input
            id="stake"
            type="number"
            min="0"
            step="any"
            inputMode="decimal"
            value={draftStake}
            onChange={(e) => updateDraftField("draftStake", e.target.value)}
            placeholder="100"
            className={INPUT_CLASS}
          />
        </div>
      </div>

      {/* Live price cross-reference */}
      <div className="min-h-[1.25rem] space-y-1 text-xs">
        {priceShifted && liveTick && (
          <div className="flex items-center justify-between gap-2 text-yellow-400">
            <span>Live Price shifted to: {liveTick.odds.toFixed(2)}</span>
            <button
              type="button"
              onClick={() => updateDraftField("draftOdds", String(liveTick.odds))}
              className="rounded px-2 py-0.5 border border-yellow-500/60 hover:bg-yellow-500/10"
            >
              USE LIVE
            </button>
          </div>
        )}
        {liveTick?.isSuspended && <p className="text-yellow-400">Market SUSPENDED on feed.</p>}
        {draftMatchId && !isFeedConnected && (
          <p className="text-yellow-400">Live feed disconnected: price not verified.</p>
        )}
      </div>

      <div className="grid grid-cols-4 gap-2">
        {QUICK_STAKES.map((amount) => (
          <button
            key={amount}
            type="button"
            onClick={() => updateDraftField("draftStake", String(amount))}
            className="rounded py-1.5 text-xs font-mono bg-gray-900 border border-gray-700 text-gray-300 hover:border-emerald-500 hover:text-emerald-400 transition-colors"
          >
            [{amount}]
          </button>
        ))}
      </div>

      <div>
        <label htmlFor="exchange_name" className={LABEL_CLASS}>Exchange</label>
        <input
          id="exchange_name"
          type="text"
          value={draftExchangeName}
          onChange={(e) => updateDraftField("draftExchangeName", e.target.value)}
          className={INPUT_CLASS}
          spellCheck={false}
          autoComplete="off"
        />
      </div>

      {/* Deliberately NOT a form submit: Enter must never fire an order. */}
      <button
        type="button"
        onClick={handleExecute}
        disabled={isExecuting || !isValid}
        className="w-full rounded-lg py-5 text-lg font-bold tracking-widest bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-700 disabled:text-gray-500 disabled:cursor-not-allowed transition-colors"
      >
        {isExecuting ? "ROUTING TO EXCHANGE..." : "EXECUTE"}
      </button>

      <div className="min-h-[1.5rem] space-y-1" aria-live="polite">
        {lastError && <p className="text-sm text-red-400 break-words">{lastError}</p>}
        {lastSuccess && <p className="text-sm text-emerald-400 break-words">{lastSuccess}</p>}
      </div>
    </div>
  );
}
