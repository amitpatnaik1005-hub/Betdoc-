/**
 * Add / edit a config-driven provider: a JSON ProviderSpec (endpoints, auth, rate limit, JSONPath
 * mapping), dry-run against a pasted sample response before it ever makes a request.
 */
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { apiClient } from "../../api/client";
import { formatOdds, formatRatioPct } from "../../lib/format";
import { runMutation } from "../../lib/resource";
import { Button, Field, Pill, SPRING, Select, TextInput, Toggle } from "../../ui/kit";
import { type FleetSource, type ProviderPreview, cx } from "./types";

const ID_PATTERN = /^[a-z][a-z0-9_]{2,47}$/;

const editorClass =
  "w-full rounded-2xl bg-stone-100/80 px-4 py-3 font-mono text-[12px] leading-relaxed text-stone-800 ring-1 ring-inset ring-transparent focus:bg-white focus:outline-none focus:ring-[color-mix(in_srgb,var(--accent)_45%,transparent)] dark:bg-stone-800/70 dark:text-stone-100 dark:focus:bg-stone-800";

const parse = (text: string): { value: unknown; error: string | null } => {
  if (!text.trim()) return { value: undefined, error: null };
  try {
    return { value: JSON.parse(text), error: null };
  } catch (err) {
    return { value: undefined, error: err instanceof Error ? err.message : "Invalid JSON" };
  }
};

/** Mounted only while open (and keyed per provider), so every opening starts from a clean editor. */
export const ProviderModal = ({ open, editing, onClose }: { open: boolean; editing: FleetSource | null; onClose: () => void }) => (
  <AnimatePresence>{open && <ProviderDialog key={editing?.source_id ?? "new"} editing={editing} onClose={onClose} />}</AnimatePresence>
);

