/**
 * The post-execution feedback loop on The Oracle page (Group 73).
 *
 * - Closing-line value: how often the prices taken beat the sharp close, raw and de-vigged.
 * - Model accuracy over the Brier window: every predictor's Brier score, log loss and ranked probability
 *   score, the inverse-Brier weight it earns and the weight pillar 1 is using now. The ensemble and the
 *   sharp close are benchmarks, measured but never weighted.
 * - Root causes: why the losses lost, with the evidence the classifier used.
 * - Sweep settles and attributes now; Recalibrate publishes the weights (administrators).
 */
import { useState } from "react";
import { ApiError } from "../../api/client";
import { formatDateTime } from "../../lib/format";
import { REFERENCE_LABEL, ROOT_CAUSE_LABEL, recalibrateWeights, sweepSettlements, useModelAccuracy, useSettlementSummary, type ModelAccuracy } from "../../lib/feedback";
import { rupees } from "../../lib/oracle";
import { invalidate } from "../../lib/resource";
import { toast } from "../../store/useToastStore";
import { Async, Button, DataTable, EmptyState, Panel, Pill, Stat, StatGrid } from "../../ui/kit";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const pct = (v: number | null | undefined, digits = 1): string => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);
const signed = (v: number | null | undefined, digits = 2): string => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}%`);
const fixed = (v: number | null | undefined, digits = 4): string => (v === null || v === undefined ? "—" : v.toFixed(digits));

const COLUMNS = [
  {
    key: "model",
    header: "Predictor",
    render: (m: ModelAccuracy) => (
      <span className="flex items-center gap-2">
        <span className="font-mono text-stone-900 dark:text-stone-100">{m.model_name}</span>
        {m.reference && <Pill tone="info">{REFERENCE_LABEL[m.model_name] ?? "benchmark"}</Pill>}
      </span>
    ),
  },
  { key: "n", header: "Settled", align: "right" as const, render: (m: ModelAccuracy) => m.predictions.toLocaleString("en-IN") },
  { key: "brier", header: "Brier ↓", align: "right" as const, render: (m: ModelAccuracy) => <span className="font-mono">{fixed(m.avg_brier)}</span> },
  { key: "logloss", header: "Log loss ↓", align: "right" as const, render: (m: ModelAccuracy) => <span className="font-mono">{fixed(m.avg_log_loss)}</span> },
  { key: "rps", header: "RPS ↓", align: "right" as const, render: (m: ModelAccuracy) => <span className="font-mono">{fixed(m.avg_rps)}</span> },
  {
    key: "clv",
    header: "Leg CLV",
    align: "right" as const,
    render: (m: ModelAccuracy) => <span className={(m.avg_clv_pct ?? 0) >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-rose-600"}>{signed(m.avg_clv_pct)}</span>,
  },
  {
    key: "weight",
    header: "Weight earned · in use",
    align: "right" as const,
    render: (m: ModelAccuracy) =>
      m.reference ? (
        <span className="text-stone-400">not weighted</span>
      ) : (
        <span className="font-mono">
          {m.recommended_weight === null ? <span className="text-stone-400">needs history</span> : m.recommended_weight.toFixed(3)} · {m.published_weight === null ? "1 (neutral)" : m.published_weight.toFixed(3)}
        </span>
      ),
  },
];

export const SettlementFeedbackPanel = () => {
  const accuracy = useModelAccuracy();
  const summary = useSettlementSummary();
  const [busy, setBusy] = useState<"sweep" | "weights" | null>(null);
  const sweep = async () => {
    setBusy("sweep");
    try {
      const r = await sweepSettlements();
      toast.success(`Settled ${r.settled_bets} · attributed ${r.attributed_bets}`, `${r.feedback_records} predictions scored · P&L ${rupees(r.total_pnl_inr, true)}`);
      invalidate("feedback", "oracle:bets", "oracle:pnl", "twin:ledger");
    } catch (err) {
      toast.error("Sweep failed", refusal(err));
    } finally {
      setBusy(null);
    }
  };
  const recalibrate = async () => {
    setBusy("weights");
    try {
      const r = await recalibrateWeights();
      toast.success(
        r.published ? "Pillar 1 weights published" : "Weights unchanged",
        r.published ? Object.entries(r.weights).map(([k, v]) => `${k} ${v.toFixed(3)}`).join(" · ") : `No model has ${r.min_samples} settled predictions in the window yet`,
      );
      invalidate("feedback:accuracy");
    } catch (err) {
      toast.error("Recalibration failed", refusal(err));
    } finally {
      setBusy(null);
    }
  };
  return (
    <Panel
      title="Feedback loop · CLV & model attribution"
      icon="model_training"
      className="lg:col-span-12"
      subtitle="Every settled leg scores every model that priced it; the best-calibrated models earn more say in pillar 1"
      updatedAt={accuracy.updatedAt}
      actions={
        <>
          <Button size="sm" variant="ghost" icon="tune" busy={busy === "weights"} onClick={() => void recalibrate()}>
            Recalibrate weights
          </Button>
          <Button size="sm" icon="bolt" busy={busy === "sweep"} onClick={() => void sweep()}>
            Sweep settlements
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-5">
        <Async resource={summary} skeletonRows={1}>
          {(s) => (
            <StatGrid cols={4}>
              <Stat label="Attributed bets" value={s.attributed_bets.toLocaleString("en-IN")} icon="fact_check" hint={`${s.clv.bets} with a sharp close`} />
              <Stat label="Average CLV" value={signed(s.clv.avg_clv_pct)} icon="trending_up" tone={(s.clv.avg_clv_pct ?? 0) >= 0 ? "positive" : "negative"} hint="price taken vs sharp close" />
              <Stat label="Beat the close" value={pct(s.clv.beat_close_rate)} icon="military_tech" hint="share of bets" />
              <Stat label="CLV vs fair" value={signed(s.clv.avg_clv_sharp_pct)} icon="balance" hint="EV at the de-vigged close" />
            </StatGrid>
          )}
        </Async>

        <Async
          resource={accuracy}
          isEmpty={(a) => a.models.length === 0}
          empty={<EmptyState icon="query_stats" title="No settled predictions yet" detail="Place bets from twin-vetted slips and let them settle: each leg then scores every model that priced it." />}
        >
          {(a) => (
            <div className="flex flex-col gap-2">
              <DataTable columns={COLUMNS} rows={a.models} rowKey={(m) => m.model_name} dense />
              <p className="text-[11px] text-stone-500 dark:text-stone-400">
                Window {a.window_days} days · a model needs {a.min_samples} settled predictions to earn a weight · weights average 1 ·{" "}
                {a.published_at ? `published ${formatDateTime(a.published_at)}` : "none published yet: pillar 1 weighs every model equally"}
              </p>
            </div>
          )}
        </Async>

        <Async resource={summary} skeletonRows={2}>
          {(s) => (
            <section className="flex flex-col gap-3">
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(s.root_causes).map(([tag, n]) => (
                  <Pill key={tag} tone={tag === "NONE" ? "good" : tag === "VARIANCE_BAD_LUCK" ? "neutral" : "warning"}>
                    {ROOT_CAUSE_LABEL[tag] ?? tag} · {n}
                  </Pill>
                ))}
              </div>
              {s.recent.length === 0 ? (
                <p className="text-xs text-stone-500 dark:text-stone-400">No losses to explain yet.</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {s.recent.map((r) => (
                    <li key={r.bet_id} className="rounded-2xl bg-stone-50 px-3 py-2 text-sm dark:bg-stone-800/40">
                      <p className="flex flex-wrap items-center gap-2 text-stone-800 dark:text-stone-200">
                        <Pill tone="warning">{ROOT_CAUSE_LABEL[r.root_cause_tag] ?? r.root_cause_tag}</Pill>
                        <span className="font-mono">{rupees(r.pnl_inr, true)}</span>
                        {r.booking_code && <span className="font-mono text-[11px] text-amber-700 dark:text-amber-300">{r.booking_code}</span>}
                        <span className="text-[11px] text-stone-400">
                          {r.status.replace("_", " ").toLowerCase()} · CLV {signed(r.clv_pct)} · models' Brier {fixed(r.model_error_delta, 3)}
                          {r.settled_at ? ` · ${formatDateTime(r.settled_at)}` : ""}
                        </span>
                      </p>
                      <p className="mt-0.5 text-xs text-stone-500 dark:text-stone-400">{r.explanation}</p>
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-[11px] text-stone-400 dark:text-stone-500">Developer: {s.developer_credit}</p>
            </section>
          )}
        </Async>
      </div>
    </Panel>
  );
};
