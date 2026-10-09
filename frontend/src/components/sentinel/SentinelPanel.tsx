/**
 * Control Panel, The Sentinel (Group 68): global alerting, liveness and remote command.
 *
 * - Watch: Garuda's heartbeat (the dead man's switch), Postgres / Redis / bookmaker API health, the
 *   kill switch, and the spam debouncer's state per channel.
 * - Live alerts: the `/ws/sentinel` feed merged with the alert trail; each alert shows where it went
 *   (sent, batched into a digest, failed, skipped).
 * - Sirens: an HTML5 (WebAudio) wail for the routing rows that list the browser (FATAL by default),
 *   armed with a click because browsers only allow sound after a gesture, and a banner to acknowledge.
 * - Routing matrix: severity (and the 08:00 hype) x channel.
 * - Channel vault: Telegram, Discord, Twilio, PagerDuty. Secrets are write-only (stored encrypted
 *   server-side; the page only learns a hint and whether each one is set).
 * - The 08:00 hype forecast (preview without sending) and the Telegram command log.
 */
import { useEffect, useMemo, useState } from "react";
import { ApiError, apiClient } from "../../api/client";
import { formatAgo, formatDateTime, formatInt, humanize } from "../../lib/format";
import { invalidate } from "../../lib/resource";
import {
  BROWSER,
  CHANNEL_ICON,
  CHANNEL_LABEL,
  ROW_LABEL,
  SEVERITIES,
  siren,
  subscribeSentinel,
  useSentinelAlerts,
  useSentinelChannels,
  useSentinelCommands,
  useSentinelRouting,
  useSentinelStatus,
  type ChannelView,
  type Delivery,
  type HypeResult,
  type RoutingView,
  type SentinelAlert,
  type SentinelStatus,
  type Severity,
} from "../../lib/sentinel";
import { useAuthStore } from "../../store/useAuthStore";
import { toast } from "../../store/useToastStore";
import { Async, Button, ConfirmButton, DataTable, EmptyState, Field, Panel, Pill, Select, Stat, StatGrid, TextInput, Toggle, type Tone } from "../../ui/kit";

const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");
const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const SEVERITY_TONE: Record<Severity, Tone> = { FATAL: "critical", CRITICAL: "serious", WARNING: "warning", INFO: "info" };
const SEVERITY_ICON: Record<Severity, string> = { FATAL: "crisis_alert", CRITICAL: "error", WARNING: "warning", INFO: "info" };
const DELIVERY_TONE: Record<Delivery["status"], Tone> = { SENT: "good", DIGEST: "good", BATCHED: "neutral", FAILED: "critical", SKIPPED: "neutral" };
const FIELD_LABEL: Record<string, string> = {
  bot_token: "Bot token",
  webhook_secret: "Webhook secret token",
  webhook_url: "Webhook URL",
  account_sid: "Account SID",
  auth_token: "Auth token",
  routing_key: "Integration (routing) key",
  chat_ids: "Admin chat ids",
  admin_user_ids: "Admin user ids (optional)",
  mention_on_fatal: "@here on FATAL",
  from_number: "From number (E.164)",
  to_numbers: "Admin phone numbers",
  voice_on_fatal: "Phone call on FATAL",
  source: "Source name",
};
const TIER_TONE: Record<HypeResult["tier"], Tone> = { BUSSIN: "good", PRIMED: "accent", VOLATILE: "warning", QUIET: "neutral" };

