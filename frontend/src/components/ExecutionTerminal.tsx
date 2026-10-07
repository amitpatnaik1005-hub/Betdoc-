import { useEffect, useRef, type JSX } from "react";
import { Link } from "react-router-dom";
import { SELECTIONS, useExecutionStore } from "../store/useExecutionStore";
import { useMarketStore } from "../store/useMarketStore";
import { useSystemStore } from "../store/useSystemStore";
import { useControls, useDashboardSummary, useExchanges } from "../lib/api";
import { formatINR, formatPct, formatRatioPct } from "../lib/format";
import { Button, Field, NumberInput, Pill, Select, TextInput } from "../ui/kit";

const QUICK_STAKES: readonly number[] = [100, 500, 1000, 5000];

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
  const draftTrueProbability = useExecutionStore((s) => s.draftTrueProbability);
  const draftLabel = useExecutionStore((s) => s.draftLabel);
  const draftSource = useExecutionStore((s) => s.draftSource);
  const updateDraftField = useExecutionStore((s) => s.updateDraftField);

  const isExecuting = useExecutionStore((s) => s.isExecuting);
  const lastError = useExecutionStore((s) => s.lastError);
  const lastSuccess = useExecutionStore((s) => s.lastSuccess);
  const placeBet = useExecutionStore((s) => s.placeBet);

  const halted = useSystemStore((s) => s.halted);
  const controls = useControls();
  const summary = useDashboardSummary();
  const exchanges = useExchanges();
  const activeAccounts = (exchanges.data ?? []).filter((a) => a.is_active);

  // Re-renders only when THIS selection's tick object changes.
  const liveTick = useMarketStore((s) => s.matches[draftMatchId]?.selections[draftSelection]);
  const isFeedConnected = useMarketStore((s) => s.isConnected);
  const inFlightRef = useRef<boolean>(false);

  // Default to the first linked account; drop a stale choice if the account was deactivated.
  useEffect(() => {
    if (activeAccounts.length === 0) return;
    if (!activeAccounts.some((a) => a.exchange_name === draftExchangeName)) {
      updateDraftField("draftExchangeName", activeAccounts[0].exchange_name);
    }
  }, [activeAccounts, draftExchangeName, updateDraftField]);

  const parsedStake: number = parseFinite(draftStake);
  const parsedOdds: number = parseFinite(draftOdds);
  const maxBet = controls.data?.max_bet_size;
  const priceShifted: boolean = liveTick !== undefined && !Number.isNaN(parsedOdds) && !sameAt4dp(parsedOdds, liveTick.odds);
  const overCap = maxBet !== undefined && parsedStake > maxBet;

  // Edge maths from the probability handed over with the bet (Oracle model or feed consensus).
  const p = draftTrueProbability ?? liveTick?.trueProbability ?? null;
  const implied = parsedOdds > 1 ? 1 / parsedOdds : null;
  const edge = p !== null && implied !== null ? p - implied : null;
  const ev = p !== null && parsedOdds > 1 ? p * parsedOdds - 1 : null;
  const fullKelly = p !== null && parsedOdds > 1 ? (p * (parsedOdds - 1) - (1 - p)) / (parsedOdds - 1) : null;
  const kellyFraction = controls.data?.default_kelly_fraction ?? 0.25;
  const bankroll = summary.data?.total_bankroll ?? 0;
  const kellyStake =
    fullKelly !== null && fullKelly > 0 && bankroll > 0 ? Math.min(fullKelly * kellyFraction * bankroll, maxBet ?? Number.POSITIVE_INFINITY) : null;

  const isValid: boolean =
    !halted &&
    draftMatchId.trim().length > 0 &&
    draftExchangeName.trim().length > 0 &&
    parsedOdds > 1 &&
    parsedStake > 0 &&
    !overCap;

  const handleExecute = async (): Promise<void> => {
    if (inFlightRef.current || isExecuting || !isValid) return;
    inFlightRef.current = true;
    try {
      await placeBet({
        idempotency_key: useExecutionStore.getState().draftIdempotencyKey,
        exchange_name: draftExchangeName.trim(),
        match_id: draftMatchId.trim(),
        market_type: "Match Odds",
        selection: draftSelection,
        currency: "INR",
        odds: parsedOdds,
        stake: parsedStake,
        true_probability: p ?? 1 / parsedOdds,
      });
    } finally {
      inFlightRef.current = false;
    }
  };

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-50">Execution terminal</h2>
          <p className="text-[11px] text-slate-500 dark:text-slate-400">Risk mandate, stop-loss and Control Panel limits apply before routing.</p>
        </div>
        <Pill tone="info" icon="science">Paper exchange</Pill>
      </div>

      {halted && (
        <p role="alert" className="rounded-xl bg-rose-50 px-3 py-2 text-xs font-medium text-rose-700 ring-1 ring-inset ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-300">
          Emergency stop is active. Orders are refused until trading resumes.
        </p>
      )}

      {draftLabel && (
        <div className="rounded-xl bg-slate-50 px-3 py-2 dark:bg-white/[0.04]">
          <p className="truncate text-xs font-semibold text-slate-800 dark:text-slate-100">{draftLabel}</p>
          {draftSource && <p className="text-[10px] uppercase tracking-wider text-slate-400">from {draftSource}</p>}
        </div>
      )}

      <Field label="Match ID">
        <TextInput value={draftMatchId} onChange={(e) => updateDraftField("draftMatchId", e.target.value)} placeholder="Click a price anywhere, or type an ID" />
      </Field>

      <div className="grid grid-cols-3 gap-1.5" role="radiogroup" aria-label="Selection">
        {SELECTIONS.map((s) => (
          <button
            key={s}
            type="button"
            role="radio"
            aria-checked={draftSelection === s}
            onClick={() => updateDraftField("draftSelection", s)}
            className={`rounded-xl py-2 text-xs font-bold tracking-wider ring-1 ring-inset transition-colors ${
              draftSelection === s
                ? "bg-[var(--accent)] text-white ring-transparent"
                : "bg-white text-slate-600 ring-slate-900/10 hover:bg-slate-50 dark:bg-white/[0.04] dark:text-slate-300 dark:ring-white/10"
            }`}
          >
            {s}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <Field label="Odds">
          <NumberInput min="1.01" value={draftOdds} onChange={(e) => updateDraftField("draftOdds", e.target.value)} placeholder="2.10" />
        </Field>
        <Field label="Stake (₹)" hint={maxBet !== undefined ? `Max bet ${formatINR(maxBet)}` : undefined}>
          <NumberInput min="0" value={draftStake} onChange={(e) => updateDraftField("draftStake", e.target.value)} placeholder="100" aria-invalid={overCap} />
        </Field>
      </div>

      <div className="grid grid-cols-4 gap-1.5">
        {QUICK_STAKES.map((amount) => {
          const disabled = maxBet !== undefined && amount > maxBet;
          return (
            <button
              key={amount}
              type="button"
              disabled={disabled}
              title={disabled ? `Above the ${formatINR(maxBet)} max bet` : undefined}
              onClick={() => updateDraftField("draftStake", String(amount))}
              className="rounded-lg py-1.5 font-mono text-xs text-slate-600 ring-1 ring-inset ring-slate-900/10 transition-colors hover:ring-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-40 dark:text-slate-300 dark:ring-white/10"
            >
              ₹{amount.toLocaleString("en-IN")}
            </button>
          );
        })}
      </div>

      {(edge !== null || liveTick || (draftMatchId && !isFeedConnected)) && (
        <div className="space-y-1.5 rounded-xl bg-slate-50 px-3 py-2.5 text-xs dark:bg-white/[0.04]">
          {edge !== null && implied !== null && p !== null && (
            <>
              <div className="flex justify-between text-slate-500 dark:text-slate-400">
                <span>Model {formatRatioPct(p)} vs implied {formatRatioPct(implied)}</span>
                <span className={`font-semibold tabular-nums ${edge > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
                  {edge > 0 ? "+" : ""}
                  {formatPct(edge * 100, 2)} edge
                </span>
              </div>
              {ev !== null && (
                <div className="flex justify-between text-slate-500 dark:text-slate-400">
                  <span>EV per ₹1 · Kelly ×{kellyFraction}</span>
                  <span className="tabular-nums text-slate-700 dark:text-slate-200">
                    {ev >= 0 ? "+" : ""}
                    {ev.toFixed(3)}
                    {kellyStake !== null && (
                      <button
                        type="button"
                        onClick={() => updateDraftField("draftStake", String(Math.max(1, Math.floor(kellyStake))))}
                        className="ml-2 rounded-md bg-[var(--accent)] px-1.5 py-0.5 text-[10px] font-bold text-white"
                        title="Use the fractional-Kelly stake"
                      >
                        {formatINR(Math.floor(kellyStake))}
                      </button>
                    )}
                  </span>
                </div>
              )}
            </>
          )}
          {priceShifted && liveTick && (
            <div className="flex items-center justify-between gap-2 text-amber-700 dark:text-amber-300">
              <span>Live price moved to {liveTick.odds.toFixed(2)}</span>
              <button type="button" onClick={() => updateDraftField("draftOdds", String(liveTick.odds))} className="rounded-md px-2 py-0.5 font-semibold ring-1 ring-inset ring-amber-500/50 hover:bg-amber-500/10">
                Use live
              </button>
            </div>
          )}
          {liveTick?.isSuspended && <p className="text-amber-700 dark:text-amber-300">Market suspended on the feed.</p>}
          {draftMatchId && !isFeedConnected && <p className="text-slate-500 dark:text-slate-400">Live feed offline: price not cross-checked.</p>}
        </div>
      )}

      <Field label="Exchange account">
        {activeAccounts.length > 0 ? (
          <Select value={draftExchangeName} onChange={(e) => updateDraftField("draftExchangeName", e.target.value)}>
            {activeAccounts.map((a) => (
              <option key={a.id} value={a.exchange_name}>{a.exchange_name}</option>
            ))}
          </Select>
        ) : (
          <p className="rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-800 ring-1 ring-inset ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-200">
            {exchanges.data ? "No exchange account linked. " : "Loading accounts… "}
            <Link to="/control-panel" className="font-semibold underline">Link one in the Control Panel</Link>
          </p>
        )}
      </Field>

      {/* Deliberately NOT a form submit: Enter must never fire an order. */}
      <Button variant="primary" onClick={handleExecute} disabled={!isValid} busy={isExecuting} className="w-full py-3.5 text-base tracking-widest" icon="bolt">
        {isExecuting ? "Routing to exchange…" : "Execute"}
      </Button>

      <div className="min-h-[1.25rem] space-y-1" aria-live="polite">
        {overCap && <p className="text-xs text-rose-600 dark:text-rose-400">Stake is above the Control Panel max bet.</p>}
        {lastError && <p className="break-words text-xs text-rose-600 dark:text-rose-400">{lastError}</p>}
        {lastSuccess && <p className="break-words text-xs text-emerald-600 dark:text-emerald-400">{lastSuccess}</p>}
      </div>
    </div>
  );
}
