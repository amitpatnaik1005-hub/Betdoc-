/**
 * The model recalibration engine on The Oracle page (Group 74).
 *
 * - One card per model: its lifecycle state, the weight pillar 1 uses now against its state's band, its
 *   Brier skill against the sharp close, its CLV and sample size, pinned or not.
 * - Run now (administrators), a manual weight (optionally pinned through later runs), and the emergency
 *   reset to equal weights. Every one of them is recorded as a run.
 * - The latest audits: each model's move through the lifecycle, and why.
 */
import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { formatDateTime } from "../../lib/format";
import { LIFECYCLE, overrideWeight, resetWeights, runRecalibration, useCalibrationHistory, useCalibrationWeights, type ModelState, type WeightAudit } from "../../lib/calibration";
import { invalidate } from "../../lib/resource";
import { toast } from "../../store/useToastStore";
import { Async, Button, ConfirmButton, DataTable, EmptyState, Field, Meter, NumberInput, Panel, Pill, TextInput, Toggle } from "../../ui/kit";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const signed = (v: number | null | undefined, digits = 1, scale = 1): string => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v * scale).toFixed(digits)}%`);
const refresh = () => invalidate("calibration", "feedback:accuracy");

const OverrideDialog = ({ model, ceiling, onClose }: { model: ModelState; ceiling: number; onClose: () => void }) => {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  const [weight, setWeight] = useState(String(model.weight_in_force ?? 1));
  const [reason, setReason] = useState("");
  const [pin, setPin] = useState(model.pinned);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    box.current?.querySelector<HTMLInputElement>("input")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const value = Number(weight);
  const valid = weight.trim() !== "" && value >= 0 && value <= ceiling && reason.trim().length >= 5;
  const submit = async () => {
    setBusy(true);
    try {
      await overrideWeight({ model_name: model.model_name, weight: value, reason: reason.trim(), pin });
      toast.success(`${model.model_name} set to ${value}`, pin ? "Pinned: later runs keep it" : "The next run recomputes it");
      refresh();
      onClose();
    } catch (err) {
      toast.error("Override refused", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-stone-950/40 p-4 sm:items-center" role="presentation" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={titleId} className="w-full max-w-md rounded-3xl bg-white p-6 shadow-soft-lg dark:bg-stone-900 dark:ring-1 dark:ring-white/10">
        <h3 id={titleId} className="text-base font-semibold text-stone-900 dark:text-stone-100">
          Set the weight of <span className="font-mono">{model.model_name}</span>
        </h3>
        <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">Its vote in pillar 1. 0 benches it: no vote and no veto.</p>
        <div className="mt-4 grid grid-cols-2 gap-3">
          <Field label={`Weight (0 to ${ceiling})`}>
            <NumberInput value={weight} min={0} max={ceiling} step="0.05" onChange={(e) => setWeight(e.target.value)} />
          </Field>
          <div className="flex items-end pb-1">
            <Toggle checked={pin} onChange={setPin} label="Pin through later runs" />
          </div>
          <Field label="Reason (kept in the audit)" className="col-span-2">
            <TextInput value={reason} maxLength={512} placeholder="e.g. feed outage on its inputs: benched until it recovers" onChange={(e) => setReason(e.target.value)} />
          </Field>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" icon="tune" busy={busy} disabled={!valid} onClick={() => void submit()}>
            Apply
          </Button>
        </div>
      </div>
    </div>
  );
};

const ModelCard = ({ m, bands, onEdit }: { m: ModelState; bands: Record<string, [number, number]>; onEdit: () => void }) => {
  const state = m.status ? LIFECYCLE[m.status] : null;
  const weight = m.weight_in_force ?? 1;
  const top = Math.max(...Object.values(bands).map(([, hi]) => hi), 1);
  const band = m.status ? bands[m.status] : undefined;
  return (
    <li className="flex min-w-0 flex-col gap-3 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/40">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-sm font-semibold text-stone-900 dark:text-stone-100">{m.model_name}</span>
        <span className="flex gap-1">
          {m.pinned && <Pill icon="push_pin">pinned</Pill>}
          {state ? <Pill tone={state.tone} icon={state.icon}>{state.label}</Pill> : <Pill>not yet run</Pill>}
        </span>
      </div>
      <div>
        <div className="mb-1 flex items-baseline justify-between text-[11px] text-stone-500 dark:text-stone-400">
          <span>Weight in pillar 1{band ? ` · band ${band[0]}–${band[1]}` : ""}</span>
          <span className="font-mono text-sm text-stone-900 dark:text-stone-100">{m.weight_in_force === null ? "1 (neutral)" : weight.toFixed(3)}</span>
        </div>
        <Meter value={Math.min(weight / top, 1)} tone={m.status === "BENCHED" ? "critical" : m.status === "PROBATION" ? "warning" : m.status === "ALPHA_BOOSTED" ? "good" : "accent"} label={`${m.model_name} weight`} />
      </div>
      <p className="text-[11px] text-stone-500 dark:text-stone-400">
        skill vs close {signed(m.brier_skill_score, 1, 100)} · CLV {signed(m.avg_clv_pct, 2)} · {m.sample_count ?? 0} settled
      </p>
      {m.status_reason && (
        <p className="line-clamp-2 text-[11px] text-stone-400 dark:text-stone-500" title={m.status_reason}>
          {m.status_reason}
        </p>
      )}
      <div className="flex justify-end">
        <Button size="sm" variant="ghost" icon="tune" onClick={onEdit}>
          Set weight
        </Button>
      </div>
    </li>
  );
};

const HISTORY_COLUMNS = [
  { key: "model", header: "Model", render: (a: WeightAudit) => <span className="font-mono">{a.model_name}</span> },
  {
    key: "state",
    header: "State",
    render: (a: WeightAudit) => (
      <span className="flex items-center gap-1 text-xs">
        {a.previous_status && a.previous_status !== a.status && <span className="text-stone-400">{LIFECYCLE[a.previous_status].label} →</span>}
        <Pill tone={LIFECYCLE[a.status].tone}>{LIFECYCLE[a.status].label}</Pill>
      </span>
    ),
  },
  { key: "n", header: "Settled", align: "right" as const, render: (a: WeightAudit) => `${a.sample_count} (${a.paired_count} paired)` },
  { key: "bss", header: "Skill vs close", align: "right" as const, render: (a: WeightAudit) => signed(a.brier_skill_score, 1, 100) },
  { key: "clv", header: "CLV", align: "right" as const, render: (a: WeightAudit) => signed(a.avg_clv_pct, 2) },
  {
    key: "weight",
    header: "Weight",
    align: "right" as const,
    render: (a: WeightAudit) => (
      <span className="font-mono">
        {a.previous_weight === null ? "1" : a.previous_weight.toFixed(2)} → <strong>{a.new_weight.toFixed(2)}</strong>
      </span>
    ),
  },
  { key: "when", header: "When", render: (a: WeightAudit) => <span className="text-xs text-stone-500">{formatDateTime(a.created_at)}</span> },
];

export const ModelRecalibrationPanel = () => {
  const weights = useCalibrationWeights();
  const history = useCalibrationHistory();
  const [busy, setBusy] = useState<"run" | "reset" | null>(null);
  const [editing, setEditing] = useState<ModelState | null>(null);
  const run = async () => {
    setBusy("run");
    try {
      const r = await runRecalibration();
      toast.success(r.published ? `Recalibrated: ${r.models_promoted} up, ${r.models_demoted} down` : "Nothing published", r.note ?? `${r.models_evaluated} models scored against ${r.benchmark_model}`);
      refresh();
    } catch (err) {
      toast.error("Recalibration refused", refusal(err));
    } finally {
      setBusy(null);
    }
  };
  const reset = async () => {
    setBusy("reset");
    try {
      const r = await resetWeights("emergency reset from the Oracle page");
      toast.success("Weights reset", r.message);
      refresh();
    } catch (err) {
      toast.error("Reset refused", refusal(err));
    } finally {
      setBusy(null);
    }
  };
  return (
    <Panel
      title="Model recalibration engine"
      icon="psychology"
      className="lg:col-span-12"
      subtitle="Brier skill against the sharp close moves each model through alpha boost, active, probation and the bench"
      updatedAt={weights.updatedAt}
      actions={
        <>
          <ConfirmButton size="sm" variant="ghost" icon="restart_alt" busy={busy === "reset"} confirmLabel="Reset to equal?" onConfirm={() => void reset()}>
            Reset weights
          </ConfirmButton>
          <Button size="sm" icon="model_training" busy={busy === "run"} onClick={() => void run()}>
            Run now
          </Button>
        </>
      }
    >
      <Async resource={weights} skeletonRows={3}>
        {(w) => {
          const ceiling = Math.max(...Object.values(w.bands).map(([, hi]) => hi));
          return (
            <div className="flex flex-col gap-5">
              {w.models.length === 0 ? (
                <EmptyState icon="balance" title="Equal weights in force" detail="Pillar 1 weighs every model equally until the engine has settled predictions to score (Group 73's feedback loop records them)." />
              ) : (
                <ul className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {w.models.map((m) => (
                    <ModelCard key={m.model_name} m={m} bands={w.bands} onEdit={() => setEditing(m)} />
                  ))}
                </ul>
              )}
              <Async resource={history} skeletonRows={2} isEmpty={(h) => h.length === 0} empty={<p className="text-xs text-stone-500 dark:text-stone-400">No recalibration has run yet.</p>}>
                {(h) => <DataTable columns={HISTORY_COLUMNS} rows={h} rowKey={(a) => `${a.run_id}:${a.model_name}`} dense />}
              </Async>
              <p className="text-[11px] text-stone-400 dark:text-stone-500">
                Benchmark {w.benchmark} ·{" "}
                {w.last_run ? `last run ${formatDateTime(w.last_run.created_at)} (${w.last_run.trigger_type.toLowerCase().replaceAll("_", " ")})` : "no run yet"} · Developer: {w.developer_credit}
              </p>
              {editing && <OverrideDialog model={editing} ceiling={ceiling} onClose={() => setEditing(null)} />}
            </div>
          );
        }}
      </Async>
    </Panel>
  );
};