// ---------------------------------------------------------------- sirens + the FATAL banner
const useSirens = (routing: Record<string, string[]> | undefined) => {
  const [armed, setArmed] = useState(siren.armed);
  const [sounding, setSounding] = useState(siren.sounding);
  const [alarm, setAlarm] = useState<SentinelAlert | null>(null);
  useEffect(() => siren.onChange(setSounding), []);
  useEffect(
    () =>
      subscribeSentinel((frame) => {
        if (frame.type !== "alert") {
          if (frame.type === "digest" || frame.type === "health") invalidate("sentinel:status", "sentinel:alerts");
          return;
        }
        invalidate("sentinel:alerts", "sentinel:status");
        const loud = (routing?.[frame.alert.severity] ?? (frame.alert.severity === "FATAL" ? [BROWSER] : [])).includes(BROWSER);
        if (loud && !frame.alert.resolves) {
          setAlarm(frame.alert);
          siren.sound();
        }
      }),
    [routing],
  );
  const arm = async () => {
    const ok = await siren.arm();
    setArmed(ok);
    if (!ok) toast.error("Sirens unavailable", "This browser refused to start audio");
  };
  const disarm = () => {
    siren.disarm();
    setArmed(false);
  };
  const acknowledge = () => {
    siren.stop();
    setAlarm(null);
  };
  return { armed, sounding, alarm, arm, disarm, acknowledge, wanted: siren.wanted() };
};

const AlarmBanner = ({ alarm, sounding, onAcknowledge }: { alarm: SentinelAlert; sounding: boolean; onAcknowledge: () => void }) => (
  <div role="alert" className="flex flex-wrap items-center gap-3 rounded-3xl bg-rose-600 px-5 py-4 text-white shadow-soft-lg lg:col-span-12 dark:bg-rose-700">
    <span className={cx("material-symbols-outlined text-[28px]", sounding && "animate-pulse")}>crisis_alert</span>
    <div className="min-w-0 flex-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-rose-100">
        {alarm.severity} · {humanize(alarm.kind)} · {formatDateTime(alarm.occurred_at)}
      </p>
      <p className="font-semibold">{alarm.title}</p>
      {alarm.body && <p className="mt-0.5 line-clamp-2 text-sm text-rose-100">{alarm.body}</p>}
    </div>
    <button type="button" onClick={onAcknowledge} className="rounded-full bg-white px-4 py-2 text-sm font-semibold text-rose-700 hover:bg-rose-50">
      Acknowledge
    </button>
  </div>
);

// ---------------------------------------------------------------- watch
const ageLabel = (seconds: number | null | undefined): string => (seconds === null || seconds === undefined ? "never" : seconds < 90 ? `${seconds.toFixed(0)}s ago` : `${(seconds / 60).toFixed(0)} min ago`);

