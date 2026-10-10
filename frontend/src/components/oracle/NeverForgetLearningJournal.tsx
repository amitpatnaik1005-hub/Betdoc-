/**
 * The Never-Forget shield and experience on The Oracle page (Group 75).
 *
 * - Your experience: rank, XP and the way to the next rank, what each action earns. Ranks describe progress;
 *   they unlock nothing.
 * - The shield's measured record: lessons by status, the legs pillar 15 turned away, and how those legs
 *   actually finished once their scores came in (losses avoided, winners missed). Nothing is counted as
 *   "saved" before the score says so.
 * - Lessons: each lost leg as the fortress saw it before kickoff, the post-mortem's cause, the rule and its
 *   record. Administrators archive a lesson or promote a shadow one, with a reason that is kept.
 */
import { useEffect, useId, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { formatDateTime } from "../../lib/format";
import { activateRule, archiveRule, RULE_STATUS, SITUATION_LABEL, XP_ACTION_LABEL, useMistakeMemories, useNeverForgetStats, usePreventions, useXpHistory, useXpProfile, type MistakeMemory, type NeverForgetRule, type Prevention, type XpAward } from "../../lib/never_forget";
import { ROOT_CAUSE_LABEL } from "../../lib/feedback";
import { invalidate } from "../../lib/resource";
import { useAuthStore } from "../../store/useAuthStore";
import { toast } from "../../store/useToastStore";
import { Async, Button, DataTable, EmptyState, Field, Meter, Panel, Pill, Segmented, Stat, StatGrid, TextInput } from "../../ui/kit";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const rupees = (v: string | null | undefined): string => (v === null || v === undefined ? "—" : `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`);
const words = (s: string): string => s.replaceAll("_", " ").toLowerCase();
const refresh = () => invalidate("neverforget", "xp");

type Tab = "lessons" | "vetoes" | "xp";

const StatusDialog = ({ rule, target, onClose }: { rule: NeverForgetRule; target: "ARCHIVED" | "ACTIVE"; onClose: () => void }) => {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    box.current?.querySelector<HTMLInputElement>("input")?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const submit = async () => {
    setBusy(true);
    try {
      await (target === "ARCHIVED" ? archiveRule : activateRule)(rule.id, reason.trim());
      toast.success(target === "ARCHIVED" ? `${rule.rule_code} archived` : `${rule.rule_code} now guarding`, target === "ARCHIVED" ? "Pillar 15 no longer reads it" : "Pillar 15 vetoes on it from the next run");
      refresh();
      onClose();
    } catch (err) {
      toast.error("Refused", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-stone-950/40 p-4 sm:items-center" role="presentation" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={titleId} className="w-full max-w-md rounded-3xl bg-white p-6 shadow-soft-lg dark:bg-stone-900 dark:ring-1 dark:ring-white/10">
        <h3 id={titleId} className="text-base font-semibold text-stone-900 dark:text-stone-100">
          {target === "ARCHIVED" ? "Archive" : "Activate"} <span className="font-mono">{rule.rule_code}</span>
        </h3>
        <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
          {target === "ARCHIVED" ? "The lesson stays in the journal; pillar 15 stops reading it." : "Pillar 15 will veto every leg of this shape in a situation this alike."}
        </p>
        <Field label="Reason (kept on the rule)" className="mt-4">
          <TextInput value={reason} maxLength={512} placeholder="e.g. the forecast feed was broken that week" onChange={(e) => setReason(e.target.value)} />
        </Field>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" icon={target === "ARCHIVED" ? "inventory_2" : "block"} busy={busy} disabled={reason.trim().length < 5} onClick={() => void submit()}>
            {target === "ARCHIVED" ? "Archive" : "Activate"}
          </Button>
        </div>
      </div>
    </div>
  );
};

const LessonCard = ({ m, isAdmin, onStatus }: { m: MistakeMemory; isAdmin: boolean; onStatus: (rule: NeverForgetRule, target: "ARCHIVED" | "ACTIVE") => void }) => {
  const rule = m.rule;
  const state = rule ? RULE_STATUS[rule.status] : null;
  const spec = rule?.specificity;
  return (
    <li className="flex min-w-0 flex-col gap-2.5 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/40">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="min-w-0 truncate text-sm font-semibold text-stone-900 dark:text-stone-100">
          {m.home} v {m.away} · {m.market} {m.selection} @ {Number(m.placed_odds).toFixed(2)}
        </span>
        <span className="flex flex-wrap gap-1">
          {rule && <Pill icon="tag">{rule.rule_code}</Pill>}
          {state && <Pill tone={state.tone} icon={state.icon}>{state.label}</Pill>}
          <Pill tone="serious">{ROOT_CAUSE_LABEL[m.loss_root_cause] ?? words(m.loss_root_cause)}</Pill>
        </span>
      </div>
      <p className="text-xs leading-relaxed text-stone-700 dark:text-stone-300">{m.extracted_lesson}</p>
      <div className="flex flex-wrap gap-1">
        {Object.entries(m.situation).map(([k, v]) => (
          <span key={k} className="rounded-full bg-white px-2 py-0.5 font-mono text-[11px] text-stone-600 ring-1 ring-stone-200 dark:bg-stone-900 dark:text-stone-300 dark:ring-stone-700">
            {SITUATION_LABEL[k]?.(v) ?? `${k} ${v}`}
          </span>
        ))}
      </div>
      {rule && <p className="text-[11px] text-stone-500 dark:text-stone-400">{rule.status_reason}</p>}
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-stone-500 dark:text-stone-400">
        <span>
          {m.record.vetoes} veto{m.record.vetoes === 1 ? "" : "es"} · {m.record.lost} went on to lose · {m.record.won} won · held back {rupees(m.record.stake_withheld_inr)}
          {spec?.comparable ? ` · matched ${spec.matched} of ${spec.comparable} recent legs when memorised` : ""} · {formatDateTime(m.created_at)}
        </span>
        {isAdmin && rule && (
          <span className="flex gap-1">
            {rule.status !== "ACTIVE" && (
              <Button size="sm" variant="ghost" icon="block" onClick={() => onStatus(rule, "ACTIVE")}>
                Activate
              </Button>
            )}
            {rule.status !== "ARCHIVED" && (
              <Button size="sm" variant="ghost" icon="inventory_2" onClick={() => onStatus(rule, "ARCHIVED")}>
                Archive
              </Button>
            )}
          </span>
        )}
      </div>
    </li>
  );
};

const OUTCOME_TONE: Record<string, "good" | "critical" | "neutral"> = { LOST: "good", HALF_LOST: "good", WON: "critical", HALF_WON: "critical", VOID: "neutral" };

const VETO_COLUMNS = [
  { key: "leg", header: "Leg turned away", render: (p: Prevention) => <span className="text-xs">{p.home} v {p.away} · {p.market} {p.selection} @ {Number(p.odds).toFixed(2)}</span> },
  { key: "rule", header: "Lesson", render: (p: Prevention) => <span className="font-mono text-xs">{p.rule_code ?? "—"}</span> },
  { key: "sim", header: "Alike", align: "right" as const, render: (p: Prevention) => `${(p.similarity_score * 100).toFixed(0)}%` },
  { key: "stake", header: "Held back", align: "right" as const, render: (p: Prevention) => rupees(p.stake_withheld_inr) },
  {
    key: "outcome",
    header: "How it finished",
    render: (p: Prevention) => (p.outcome ? <Pill tone={OUTCOME_TONE[p.outcome] ?? "neutral"}>{p.outcome === "LOST" || p.outcome === "HALF_LOST" ? `${words(p.outcome)}: avoided` : p.outcome === "VOID" ? "void" : `${words(p.outcome)}: missed`}</Pill> : <span className="text-xs text-stone-400">awaiting the score</span>),
  },
  { key: "when", header: "When", render: (p: Prevention) => <span className="text-xs text-stone-500">{formatDateTime(p.created_at)}</span> },
];

const XP_COLUMNS = [
  { key: "what", header: "Earned for", render: (a: XpAward) => <span className="text-xs">{XP_ACTION_LABEL[a.action_type] ?? words(a.action_type)} · {a.description}</span> },
  { key: "xp", header: "XP", align: "right" as const, render: (a: XpAward) => <span className="font-mono text-emerald-700 dark:text-emerald-300">+{a.xp_amount}</span> },
  { key: "when", header: "When", render: (a: XpAward) => <span className="text-xs text-stone-500">{formatDateTime(a.created_at)}</span> },
];

export const NeverForgetLearningJournal = () => {
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const profile = useXpProfile();
  const stats = useNeverForgetStats();
  const memories = useMistakeMemories();
  const preventions = usePreventions();
  const history = useXpHistory();
  const [tab, setTab] = useState<Tab>("lessons");
  const [changing, setChanging] = useState<{ rule: NeverForgetRule; target: "ARCHIVED" | "ACTIVE" } | null>(null);
  return (
    <Panel
      title="Never-Forget shield"
      icon="shield_lock"
      className="lg:col-span-12"
      subtitle="Pillar 15: lost legs become lessons"
      updatedAt={stats.updatedAt}
    >
      <div className="flex flex-col gap-6">
        <Async resource={profile} skeletonRows={1}>
          {(p) => (
            <div className="flex flex-col gap-3 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/40 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-center gap-3">
                <span className="material-symbols-outlined text-[28px] text-[var(--accent)]">military_tech</span>
                <div>
                  <p className="text-sm font-semibold text-stone-900 dark:text-stone-100">
                    {words(p.rank_title)} · level {p.level}
                  </p>
                  <p className="text-[11px] text-stone-500 dark:text-stone-400">
                    {p.slips_vetted_count} vetted · {p.bets_won_count} won · {p.mistakes_learned_count} lessons · {p.losses_prevented_count} losses shielded · {p.streak_bonuses_count} streaks
                  </p>
                </div>
              </div>
              <div className="w-full sm:w-72">
                <div className="mb-1 flex items-baseline justify-between text-[11px] text-stone-500 dark:text-stone-400">
                  <span>{p.next_rank ? `${(p.next_level_xp ?? 0) - p.total_xp} XP to ${words(p.next_rank)}` : "the top rank"}</span>
                  <span className="font-mono text-sm text-stone-900 dark:text-stone-100">{p.total_xp.toLocaleString("en-IN")} XP</span>
                </div>
                <Meter value={p.progress_pct / 100} tone="good" label="progress to the next rank" />
              </div>
            </div>
          )}
        </Async>

        <Async resource={stats} skeletonRows={1}>
          {(s) => (
            <StatGrid cols={5}>
              <Stat label="Lessons guarding" value={String(s.rules.ACTIVE)} icon="block" hint={`${s.rules.EXPERIMENTAL} shadow · ${s.rules.ARCHIVED} archived`} />
              <Stat label="Legs turned away" value={String(s.fleet.vetoes)} icon="shield" hint={`${s.mine.vetoes} of yours`} />
              <Stat label="Went on to lose" value={String(s.fleet.lost)} icon="trending_down" tone="positive" hint={`of ${s.fleet.resolved} with a score`} />
              <Stat label="Winners missed" value={String(s.fleet.won)} icon="trending_up" tone={s.fleet.won > s.fleet.lost ? "caution" : "neutral"} hint="the price of caution" />
              <Stat label="Held back on losers" value={rupees(s.fleet.stake_withheld_on_losers_inr)} icon="savings" hint={`of ${rupees(s.fleet.stake_withheld_inr)} held back`} />
            </StatGrid>
          )}
        </Async>

        <Segmented<Tab>
          size="sm"
          label="Journal"
          value={tab}
          onChange={setTab}
          options={[
            { value: "lessons", label: "Lessons", icon: "menu_book" },
            { value: "vetoes", label: "Turned away", icon: "shield" },
            { value: "xp", label: "XP earned", icon: "military_tech" },
          ]}
        />

        {tab === "lessons" && (
          <Async
            resource={memories}
            isEmpty={(rows) => rows.length === 0}
            empty={<EmptyState icon="menu_book" title="No lesson yet" detail="When a bet placed from a twin audit loses a leg, the post-mortem memorises the situation the fortress vetted it in, and pillar 15 guards against it." />}
          >
            {(rows) => (
              <ul className="flex flex-col gap-3">
                {rows.map((m) => (
                  <LessonCard key={m.id} m={m} isAdmin={isAdmin} onStatus={(rule, target) => setChanging({ rule, target })} />
                ))}
              </ul>
            )}
          </Async>
        )}
        {tab === "vetoes" && (
          <Async resource={preventions} isEmpty={(rows) => rows.length === 0} empty={<p className="text-xs text-stone-500 dark:text-stone-400">Pillar 15 has turned none of your legs away yet.</p>}>
            {(rows) => <DataTable columns={VETO_COLUMNS} rows={rows} rowKey={(p) => p.id} dense />}
          </Async>
        )}
        {tab === "xp" && (
          <Async resource={history} isEmpty={(rows) => rows.length === 0} empty={<p className="text-xs text-stone-500 dark:text-stone-400">No XP yet: vet a slip through the fortress to earn the first.</p>}>
            {(rows) => <DataTable columns={XP_COLUMNS} rows={rows} rowKey={(a) => a.id} dense />}
          </Async>
        )}

        <Async resource={stats} skeletonRows={0}>
          {(s) => (
            <p className="text-[11px] text-stone-400 dark:text-stone-500">
              {s.enabled ? `Vetoes at ${(s.policy.threshold * 100).toFixed(0)}% alike (exp(−${s.policy.gamma}·D²)), same ${s.policy.scope === "shape" ? "shape of bet" : "any bet"}` : "The shield is switched off"} · lessons are
              shared across the fleet, never whose bet it was · Developer: {s.developer_credit}
            </p>
          )}
        </Async>
        {changing && <StatusDialog rule={changing.rule} target={changing.target} onClose={() => setChanging(null)} />}
      </div>
    </Panel>
  );
};
