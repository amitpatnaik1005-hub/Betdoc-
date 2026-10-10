/**
 * KUMBHA's capital growth on The Vault (Group 76).
 *
 * - The sizing policy in force: your bankroll, rolling drawdown and regime (the damper), the halt latch, the
 *   models' skill multiplier and the most a bet may stake now. Pillar 13 sizes with exactly this policy.
 * - Strategies: every sizing rule forecast over your own settled bets (same seed), with the analytic ruin bound.
 * - Forecast: a 10,000-path Monte Carlo of one strategy over a horizon, as a percentile fan.
 * - Advisories: the regime changes and re-confirmations; a halt is signed off by an administrator with a reason.
 * - Rebalance (administrators): venue balances from the Vault against their EV share, and the transfers.
 * Nothing here is assumed: no history, no forecast.
 */
import { useId, useState } from "react";
import { ApiError } from "../../api/client";
import { formatDateTime, formatINR, formatINRCompact } from "../../lib/format";
import {
  acknowledgeAdvisory, recordRebalance, REGIME, runForecast, setTransferStatus, useGrowthAdvisories, useGrowthPolicy, useRebalance, useSimulations, useStrategyBoard,
  type Advisory, type Simulation, type StrategyRow, type Transfer,
} from "../../lib/cfo_growth";
import { invalidate } from "../../lib/resource";
import { useAuthStore } from "../../store/useAuthStore";
import { toast } from "../../store/useToastStore";
import { LineChart } from "../../ui/charts";
import { Async, Button, DataTable, EmptyState, Field, Meter, Panel, Pill, Segmented, Select, Stat, StatGrid, TextInput } from "../../ui/kit";

type Tab = "strategies" | "forecast" | "advisories" | "rebalance";
const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const pct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);
const ratio = (v: number | null | undefined): string => (v === null || v === undefined ? "—" : v.toFixed(2));
const rupees = (v: string | number | null | undefined): string => (v === null || v === undefined ? "—" : formatINR(Number(v)));
const SEVERITY_TONE = { INFO: "neutral", RECOMMENDATION: "accent", WARNING: "warning", CRITICAL: "critical" } as const;

const STRATEGY_COLUMNS = (active: string) => [
  {
    key: "name",
    header: "Strategy",
    render: (s: StrategyRow) => (
      <span className="flex items-center gap-1.5 text-xs">
        {s.strategy_name}
        {s.strategy_code === active && <Pill tone="good">in force</Pill>}
      </span>
    ),
  },
  { key: "f", header: "Typical bet", align: "right" as const, render: (s: StrategyRow) => <span className="font-mono">{pct(s.effective_fraction, 2)}</span> },
  { key: "stake", header: "Stake now", align: "right" as const, render: (s: StrategyRow) => <span className="font-mono">{rupees(s.recommended_stake_on_next_bet_inr)}</span> },
  { key: "cagr", header: "CAGR (median)", align: "right" as const, render: (s: StrategyRow) => <span className="font-mono">{s.simulated_cagr_pct >= 0 ? "+" : ""}{s.simulated_cagr_pct.toFixed(1)}%</span> },
  { key: "sharpe", header: "Sharpe · Sortino", align: "right" as const, render: (s: StrategyRow) => <span className="font-mono">{ratio(s.sharpe_ratio)} · {ratio(s.sortino_ratio)}</span> },
  { key: "dd", header: "Max DD med · p95", align: "right" as const, render: (s: StrategyRow) => <span className="font-mono">{s.median_max_drawdown_pct.toFixed(1)}% · {s.p95_max_drawdown_pct.toFixed(1)}%</span> },
  { key: "halt", header: "P(halt)", align: "right" as const, render: (s: StrategyRow) => <span className="font-mono">{pct(s.prob_circuit_breaker)}</span> },
  { key: "ruin", header: "P(halving) bound", align: "right" as const, render: (s: StrategyRow) => <span className="font-mono">{s.ruin_bound_halving === null ? "—" : pct(s.ruin_bound_halving, 2)}</span> },
];