const ProviderDialog = ({ editing, onClose }: { editing: FleetSource | null; onClose: () => void }) => {
  const [sourceId, setSourceId] = useState(editing?.source_id ?? "");
  const [specText, setSpecText] = useState(editing?.spec ? JSON.stringify(editing.spec, null, 2) : "");
  const [sampleText, setSampleText] = useState("");
  const [chosenSport, setSport] = useState("");
  const [enable, setEnable] = useState(false);
  const [preview, setPreview] = useState<ProviderPreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "save" | null>(null);

  // A new provider starts from the server's template (an aggregator-style mapping)
  useEffect(() => {
    if (editing?.spec) return;
    let alive = true;
    apiClient
      .get<Record<string, unknown>>("/omni/fleet/providers/template")
      .then((template) => alive && setSpecText((current) => current || JSON.stringify(template, null, 2)))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [editing]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const spec = useMemo(() => parse(specText), [specText]);
  const sample = useMemo(() => parse(sampleText), [sampleText]);
  const coverage = useMemo(() => {
    const value = spec.value as { coverage?: Record<string, string> } | undefined;
    return value && typeof value.coverage === "object" && value.coverage ? Object.keys(value.coverage) : [];
  }, [spec.value]);
  const sport = coverage.includes(chosenSport) ? chosenSport : coverage[0] ?? "";

  const idOk = editing !== null || ID_PATTERN.test(sourceId);
  const specOk = spec.value !== undefined && spec.error === null;

  const runPreview = async () => {
    if (!specOk || sample.value === undefined || sample.error) return;
    setBusy("preview");
    const result = await runMutation(() => apiClient.post<ProviderPreview>("/omni/fleet/providers/preview", { spec: spec.value, sport, sample: sample.value }), {
      errorTitle: "Mapping rejected",
    });
    setBusy(null);
    if (result) setPreview(result);
  };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!idOk || !specOk) return;
    setBusy("save");
    const ok = editing
      ? await runMutation(() => apiClient.put<FleetSource>(`/omni/fleet/providers/${editing.source_id}`, { spec: spec.value }), {
          invalidate: ["fleet"],
          success: `${editing.display_name} mapping saved`,
          errorTitle: "Spec rejected",
        })
      : await runMutation(() => apiClient.post<FleetSource>("/omni/fleet/providers", { source_id: sourceId, spec: spec.value, is_enabled: enable }), {
          invalidate: ["fleet"],
          success: (s) => `${s.display_name} added to the matrix${enable ? " and switched on" : ""}`,
          errorTitle: "Spec rejected",
        });
    setBusy(null);
    if (ok) onClose();
  };

  return (
    <motion.div
      className="fixed inset-0 z-[80] flex items-end justify-center bg-stone-900/30 backdrop-blur-sm sm:items-center sm:p-6 dark:bg-black/50"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <motion.form
        role="dialog"
        aria-modal="true"
        aria-label={editing ? `Edit ${editing.display_name}` : "Add provider"}
        onSubmit={save}
        initial={{ opacity: 0, y: 32, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 24, scale: 0.98 }}
        transition={SPRING}
        className="flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-t-[2rem] bg-[#FBFAF6] shadow-soft-lg sm:rounded-[2rem] dark:bg-stone-900 dark:ring-1 dark:ring-white/[0.06]"
      >
        <header className="flex items-start justify-between gap-4 px-6 pb-4 pt-7 sm:px-10">
          <div>
            <h2 className="font-display text-xl font-semibold text-stone-900 dark:text-stone-50">{editing ? `Edit ${editing.display_name}` : "Add a provider"}</h2>
            <p className="mt-1 max-w-xl text-sm leading-relaxed text-stone-500 dark:text-stone-400">
              Describe the API as JSON: where to call, how to authenticate, how fast, and which JSONPath fields hold the teams, kick-off and prices.
              Keys never go in the spec: name the environment variable, or store the key in the matrix afterwards.
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="grid size-9 shrink-0 place-items-center rounded-full text-stone-400 transition-colors hover:bg-stone-200/60 hover:text-stone-700 dark:hover:bg-white/10">
            <span className="material-symbols-outlined text-[20px]">close</span>
          </button>
        </header>

        <div className="grid flex-1 grid-cols-1 gap-8 overflow-y-auto px-6 pb-6 sm:px-10 lg:grid-cols-2">
          <div className="flex min-w-0 flex-col gap-4">
            {!editing && (
              <Field label="Provider id" hint={sourceId && !idOk ? "lowercase letters, digits and _; 3-48 characters, starting with a letter" : "Used for its lock, rate-limit bucket and OMNI_<ID>_API_KEY"}>
                <TextInput value={sourceId} onChange={(e) => setSourceId(e.target.value.trim().toLowerCase())} placeholder="partner_feed" className="font-mono" />
              </Field>
            )}
            <Field label="Provider spec (JSON)" hint={spec.error ? <span className="text-rose-600 dark:text-rose-300">{spec.error}</span> : "Validated on the server: paths, https, auth and coverage keys"}>
              <textarea value={specText} onChange={(e) => setSpecText(e.target.value)} rows={20} spellCheck={false} className={cx(editorClass, "min-h-[22rem]")} />
            </Field>
          </div>

          <div className="flex min-w-0 flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <div className="flex items-end justify-between gap-2">
                <label htmlFor="fleet-sample" className="text-xs font-medium text-stone-500 dark:text-stone-400">Sample response (one body, as the provider sends it)</label>
                {coverage.length > 0 && (
                  <Select value={sport} onChange={(e) => setSport(e.target.value)} aria-label="Market group" className="w-44 py-1.5 text-xs">
                    {coverage.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </Select>
                )}
              </div>
              <textarea id="fleet-sample" value={sampleText} onChange={(e) => setSampleText(e.target.value)} rows={8} spellCheck={false} placeholder='{"data": [ ... ]}' className={editorClass} />
              <span className="text-[11px] text-stone-400 dark:text-stone-500">
                {sample.error ? <span className="text-rose-600 dark:text-rose-300">{sample.error}</span> : "Mapped on the server without making any request"}
              </span>
            </div>
            <Button icon="science" busy={busy === "preview"} disabled={!specOk || sample.value === undefined || !!sample.error || !sport} onClick={() => void runPreview()} className="self-start">
              Preview mapping
            </Button>

            <AnimatePresence mode="wait">
              {preview && (
                <motion.div key="preview" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={SPRING} className="flex flex-col gap-3 rounded-3xl bg-white p-4 shadow-soft dark:bg-white/[0.03] dark:shadow-none">
                  <div className="flex flex-wrap gap-1.5">
                    <Pill tone={preview.events_normalized > 0 ? "good" : "critical"} icon="sports">{preview.events_normalized}/{preview.events_seen} fixtures mapped</Pill>
                    {preview.malformed > 0 && <Pill tone="warning" icon="report">{preview.malformed} malformed</Pill>}
                    {preview.unmapped.length > 0 && <Pill tone="warning" icon="person_search">{preview.unmapped.length} unknown names</Pill>}
                    {Object.entries(preview.devig).map(([m, n]) => (
                      <Pill key={m} tone="neutral">{m} × {n}</Pill>
                    ))}
                  </div>
                  {preview.ticks.length > 0 ? (
                    <div className="max-h-56 overflow-y-auto">
                      <table className="w-full text-left text-xs">
                        <thead className="text-stone-400">
                          <tr><th className="pb-2 font-medium">Fixture</th><th className="pb-2 font-medium">Pick</th><th className="pb-2 text-right font-medium">Odds</th><th className="pb-2 text-right font-medium">True p</th></tr>
                        </thead>
                        <tbody className="divide-y divide-stone-900/[0.04] dark:divide-white/[0.04]">
                          {preview.ticks.map((t) => (
                            <tr key={`${t.match_id}-${t.selection}`}>
                              <td className="py-1.5 pr-2">
                                <span className={t.home_canonical ? "" : "text-amber-700 dark:text-amber-300"}>{t.home_team}</span>
                                <span className="text-stone-400"> v </span>
                                <span className={t.away_canonical ? "" : "text-amber-700 dark:text-amber-300"}>{t.away_team}</span>
                              </td>
                              <td className="py-1.5">{t.selection}</td>
                              <td className="py-1.5 text-right font-mono tabular-nums">{formatOdds(t.odds)}</td>
                              <td className="py-1.5 text-right font-mono tabular-nums">{formatRatioPct(t.true_probability)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="text-xs text-stone-500">No ticks: check the events path, market filter and selection names.</p>
                  )}
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>

        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-stone-900/[0.05] px-6 py-5 sm:px-10 dark:border-white/[0.05]">
          {!editing ? (
            <label className="flex items-center gap-3 text-sm text-stone-600 dark:text-stone-300">
              <Toggle label="Switch on after creating" checked={enable} onChange={setEnable} />
              Switch on after creating
            </label>
          ) : (
            <span className="text-xs text-stone-400">Saving resets its breaker and failure streak for a fresh trial.</span>
          )}
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" icon={editing ? "save" : "add"} busy={busy === "save"} disabled={!idOk || !specOk}>
              {editing ? "Save mapping" : "Add provider"}
            </Button>
          </div>
        </footer>
      </motion.form>
    </motion.div>
  );
};
