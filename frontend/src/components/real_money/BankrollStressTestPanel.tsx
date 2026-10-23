/**
 * The Monte Carlo stress test (Group 116) beside TODAR MAL's vault: many paths of bets staked as the desk stakes
 * (fractional Kelly under the 5% ceiling) and guarded as SHIVAJI guards (the 15% daily latch, the cool-down after three
 * straight losses). The inputs are the desk's own won and lost tickets when there are enough of them, else the win rate
 * and average odds the user states; the answer says which, and is only as true as they are. Developed for Amit Ashok
 * Kumar Patnaik.
 */
import { useState } from "react";
import { ApiError } from "../../api/client";
import { runStressTest, type StressTestResult } from "../../lib/real_money";
import { inr } from "../../lib/slip_ornament";
import { Button, Field, NumberInput, Panel, Pill, Segmented } from "../../ui/kit";

type Mode = "measured" | "assumed";
const MODES: { value: Mode; label: string }[] = [{ value: "measured", label: "My record" }, { value: "assumed", label: "Assumptions" }];
const pct = (p: number, digits = 1) => `${(p * 100).toFixed(digits)}%`;

const Figure = ({ label, value, hint }: { label: string; value: string; hint?: string }) => (
  <div className="flex min-w-0 flex-col rounded-xl border border-stone-200/70 p-3 dark:border-white/[0.08]">
    <span className="text-[10px] font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">{label}</span>
    <span className="mt-0.5 truncate font-mono text-base font-black">{value}</span>
    {hint && <span className="truncate text-[10px] text-stone-500 dark:text-stone-400">{hint}</span>}
  </div>
);

export const BankrollStressTestPanel = () => {
  const [mode, setMode] = useState<Mode>("measured");
  const [winPct, setWinPct] = useState("");
  const [odds, setOdds] = useState("");
  const [kelly, setKelly] = useState("0.25");
  const [bets, setBets] = useState("100");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<StressTestResult | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const win = Number(winPct) / 100;
  const odd = Number(odds);
  const kellyN = Number(kelly);
  const betsN = Math.round(Number(bets));
  const assumedOk = mode === "measured" || (win > 0 && win < 1 && odd > 1);
  const ready = assumedOk && kellyN > 0 && kellyN <= 1 && betsN >= 1 && betsN <= 1000 && !busy;

  const run = async () => {
    if (!ready) return;
    setBusy(true);
    setProblem(null);
    try {
      setResult(await runStressTest({ kelly_fraction: kellyN, bets: betsN, ...(mode === "assumed" ? { win_probability: win, average_odds: odd } : {}) }));
    } catch (err) {
      setResult(null);
      setProblem(err instanceof ApiError ? err.message : "No answer from the server");
      if (err instanceof ApiError && err.detail?.code === "INSUFFICIENT_HISTORY") setMode("assumed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="Bankroll stress test" icon="query_stats" className="lg:col-span-12" subtitle="Monte Carlo under SHIVAJI's rules"
           actions={<Segmented label="Stress test inputs" size="sm" value={mode} onChange={setMode} options={MODES} />}>
      <div className="flex flex-col gap-4">
        <p className="text-xs text-stone-500 dark:text-stone-400">
          10,000 paths of bets from your vault's balance, each staked at {kelly}× Kelly under the 5% ceiling, sitting out the rest of a day after a 15% loss
          and the next bet after three straight losses. {mode === "measured"
            ? "The win rate and average odds are your own won and lost tickets (30 needed)."
            : "The win rate and average odds below are your assumptions: the result is only as true as they are."}
        </p>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {mode === "assumed" && (
            <>
              <Field label="Win rate (%)"><NumberInput value={winPct} min={1} max={99} step="0.1" placeholder="e.g. 55" onChange={(e) => setWinPct(e.target.value)} /></Field>
              <Field label="Average odds"><NumberInput value={odds} min={1.01} step="0.01" placeholder="e.g. 1.90" onChange={(e) => setOdds(e.target.value)} /></Field>
            </>
          )}
          <Field label="Kelly fraction"><NumberInput value={kelly} min={0.05} max={1} step="0.05" onChange={(e) => setKelly(e.target.value)} /></Field>
          <Field label="Bets per path"><NumberInput value={bets} min={1} max={1000} onChange={(e) => setBets(e.target.value)} /></Field>
          <div className="flex items-end"><Button variant="primary" icon="play_arrow" busy={busy} disabled={!ready} onClick={() => void run()} className="w-full">Run</Button></div>
        </div>
        {problem && <p className="text-xs font-semibold text-rose-600" role="alert">{problem}</p>}
        {result && (
          <div className="flex flex-col gap-3" aria-live="polite">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Pill tone={result.inputs_source === "MEASURED" ? "good" : "warning"}>{result.inputs_source === "MEASURED" ? `measured from ${result.measured.settled} tickets` : "assumed inputs"}</Pill>
              <span className="text-stone-500 dark:text-stone-400">
                win {pct(result.win_probability)} at {result.average_odds.toFixed(2)} · edge {result.edge_per_unit >= 0 ? "+" : ""}{pct(result.edge_per_unit)} per rupee ·
                stake {pct(result.stake_fraction, 2)} of the balance · {result.iterations.toLocaleString("en-IN")} paths from {inr(result.starting_bankroll)}
              </span>
            </div>
            {result.note && <p className="text-xs font-semibold text-amber-600">{result.note}</p>}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Figure label="Ruin" value={pct(result.ruin_probability, 2)} hint={result.ruin_shield_passed ? "under 1% of paths" : "1% or more of paths"} />
              <Figure label="Median outcome" value={inr(result.median_final_bankroll)} hint={`5%–95%: ${inr(result.p5_final_bankroll)} – ${inr(result.p95_final_bankroll)}`} />
              <Figure label="Ends in profit" value={pct(result.probability_of_profit)} hint={`mean ${inr(result.mean_final_bankroll)}`} />
              <Figure label="Worst drawdown (95th)" value={`${result.p95_max_drawdown_pct.toFixed(1)}%`} hint={`median ${result.median_max_drawdown_pct.toFixed(1)}%`} />
            </div>
            <p className="text-[11px] text-stone-500 dark:text-stone-400">
              SHIVAJI latched {result.stop_loss_activations.toLocaleString("en-IN")} trading days and called {result.cooldowns.toLocaleString("en-IN")} cool-downs across
              the paths; {result.bets_placed.toLocaleString("en-IN")} bets were placed in all.
            </p>
          </div>
        )}
      </div>
    </Panel>
  );
};