const ForecastView = ({ sim }: { sim: Simulation }) => (
  <div className="flex flex-col gap-4">
    <StatGrid cols={5}>
      <Stat label="Median end" value={formatINRCompact(Number(sim.median_ending_bankroll_inr))} icon="trending_up" hint={`from ${formatINRCompact(Number(sim.starting_bankroll_inr))}`} />
      <Stat label="CAGR (median)" value={`${sim.expected_cagr_pct >= 0 ? "+" : ""}${sim.expected_cagr_pct.toFixed(1)}%`} icon="percent" tone={sim.expected_cagr_pct >= 0 ? "positive" : "negative"} />
      <Stat label="Sharpe · Sortino" value={`${ratio(sim.sharpe_ratio)} · ${ratio(sim.sortino_ratio)}`} icon="query_stats" hint="annualised, daily" />
      <Stat label="P(circuit breaker)" value={pct(sim.prob_circuit_breaker)} icon="gpp_maybe" tone={sim.prob_circuit_breaker > 0.1 ? "caution" : "neutral"} hint={`max DD median ${pct(sim.median_max_drawdown)}`} />
      <Stat label="P(ruin)" value={pct(sim.prob_ruin, 2)} icon="warning" tone={sim.prob_ruin > 0 ? "negative" : "neutral"} hint={`under ${pct(sim.parameters.ruin_level ?? 0.5, 0)} of the start`} />
    </StatGrid>
    <LineChart
      caption={`${sim.paths.toLocaleString("en-IN")} paths, ${sim.trades} bets each over ${sim.horizon_days} days: percentiles of the bankroll`}
      labels={sim.percentile_curve.map((p) => `Day ${p.day}`)}
      formatValue={formatINRCompact}
      area={false}
      series={[
        { name: "p95", color: "var(--viz-series-3)", values: sim.percentile_curve.map((p) => p.p95), dashed: true },
        { name: "p75", color: "var(--viz-series-2)", values: sim.percentile_curve.map((p) => p.p75) },
        { name: "Median", color: "var(--viz-series-1)", values: sim.percentile_curve.map((p) => p.p50) },
        { name: "p25", color: "var(--viz-series-2)", values: sim.percentile_curve.map((p) => p.p25) },
        { name: "p5", color: "var(--viz-series-4)", values: sim.percentile_curve.map((p) => p.p5), dashed: true },
      ]}
    />
    <p className="text-[11px] text-stone-400 dark:text-stone-500">
      Bootstrapped from {sim.parameters.history?.bets ?? "—"} of your settled bets (hit rate {pct(sim.parameters.history?.hit_rate)}, realised ROI {pct(sim.parameters.history?.realised_roi, 2)}, {sim.parameters.history?.per_day.toFixed(2)} a day) · seed {sim.seed} ·{" "}
      {formatDateTime(sim.created_at)}
    </p>
  </div>
);

const ForecastTab = ({ horizons, strategies, active }: { horizons: number[]; strategies: { code: string; label: string }[]; active: string }) => {
  const sims = useSimulations();
  const [strategy, setStrategy] = useState(active);
  const [horizon, setHorizon] = useState(horizons.includes(90) ? 90 : horizons[0]);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      await runForecast(strategy, horizon);
      invalidate("growth:simulations");
    } catch (err) {
      toast.error("Forecast refused", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Strategy">
          <Select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.code} value={s.code}>{s.label}</option>
            ))}
          </Select>
        </Field>
        <Field label="Horizon">
          <Select value={String(horizon)} onChange={(e) => setHorizon(Number(e.target.value))}>
            {horizons.map((h) => (
              <option key={h} value={h}>{h} days</option>
            ))}
          </Select>
        </Field>
        <Button variant="primary" icon="casino" busy={busy} onClick={() => void run()}>
          Run the forecast
        </Button>
      </div>
      <Async resource={sims} isEmpty={(rows) => rows.length === 0} empty={<EmptyState icon="casino" title="No forecast yet" detail="Run one: it bootstraps your own settled bets, sized by the strategy you pick and the drawdown damper." />}>
        {(rows) => <ForecastView sim={rows[0]} />}
      </Async>
    </div>
  );
};