const Watch = ({ s, isAdmin, sirens }: { s: SentinelStatus; isAdmin: boolean; sirens: ReturnType<typeof useSirens> }) => {
  const [severity, setSeverity] = useState<Severity>("WARNING");
  const [busy, setBusy] = useState<string | null>(null);
  const health = s.health ?? [];
  const down = health.filter((c) => !c.ok);
  const live = s.liveness;
  const emitted = Object.entries(s.stats ?? {}).filter(([k]) => k.startsWith("emitted:")).reduce((a, [, v]) => a + Number(v), 0);
  const run = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try {
      await fn();
      toast.success(ok);
      invalidate("sentinel:status", "sentinel:alerts");
    } catch (err) {
      toast.error("The Sentinel refused", refusal(err));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="flex flex-col gap-6">
      {!s.redis && (
        <p className="flex items-start gap-2 rounded-xl bg-rose-50 px-3 py-2 text-xs text-rose-800 dark:bg-rose-500/10 dark:text-rose-200">
          <span className="material-symbols-outlined text-[16px]">error</span>Redis is unreachable: the alert bus, the heartbeat and the debouncer cannot be read. A Redis outage is still
          delivered to the channels directly.
        </p>
      )}
      <StatGrid cols={4}>
        <Stat
          label="Garuda heartbeat"
          icon="monitor_heart"
          value={live?.status === "SILENT" ? "SILENT" : live?.last_beat_at ? ageLabel(live.age_seconds) : "—"}
          hint={live ? `${live.runner ?? "no runner"} · FATAL after ${live.timeout_seconds}s silent` : "unknown"}
          tone={live?.status === "SILENT" ? "negative" : "neutral"}
        />
        <Stat
          label="Dependencies"
          icon="lan"
          value={health.length ? `${health.length - down.length}/${health.length} up` : "—"}
          hint={down.length ? `down: ${down.map((c) => c.name).join(", ")}` : `checked every ${s.settings.health_interval_seconds}s`}
          tone={down.length ? "negative" : "neutral"}
        />
        <Stat label="Kill switch" icon="power_settings_new" value={s.kill_switch ? "ENGAGED" : s.kill_switch === false ? "Off" : "—"} hint="Telegram /halt engages it" tone={s.kill_switch ? "negative" : "neutral"} />
        <Stat label="Alerts emitted" icon="notifications" value={formatInt(emitted)} hint={`${formatInt(s.stream_length ?? 0)} on the stream`} />
      </StatGrid>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div>
          <h3 className="mb-2 text-sm font-semibold text-stone-800 dark:text-stone-100">Dependency health</h3>
          {health.length === 0 ? (
            <p className="text-xs text-stone-500 dark:text-stone-400">Not checked yet: the first pass runs within {s.settings.health_interval_seconds}s.</p>
          ) : (
            <ul className="flex flex-col divide-y divide-stone-100 dark:divide-stone-800">
              {health.map((c) => (
                <li key={c.name} className="flex items-start justify-between gap-3 py-2">
                  <div className="min-w-0">
                    <p className="truncate font-mono text-xs text-stone-800 dark:text-stone-200">{c.name}</p>
                    <p className="truncate text-[11px] text-stone-500 dark:text-stone-400" title={c.detail}>
                      {c.detail}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {c.latency_ms !== null && <span className="font-mono text-[11px] text-stone-400">{c.latency_ms.toFixed(0)} ms</span>}
                    <Pill tone={c.ok ? "good" : "critical"} icon={c.ok ? "check_circle" : "cancel"}>
                      {c.ok ? "Up" : "Down"}
                    </Pill>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[11px] text-stone-400 dark:text-stone-500">Bookmaker APIs are judged from the ingestion fleet's own record: no request, no quota.</p>
        </div>
        <div className="flex flex-col gap-4">
          <div>
            <h3 className="mb-2 text-sm font-semibold text-stone-800 dark:text-stone-100">Spam debouncer</h3>
            <p className="mb-2 text-[11px] text-stone-500 dark:text-stone-400">At most one CRITICAL per channel every {s.settings.debounce_seconds}s; the rest leave together as one digest. FATAL never waits.</p>
            {Object.keys(s.debounce ?? {}).length === 0 ? (
              <p className="text-xs text-stone-500 dark:text-stone-400">No CRITICAL alert has been debounced yet.</p>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {Object.entries(s.debounce ?? {}).map(([channel, d]) => (
                  <li key={channel} className="flex items-center justify-between gap-3 text-xs">
                    <span className="text-stone-700 dark:text-stone-300">{CHANNEL_LABEL[channel] ?? channel}</span>
                    <span className="font-mono text-stone-500 dark:text-stone-400">
                      {d.holding ? `${d.holding} held · digest ${formatAgo(d.window_closes)}` : "idle"} · {d.sent} sent · {d.digests} digest(s)
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/50">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-stone-800 dark:text-stone-100">Browser sirens</p>
                <p className="text-[11px] text-stone-500 dark:text-stone-400">
                  {sirens.armed ? "Armed: rows routed to the browser wail here." : sirens.wanted ? "Click to re-arm (browsers need a click after a reload)." : "Off."}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {sirens.armed && (
                  <Button size="sm" variant="ghost" icon="volume_up" onClick={() => (siren.sounding ? siren.stop() : siren.sound(3))}>
                    {sirens.sounding ? "Stop" : "Test"}
                  </Button>
                )}
                <Toggle label="Arm sirens" checked={sirens.armed} onChange={(next) => void (next ? sirens.arm() : sirens.disarm())} />
              </div>
            </div>
          </div>
          {isAdmin && (
            <div className="flex flex-wrap items-end gap-2">
              <Button size="sm" icon="stethoscope" busy={busy === "check"} onClick={() => void run("check", () => apiClient.post("/sentinel/check"), "Checks ran")}>
                Run checks now
              </Button>
              <Select aria-label="Test alert severity" value={severity} onChange={(e) => setSeverity(e.target.value as Severity)} className="!w-auto !py-1.5 text-xs">
                {SEVERITIES.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </Select>
              <Button size="sm" icon="science" busy={busy === "test"} onClick={() => void run("test", () => apiClient.post("/sentinel/test-alert", { severity }), `Test ${severity.toLowerCase()} alert sent`)}>
                Send test alert
              </Button>
            </div>
          )}
        </div>
      </div>
      <p className="text-xs text-stone-500 dark:text-stone-400">
        Whale order from ₹{formatInt(Number(s.settings.whale_stake_inr))} · margin call at {(Number(s.settings.margin_utilisation) * 100).toFixed(0)}% of equity at risk · flash crashes only on
        prices at {(s.settings.flash_min_probability * 100).toFixed(0)}% or more (odds ≤ {(1 / s.settings.flash_min_probability).toFixed(1)}) · hype at {s.settings.hype_at}
      </p>
    </div>
  );
};

// ---------------------------------------------------------------- live alerts
const AlertRow = ({ a }: { a: SentinelAlert }) => {
  const [open, setOpen] = useState(false);
  return (
    <li className="py-2.5">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-start gap-3 text-left">
        <Pill tone={a.resolves ? "good" : SEVERITY_TONE[a.severity]} icon={a.resolves ? "check_circle" : SEVERITY_ICON[a.severity]}>
          {a.resolves ? "Resolved" : a.severity}
        </Pill>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-stone-800 dark:text-stone-100">{a.title}</span>
          <span className="block truncate text-[11px] text-stone-500 dark:text-stone-400">
            {humanize(a.kind)} · {a.source} · {formatDateTime(a.occurred_at)}
          </span>
        </span>
        <span className="flex shrink-0 flex-wrap justify-end gap-1">
          {(a.deliveries ?? []).filter((d) => d.status !== "SKIPPED").map((d, i) => (
            <Pill key={`${d.channel}-${i}`} tone={DELIVERY_TONE[d.status]}>
              {CHANNEL_LABEL[d.channel]?.split(" ")[0] ?? d.channel} {d.status.toLowerCase()}
            </Pill>
          ))}
        </span>
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-2 rounded-2xl bg-stone-50 p-3 text-xs dark:bg-stone-800/50">
          {a.body && <p className="whitespace-pre-wrap text-stone-700 dark:text-stone-300">{a.body}</p>}
          {(a.deliveries ?? []).length > 0 && (
            <ul className="flex flex-col gap-1">
              {(a.deliveries ?? []).map((d, i) => (
                <li key={i} className="flex flex-wrap justify-between gap-2 font-mono text-[11px] text-stone-500 dark:text-stone-400">
                  <span>
                    {d.channel} · {d.status}
                    {d.latency_ms !== null ? ` · ${d.latency_ms} ms` : ""}
                  </span>
                  <span className="truncate">{d.error ?? (d.digest_id ? `digest ${d.digest_id.slice(0, 8)}` : formatDateTime(d.attempted_at))}</span>
                </li>
              ))}
            </ul>
          )}
          {a.dedupe_key && <p className="font-mono text-[11px] text-stone-400">incident {a.dedupe_key}</p>}
        </div>
      )}
    </li>
  );
};

const LiveAlerts = () => {
  const [severity, setSeverity] = useState<Severity | "">("");
  const alerts = useSentinelAlerts(severity);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-1.5">
        {(["", ...SEVERITIES] as const).map((v) => (
          <button
            key={v || "all"}
            type="button"
            onClick={() => setSeverity(v)}
            className={cx(
              "rounded-full px-3 py-1 text-xs font-medium",
              severity === v ? "bg-stone-900 text-white dark:bg-white dark:text-stone-900" : "bg-stone-100 text-stone-600 hover:bg-stone-200 dark:bg-stone-800 dark:text-stone-300",
            )}
          >
            {v ? ROW_LABEL[v] : "All"}
          </button>
        ))}
      </div>
      <Async resource={alerts} skeletonRows={5} isEmpty={(rows) => rows.length === 0} empty={<EmptyState icon="notifications_off" title="No alerts yet" detail="Alerts appear here the moment they are raised." />}>
        {(rows) => (
          <ul className="flex max-h-[34rem] flex-col divide-y divide-stone-100 overflow-y-auto pr-1 dark:divide-stone-800">
            {rows.map((a) => (
              <AlertRow key={a.id} a={a} />
            ))}
          </ul>
        )}
      </Async>
    </div>
  );
};

// ---------------------------------------------------------------- routing matrix
const RoutingMatrix = ({ view, isAdmin }: { view: RoutingView; isAdmin: boolean }) => {
  const [matrix, setMatrix] = useState(view.matrix);
  const [busy, setBusy] = useState(false);
  const dirty = JSON.stringify(matrix) !== JSON.stringify(view.matrix);
  const flip = (row: string, column: string) =>
    setMatrix((m) => {
      const has = (m[row] ?? []).includes(column);
      return { ...m, [row]: has ? m[row].filter((c) => c !== column) : view.columns.filter((c) => c === column || (m[row] ?? []).includes(c)) };
    });
  const save = async () => {
    setBusy(true);
    try {
      await apiClient.put("/sentinel/routing", { matrix });
      toast.success("Routing saved");
      invalidate("sentinel:routing", "sentinel:status");
    } catch (err) {
      toast.error("Routing refused", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-3">
      <div className="-mx-1 overflow-x-auto">
        <table className="w-full min-w-[300px] table-fixed text-xs">
          <thead>
            <tr>
              <th className="w-[24%] px-1 pb-2 text-left font-medium text-stone-400" scope="col">
                Alert
              </th>
              {view.columns.map((c) => (
                <th key={c} scope="col" title={CHANNEL_LABEL[c] ?? c} className="px-0.5 pb-2 text-center text-[10px] font-medium text-stone-400">
                  <span className="material-symbols-outlined block text-[16px]" aria-hidden>
                    {CHANNEL_ICON[c]}
                  </span>
                  {CHANNEL_LABEL[c]?.split(" ")[0] ?? c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.rows.map((row) => (
              <tr key={row} className="border-t border-stone-100 dark:border-stone-800">
                <th scope="row" className="truncate px-1 py-2 text-left font-medium text-stone-700 dark:text-stone-200">
                  {ROW_LABEL[row] ?? row}
                </th>
                {view.columns.map((c) => (
                  <td key={c} className="px-1 py-2 text-center">
                    <input
                      type="checkbox"
                      aria-label={`${ROW_LABEL[row] ?? row} to ${CHANNEL_LABEL[c] ?? c}`}
                      checked={(matrix[row] ?? []).includes(c)}
                      disabled={!isAdmin}
                      onChange={() => flip(row, c)}
                      className="size-4 accent-[var(--accent)]"
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {isAdmin && (
        <div className="flex gap-2">
          <Button size="sm" variant="primary" icon="save" disabled={!dirty} busy={busy} onClick={() => void save()}>
            Save routing
          </Button>
          {dirty && (
            <Button size="sm" variant="ghost" onClick={() => setMatrix(view.matrix)}>
              Discard
            </Button>
          )}
        </div>
      )}
      <p className="text-[11px] text-stone-400 dark:text-stone-500">The browser column sounds this tab's siren. CRITICAL is debounced per channel; FATAL always goes at once.</p>
    </div>
  );
};

// ---------------------------------------------------------------- the channel vault
const listText = (value: unknown): string => (Array.isArray(value) ? value.join(", ") : "");

const ChannelCard = ({ c }: { c: ChannelView }) => {
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [config, setConfig] = useState<Record<string, unknown>>(c.config);
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try {
      await fn();
      toast.success(ok);
      invalidate("sentinel:channels", "sentinel:status");
      return true;
    } catch (err) {
      toast.error(`${CHANNEL_LABEL[c.channel]}: refused`, refusal(err));
      return false;
    } finally {
      setBusy(null);
    }
  };
  const saveSecrets = async () => {
    const values = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v.trim()));
    if (await act("secrets", () => apiClient.put(`/sentinel/channels/${c.channel}/credentials`, { values }), "Secrets stored, encrypted")) setSecrets({});
  };
  const saveConfig = () => {
    const body: Record<string, unknown> = {};
    for (const [name, kind] of Object.entries(c.config_fields)) {
      const value = config[name];
      if (kind === "list") body[name] = typeof value === "string" ? value.split(/[\s,]+/).filter(Boolean) : (value ?? []);
      else if (kind === "bool") body[name] = Boolean(value);
      else body[name] = value ?? "";
    }
    void act("config", () => apiClient.put(`/sentinel/channels/${c.channel}`, { config: body }), "Settings saved");
  };
  const test = async () => {
    setBusy("test");
    try {
      const r = await apiClient.post<{ ok: boolean; error: string | null; latency_ms: number; deliveries: number }>(`/sentinel/channels/${c.channel}/test`);
      (r.ok ? toast.success : toast.error)(`${CHANNEL_LABEL[c.channel]} test ${r.ok ? "delivered" : "failed"}`, r.ok ? `${r.deliveries} delivery(ies) in ${r.latency_ms} ms` : (r.error ?? undefined));
      invalidate("sentinel:channels");
    } catch (err) {
      toast.error(`${CHANNEL_LABEL[c.channel]} test refused`, refusal(err));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="flex min-w-0 flex-col gap-4 rounded-3xl bg-stone-50 p-5 dark:bg-stone-800/40">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="material-symbols-outlined grid size-9 place-items-center rounded-2xl bg-white text-[18px] text-stone-500 shadow-soft dark:bg-stone-800 dark:text-stone-300">{CHANNEL_ICON[c.channel]}</span>
          <div className="min-w-0">
            <p className="font-semibold text-stone-900 dark:text-stone-100">{CHANNEL_LABEL[c.channel]}</p>
            <p className="truncate text-[11px] text-stone-500 dark:text-stone-400">{c.credentials_hint ?? "no credentials"}</p>
          </div>
        </div>
        <Toggle label={`Enable ${CHANNEL_LABEL[c.channel]}`} checked={c.enabled} disabled={busy !== null} onChange={(next) => void act("enable", () => apiClient.put(`/sentinel/channels/${c.channel}`, { enabled: next }), next ? "Enabled" : "Disabled")} />
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Pill tone={c.ready ? "good" : c.enabled ? "warning" : "neutral"} icon={c.ready ? "check_circle" : "pending"}>
          {c.ready ? "Ready" : c.enabled ? "Not ready" : "Off"}
        </Pill>
        {c.last_success_at && <Pill tone="neutral">last sent {formatAgo(c.last_success_at)}</Pill>}
      </div>
      {c.problem && c.enabled && <p className="text-[11px] text-amber-700 dark:text-amber-300">{c.problem}</p>}
      {c.last_error && <p className="truncate text-[11px] text-rose-600 dark:text-rose-300" title={c.last_error}>Last error {formatAgo(c.last_error_at)}: {c.last_error}</p>}

      <div className="flex flex-col gap-3">
        {c.credential_fields.map((name) => (
          <Field key={name} label={FIELD_LABEL[name] ?? humanize(name)} hint={c.credentials_set[name] ? "set · type to replace" : "not set"}>
            <TextInput
              type="password"
              autoComplete="new-password"
              placeholder={c.credentials_set[name] ? "••••••••" : ""}
              value={secrets[name] ?? ""}
              onChange={(e) => setSecrets((s) => ({ ...s, [name]: e.target.value }))}
            />
          </Field>
        ))}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="primary" icon="lock" disabled={!Object.values(secrets).some((v) => v.trim())} busy={busy === "secrets"} onClick={() => void saveSecrets()}>
            Store secrets
          </Button>
          {Object.values(c.credentials_set).some(Boolean) && (
            <ConfirmButton size="sm" variant="danger" icon="delete" confirmLabel="Wipe them?" onConfirm={() => void act("wipe", () => apiClient.delete(`/sentinel/channels/${c.channel}/credentials`), "Secrets wiped")}>
              Wipe
            </ConfirmButton>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-3 border-t border-stone-200/70 pt-4 dark:border-stone-700/60">
        {Object.entries(c.config_fields).map(([name, kind]) =>
          kind === "bool" ? (
            <div key={name} className="flex items-center justify-between gap-3">
              <span className="text-xs font-medium text-stone-500 dark:text-stone-400">{FIELD_LABEL[name] ?? humanize(name)}</span>
              <Toggle label={FIELD_LABEL[name] ?? name} checked={Boolean(config[name] ?? (name === "voice_on_fatal"))} onChange={(next) => setConfig((s) => ({ ...s, [name]: next }))} />
            </div>
          ) : (
            <Field key={name} label={FIELD_LABEL[name] ?? humanize(name)} hint={kind === "list" ? "comma separated" : undefined}>
              <TextInput value={kind === "list" ? (typeof config[name] === "string" ? (config[name] as string) : listText(config[name])) : String(config[name] ?? "")} onChange={(e) => setConfig((s) => ({ ...s, [name]: e.target.value }))} />
            </Field>
          ),
        )}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" icon="save" busy={busy === "config"} onClick={saveConfig}>
            Save settings
          </Button>
          <Button size="sm" variant="ghost" icon="send" disabled={!c.ready} busy={busy === "test"} onClick={() => void test()}>
            Send test
          </Button>
        </div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------- hype + Telegram
const Hype = ({ last }: { last: HypeResult | null | undefined }) => {
  const [preview, setPreview] = useState<HypeResult | null>(null);
  const [busy, setBusy] = useState(false);
  const shown = preview ?? last ?? null;
  const run = async () => {
    setBusy(true);
    try {
      setPreview(await apiClient.post<HypeResult>("/sentinel/hype/preview"));
    } catch (err) {
      toast.error("Forecast unavailable", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-4">
      {shown ? (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={TIER_TONE[shown.tier]} icon="rocket_launch">
              {shown.tier}
            </Pill>
            <span className="text-[11px] text-stone-500 dark:text-stone-400">
              {preview ? "preview, not sent" : shown.sent ? `sent ${formatAgo(shown.at)}` : shown.at ? `checked ${formatAgo(shown.at)}, not sent` : ""}
            </span>
          </div>
          <p className="text-base font-semibold text-stone-900 dark:text-stone-100">{shown.title ?? "A quiet day: nothing worth a push."}</p>
          <p className="whitespace-pre-wrap text-xs text-stone-600 dark:text-stone-300">
            {shown.body ??
              `${shown.forecast.fixtures_today} fixtures · ${shown.forecast.live_edges} live edges · total EV +${shown.forecast.total_ev_pct}% · ${shown.forecast.steam_moves} steam moves`}
          </p>
        </div>
      ) : (
        <p className="text-xs text-stone-500 dark:text-stone-400">No forecast yet today.</p>
      )}
      <div>
        <Button size="sm" icon="visibility" busy={busy} onClick={() => void run()}>
          Preview today's forecast
        </Button>
      </div>
    </div>
  );
};

const Telegram = () => {
  const commands = useSentinelCommands(true);
  const webhook = `${window.location.origin}/api/v1/sentinel/webhook/telegram`;
  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-2xl bg-stone-50 p-4 text-xs leading-relaxed text-stone-600 dark:bg-stone-800/50 dark:text-stone-300">
        <p className="mb-1 font-semibold text-stone-800 dark:text-stone-100">Two-way commands</p>
        <p>
          Point the bot's webhook (<code>setWebhook</code>) at <code className="break-all">{webhook}</code> on a public HTTPS host, with <code>secret_token</code> set to the webhook secret stored
          above. Only the admin chat ids (and admin user ids, when set) can command: <code>/status</code>, <code>/halt</code>, <code>/resume</code>.
        </p>
      </div>
      <Async resource={commands} skeletonRows={2} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="chat" title="No commands yet" />}>
        {(rows) => (
          <div className="max-h-72 overflow-y-auto">
            <DataTable
              dense
              rows={rows}
              rowKey={(r) => String(r.id)}
              columns={[
                { key: "t", header: "When", render: (r) => <span className="whitespace-nowrap text-xs">{formatDateTime(r.received_at)}</span> },
                { key: "c", header: "Command", render: (r) => <span className="font-mono text-xs">{r.command}</span> },
                { key: "s", header: "From", render: (r) => <span className="text-xs">{r.sender ?? r.chat_id ?? "—"}</span> },
                {
                  key: "o",
                  header: "Outcome",
                  render: (r) => (
                    <Pill tone={!r.authorised ? "neutral" : r.outcome === "HALTED" ? "critical" : r.outcome === "RESUMED" ? "good" : "info"}>{r.authorised ? r.outcome : "ignored"}</Pill>
                  ),
                },
              ]}
            />
          </div>
        )}
      </Async>
    </div>
  );
};

// ---------------------------------------------------------------- the tab
export const SentinelPanel = () => {
  const status = useSentinelStatus();
  const routing = useSentinelRouting();
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const channels = useSentinelChannels(isAdmin);
  const sirens = useSirens(routing.data?.matrix);
  const ready = useMemo(() => (status.data?.channels ?? []).filter((c) => c.ready).length, [status.data?.channels]);
  return (
    <>
      {sirens.alarm && <AlarmBanner alarm={sirens.alarm} sounding={sirens.sounding} onAcknowledge={sirens.acknowledge} />}
      <Panel title="The Sentinel · watch" icon="shield" className="lg:col-span-12" subtitle={`liveness, dependencies, the debouncer · ${ready} channel(s) ready`} updatedAt={status.updatedAt}>
        <Async resource={status} skeletonRows={4}>{(s) => <Watch s={s} isAdmin={isAdmin} sirens={sirens} />}</Async>
      </Panel>
      <Panel title="Alerts" icon="notifications_active" className="lg:col-span-7" subtitle="live over the Sentinel socket, with where each went">
        <LiveAlerts />
      </Panel>
      <Panel title="Routing matrix" icon="alt_route" className="lg:col-span-5" subtitle="which channels hear what">
        <Async resource={routing} skeletonRows={4}>{(view) => <RoutingMatrix key={JSON.stringify(view.matrix)} view={view} isAdmin={isAdmin} />}</Async>
      </Panel>
      {isAdmin && (
        <Panel title="Channel vault" icon="key" className="lg:col-span-12" subtitle="secrets are encrypted server-side and never shown again">
          <Async resource={channels} skeletonRows={4}>
            {(rows) => (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-4">
                {rows.map((c) => (
                  <ChannelCard key={`${c.channel}:${JSON.stringify(c.config)}`} c={c} />
                ))}
              </div>
            )}
          </Async>
        </Panel>
      )}
      {isAdmin && (
        <Panel title="08:00 market hype" icon="rocket_launch" className="lg:col-span-5" subtitle={status.data ? `daily at ${status.data.settings.hype_at}` : undefined}>
          <Hype last={status.data?.hype} />
        </Panel>
      )}
      {isAdmin && (
        <Panel title="Telegram command center" icon="smart_toy" className="lg:col-span-7" subtitle="/halt · /resume · /status">
          <Telegram />
        </Panel>
      )}
    </>
  );
};
