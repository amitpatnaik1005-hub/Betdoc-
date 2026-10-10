/**
 * Group 77 additions to the Lab's backtest results (QuantLab, Group 66). The engine and its run are the Lab's;
 * these panels show what Group 77 added to them:
 *
 * - Rolling walk-forward: each fold tuned on its own in-sample window, judged on the window after it, and the
 *   walk-forward efficiency (mean out-of-sample return over mean in-sample return).
 * - Tail risk and skill: the bootstrap's 95% and 99% Value-at-Risk and CVaR of the run's P&L, the Sharpe and
 *   Sortino net of the risk-free rate, and the models' Brier skill against the closing line on the same fills.
 */
import { formatDate, formatINR } from "../../lib/format";
import type { BacktestResult, RollingFold } from "../../lib/lab";
import { DataTable, EmptyState, Pill, Stat, StatGrid } from "../../ui/kit";

const pct = (v: number | null | undefined, digits = 2): string => (v === null || v === undefined || !Number.isFinite(v) ? "—" : `${v.toFixed(digits)}%`);
const signed = (v: number | null | undefined, digits = 2): string => (v === null || v === undefined || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`);
const ratio = (v: number | null | undefined): string => (v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(2));
const VERDICT_TONE = { ROBUST: "good", DEGRADED: "warning", OVERFIT: "critical", NO_EDGE: "critical", INSUFFICIENT_DATA: "neutral" } as const;

const FOLD_COLUMNS = [
  { key: "fold", header: "Fold", render: (f: RollingFold) => <span className="font-mono">{f.fold}</span> },
  {
    key: "window",
    header: "In-sample → out-of-sample",
    render: (f: RollingFold) => (
      <span className="text-xs text-stone-600 dark:text-stone-300">
        {formatDate(f.in_sample_window[0])} → {formatDate(f.out_of_sample_window[0])} → {formatDate(f.out_of_sample_window[1])}
      </span>
    ),
  },
  { key: "kelly", header: "Kelly", align: "right" as const, render: (f: RollingFold) => <span className="font-mono">{f.kelly ? `×${f.kelly}` : "bots'"}</span> },
  { key: "is", header: "IS Sharpe · return", align: "right" as const, render: (f: RollingFold) => <span className="font-mono">{ratio(f.in_sample.sharpe)} · {signed(f.in_sample.return_pct)}</span> },
  { key: "oos", header: "OOS Sharpe · return", align: "right" as const, render: (f: RollingFold) => <span className="font-mono">{ratio(f.out_of_sample.sharpe)} · {signed(f.out_of_sample.return_pct)}</span> },
  { key: "trades", header: "OOS trades", align: "right" as const, render: (f: RollingFold) => <span className="font-mono">{f.out_of_sample.trades}</span> },
  { key: "verdict", header: "Verdict", render: (f: RollingFold) => <Pill tone={VERDICT_TONE[f.verdict.verdict] ?? "neutral"}>{f.verdict.verdict.toLowerCase().replace("_", " ")}</Pill> },
];

export const RollingWalkForwardPanel = ({ result }: { result: BacktestResult }) => {
  const rolling = result.walk_forward.rolling;
  if (!rolling) {
    return <EmptyState icon="view_timeline" title="One split only" detail="Set rolling folds above 1 to tune and test on successive windows: an edge that holds up fold after fold is not an accident of one split." />;
  }
  const s = rolling.summary;
  return (
    <div className="flex flex-col gap-4">
      <StatGrid cols={4}>
        <Stat label="Robust folds" value={`${s.robust_folds} / ${s.folds}`} tone={s.robust_folds * 2 >= s.folds ? "positive" : "caution"} hint={`${Math.round(rolling.train_ratio * 100)}% in-sample per fold`} />
        <Stat label="OOS Sharpe (mean · worst)" value={`${ratio(s.oos_sharpe_mean)} · ${ratio(s.oos_sharpe_min)}`} />
        <Stat label="OOS return (mean)" value={signed(s.oos_return_pct_mean)} tone={s.oos_return_pct_mean === null ? "neutral" : s.oos_return_pct_mean >= 0 ? "positive" : "negative"} />
        <Stat label="Walk-forward efficiency" value={s.walk_forward_efficiency === null ? "—" : s.walk_forward_efficiency.toFixed(2)} hint="OOS ÷ IS return: near 1 holds up, near 0 was fitted" />
      </StatGrid>
      <DataTable columns={FOLD_COLUMNS} rows={rolling.folds} rowKey={(f) => String(f.fold)} dense />
    </div>
  );
};

export const TailRiskPanel = ({ result }: { result: BacktestResult }) => {
  const tail = result.monte_carlo.bootstrap.var_cvar_inr;
  const m = result.metrics;
  return (
    <StatGrid cols={4}>
      <Stat label="VaR 95% · CVaR" value={tail ? formatINR(tail["95"].var) : "—"} icon="trending_down" hint={tail ? `CVaR ${formatINR(tail["95"].cvar)}: the mean of the worst 5%` : "no bootstrap"} />
      <Stat label="VaR 99% · CVaR" value={tail ? formatINR(tail["99"].var) : "—"} icon="warning" hint={tail ? `CVaR ${formatINR(tail["99"].cvar)}: the mean of the worst 1%` : undefined} />
      <Stat label="Brier skill vs close" value={m.brier_skill_score === null || m.brier_skill_score === undefined ? "—" : signed(m.brier_skill_score * 100, 1)}
            tone={m.brier_skill_score === null || m.brier_skill_score === undefined ? "neutral" : m.brier_skill_score >= 0 ? "positive" : "negative"}
            hint={`Brier ${m.brier_score?.toFixed(4) ?? "—"} on ${m.brier_fills ?? 0} fills`} />
      <Stat label="Risk-free rate" value={pct((m.risk_free_rate ?? 0) * 100, 1)} hint="Sharpe and Sortino are in excess of it" />
    </StatGrid>
  );
};