const AdvisoryCard = ({ a, isAdmin }: { a: Advisory; isAdmin: boolean }) => {
  const noteId = useId();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const halt = a.insight_code === "CAPITAL_PRESERVATION_HALT";
  const ack = async () => {
    setBusy(true);
    try {
      const r = await acknowledgeAdvisory(a.id, halt ? note.trim() : undefined);
      toast.success(halt ? "Halt signed off" : "Acknowledged", halt ? `${r.signed_off} halt(s) released; sizing follows the drawdown again` : undefined);
      invalidate("growth");
    } catch (err) {
      toast.error("Refused", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/40">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <Pill tone={SEVERITY_TONE[a.severity]}>{a.severity.toLowerCase()}</Pill>
          <span className="truncate text-sm font-semibold text-stone-900 dark:text-stone-100">{a.title}</span>
        </span>
        <span className="text-[11px] text-stone-500">{formatDateTime(a.created_at)}</span>
      </div>
      <p className="text-xs leading-relaxed text-stone-700 dark:text-stone-300">{a.message}</p>
      {a.action_directive && <p className="text-[11px] text-stone-500 dark:text-stone-400">{a.action_directive}</p>}
      {a.is_acknowledged ? (
        <p className="text-[11px] text-stone-400">Acknowledged{a.acknowledgement_note ? `: ${a.acknowledgement_note}` : ""}</p>
      ) : halt ? (
        isAdmin ? (
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Sign-off reason (kept)" className="min-w-[16rem] flex-1">
              <TextInput id={noteId} value={note} maxLength={512} placeholder="e.g. losses reviewed with the desk; resume" onChange={(e) => setNote(e.target.value)} />
            </Field>
            <Button variant="primary" icon="verified_user" busy={busy} disabled={note.trim().length < 5} onClick={() => void ack()}>
              Sign off the halt
            </Button>
          </div>
        ) : (
          <p className="text-[11px] text-rose-600 dark:text-rose-300">Awaiting an administrator's sign-off: every stake stays at zero until then.</p>
        )
      ) : (
        <div className="flex justify-end">
          <Button size="sm" variant="ghost" icon="done" busy={busy} onClick={() => void ack()}>
            Acknowledge
          </Button>
        </div>
      )}
    </li>
  );
};

const TRANSFER_COLUMNS = (onStatus: (t: Transfer, s: "APPROVED" | "EXECUTED" | "DISMISSED") => void) => [
  { key: "move", header: "Move", render: (t: Transfer) => <span className="font-mono text-xs">{t.source_venue} → {t.destination_venue}</span> },
  { key: "amt", header: "Amount", align: "right" as const, render: (t: Transfer) => <span className="font-mono">{rupees(t.amount_inr)}</span> },
  { key: "status", header: "Status", render: (t: Transfer) => <Pill tone={t.status === "EXECUTED" ? "good" : t.status === "PENDING" || t.status === "APPROVED" ? "accent" : "neutral"}>{t.status.toLowerCase()}</Pill> },
  {
    key: "act",
    header: "",
    render: (t: Transfer) =>
      t.status === "PENDING" || t.status === "APPROVED" ? (
        <span className="flex gap-1">
          <Button size="sm" variant="ghost" icon="task_alt" onClick={() => onStatus(t, "EXECUTED")}>Executed</Button>
          <Button size="sm" variant="ghost" icon="close" onClick={() => onStatus(t, "DISMISSED")}>Dismiss</Button>
        </span>
      ) : null,
  },
  { key: "when", header: "Recorded", render: (t: Transfer) => <span className="text-xs text-stone-500">{formatDateTime(t.created_at)}</span> },
];

const RebalanceTab = () => {
  const plan = useRebalance(true);
  const [busy, setBusy] = useState(false);
  const record = async () => {
    setBusy(true);
    try {
      const r = await recordRebalance();
      toast.success(r.transfers.length ? `${r.transfers.length} transfer(s) recorded` : "Nothing to move", r.reason ?? undefined);
      invalidate("growth");
    } catch (err) {
      toast.error("Refused", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const onStatus = async (t: Transfer, status: "APPROVED" | "EXECUTED" | "DISMISSED") => {
    try {
      await setTransferStatus(t.id, status);
      invalidate("growth:rebalance");
    } catch (err) {
      toast.error("Refused", refusal(err));
    }
  };
  return (
    <Async resource={plan} skeletonRows={2}>
      {(p) => {
        const venues = Object.keys({ ...p.venue_balances, ...p.target_allocations }).sort();
        const total = Number(p.total_bankroll_inr) || 1;
        return (
          <div className="flex flex-col gap-4">
            {venues.length === 0 ? (
              <EmptyState icon="account_balance" title="No venue balance in the Vault" detail="Record each bookmaker account's balance in the Vault; rebalancing allocates what is there by the EV each venue's bets captured." />
            ) : (
              <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {venues.map((v) => (
                  <li key={v} className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/40">
                    <div className="flex items-baseline justify-between">
                      <span className="font-mono text-sm font-semibold text-stone-900 dark:text-stone-100">{v}</span>
                      <span className="text-[11px] text-stone-500">EV ₹{(p.ev_flow_inr[v] ?? 0).toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
                    </div>
                    <Meter value={Number(p.venue_balances[v] ?? 0) / total} label={`${v} balance share`} />
                    <p className="text-[11px] text-stone-500 dark:text-stone-400">
                      holds {rupees(p.venue_balances[v] ?? 0)} · EV share {p.target_allocations[v] ? rupees(p.target_allocations[v]) : "—"} ({pct(p.target_weights[v], 0)})
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {Object.keys(p.unpriced).length > 0 && (
              <p className="text-[11px] text-amber-700 dark:text-amber-300">Not counted: {Object.entries(p.unpriced).map(([v, why]) => `${v} (${why})`).join("; ")}</p>
            )}
            {p.reason && <p className="text-xs text-stone-500 dark:text-stone-400">{p.reason}</p>}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-stone-600 dark:text-stone-300">
                {p.transfers.length ? p.transfers.map((t) => `${t.source_venue} → ${t.destination_venue} ${rupees(t.amount_inr)}`).join(" · ") : "Every venue is within the minimum transfer of its EV share."}
              </p>
              <Button size="sm" icon="swap_horiz" busy={busy} disabled={!p.transfers.length} onClick={() => void record()}>
                Record this plan
              </Button>
            </div>
            {p.recorded.length > 0 && <DataTable columns={TRANSFER_COLUMNS((t, s) => void onStatus(t, s))} rows={p.recorded} rowKey={(t) => t.id} dense />}
            <p className="text-[11px] text-stone-400 dark:text-stone-500">
              V* = W·E^γ/ΣE^γ, γ {p.parameters.gamma}, EV over {p.parameters.lookback_days} days, moves of at least {rupees(p.parameters.min_transfer_inr)}. Nothing moves money: withdraw and deposit at the bookmakers, then mark it executed.
            </p>
          </div>
        );
      }}
    </Async>
  );
};

export const BankrollGrowthOptimizer = () => {
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const policy = useGrowthPolicy();
  const board = useStrategyBoard();
  const advisories = useGrowthAdvisories();
  const [tab, setTab] = useState<Tab>("strategies");
  const open = advisories.data?.filter((a) => !a.is_acknowledged).length ?? 0;
  return (
    <Panel
      title="KUMBHA · capital growth"
      icon="savings"
      className="lg:col-span-12"
      subtitle="One sizing policy for every stake: fractional Kelly, a ceiling, and the drawdown damper"
      updatedAt={policy.updatedAt}
    >
      <div className="flex flex-col gap-6">
        <Async resource={policy} skeletonRows={1}>
          {(p) => {
            const regime = REGIME[p.regime];
            return (
              <div className="flex flex-col gap-4">
                <StatGrid cols={5}>
                  <Stat label="Bankroll" value={p.bankroll_inr ? formatINRCompact(Number(p.bankroll_inr)) : "—"} icon="account_balance_wallet" hint={p.bankroll_inr ? "the CFO main account" : "fund the CFO main account"} />
                  <Stat label={`Drawdown (${p.window_days}d)`} value={pct(p.drawdown)} icon="trending_down" tone={p.drawdown >= p.policy.regimes[1].from_drawdown ? "caution" : "neutral"} />
                  <Stat label="Regime" value={regime.label} icon="speed" tone={regime.tone === "good" ? "positive" : regime.tone === "warning" ? "caution" : "negative"} hint={`damper ×${p.damper}`} />
                  <Stat label="Most a bet stakes" value={p.stake_ceiling_inr ? formatINR(Number(p.stake_ceiling_inr)) : "—"} icon="price_check" hint={`${pct(p.effective_ceiling, 2)} of bankroll`} />
                  <Stat label="Skill multiplier" value={`×${p.skill_multiplier.toFixed(2)}`} icon="psychology" hint={p.skill_bss === null ? "no measured skill: neutral" : `BSS ${p.skill_bss >= 0 ? "+" : ""}${(p.skill_bss * 100).toFixed(1)}%`} />
                </StatGrid>
                <div className="grid grid-cols-4 gap-1" aria-label="drawdown regimes">
                  {p.policy.regimes.map((r) => (
                    <div key={r.name} className={`rounded-xl px-2 py-1.5 text-[11px] ${r.name === p.regime || (p.regime === "LATCHED_HALT" && r.name === "CIRCUIT_BREAKER_HALT") ? "bg-stone-900 text-white dark:bg-stone-100 dark:text-stone-900" : "bg-stone-100 text-stone-500 dark:bg-stone-800 dark:text-stone-400"}`}>
                      <span className="block font-semibold">{REGIME[r.name].label}</span>
                      from {pct(r.from_drawdown, 0)} · ×{r.multiplier}
                    </div>
                  ))}
                </div>
                {p.halt_latched && <p className="text-xs font-medium text-rose-600 dark:text-rose-300">A drawdown halt awaits an administrator's sign-off: every stake is zero, the twin's pillar 13 included.</p>}
              </div>
            );
          }}
        </Async>

        <Segmented<Tab>
          size="sm"
          label="Capital growth"
          value={tab}
          onChange={setTab}
          options={[
            { value: "strategies", label: "Strategies", icon: "balance" },
            { value: "forecast", label: "Forecast", icon: "casino" },
            { value: "advisories", label: open ? `Advisories (${open})` : "Advisories", icon: "notifications" },
            ...(isAdmin ? [{ value: "rebalance" as const, label: "Rebalance", icon: "swap_horiz" }] : []),
          ]}
        />

        {tab === "strategies" && (
          <Async resource={board} skeletonRows={3}>
            {(b) => (
              <div className="flex flex-col gap-3">
                <DataTable columns={STRATEGY_COLUMNS(b.active_strategy)} rows={b.strategies} rowKey={(s) => s.strategy_code} dense />
                <p className="text-[11px] text-stone-400 dark:text-stone-500">
                  Each forecast: {b.paths.toLocaleString("en-IN")} paths over {b.horizon_days} days, one seed for all, bootstrapping {b.history.bets} of your settled bets (hit rate {pct(b.history.hit_rate)}, realised ROI{" "}
                  {pct(b.history.realised_roi, 2)}). "Typical bet": your median full Kelly {pct(b.typical_bet.full_kelly, 2)} at odds {b.typical_bet.odds}. The ruin bound is (1 − ½)^(2/κ − 1).
                </p>
              </div>
            )}
          </Async>
        )}
        {tab === "forecast" && (
          <Async resource={policy} skeletonRows={1}>
            {(p) => <ForecastTab horizons={p.horizons} strategies={p.strategies} active={p.active_strategy} />}
          </Async>
        )}
        {tab === "advisories" && (
          <Async resource={advisories} isEmpty={(rows) => rows.length === 0} empty={<p className="text-xs text-stone-500 dark:text-stone-400">No advisory yet: the scan writes one when your drawdown regime changes.</p>}>
            {(rows) => (
              <ul className="flex flex-col gap-3">
                {rows.map((a) => (
                  <AdvisoryCard key={a.id} a={a} isAdmin={isAdmin} />
                ))}
              </ul>
            )}
          </Async>
        )}
        {tab === "rebalance" && isAdmin && <RebalanceTab />}

        <Async resource={policy} skeletonRows={0}>
          {(p) => (
            <p className="text-[11px] text-stone-400 dark:text-stone-500">
              f = min(f*·κ·ψ·λ, {pct(p.policy.max_fraction, 1)})·φ(D), κ = {p.policy.kelly_fraction} · quarter Kelly halves the bankroll with probability {pct(p.ruin_bound_halving, 2)} · Developer: {p.developer_credit}
            </p>
          )}
        </Async>
      </div>
    </Panel>
  );
};
