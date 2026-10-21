/**
 * Group 92: BHEESHMA's API rate-limit intelligence & quota guard. Architect: Amit Ashok Kumar Patnaik.
 * Token buckets, measured provider quota and breakers for every fleet source and Omni router provider, and the
 * quota cache shield: while it is engaged the fleet beat polls a metered source every interval x multiplier and
 * the desk serves the stored odds in between. Under the fleet's quota reserve the reserve always wins.
 * A provider that never sent quota headers reads "unmetered"; nothing is estimated.
 */
import { useState } from 'react';
import { apiClient } from '../../api/client';
import { formatAgo } from '../../lib/format';
import { runMutation, useResource } from '../../lib/resource';
import { useAuthStore } from '../../store/useAuthStore';
import { Async, ConfirmButton, EmptyState, Panel, Pill, Segmented, type Tone } from '../../ui/kit';

type GuardState = 'HEALTHY' | 'WARNING_CACHE_SHIELD' | 'CRITICAL_LOCKED' | 'UNMETERED';
type PollingMode = 'LIVE_FULL' | 'CACHE_SHIELD' | 'RESERVE_FAILOVER' | 'BREAKER_HOLD' | 'NOT_POLLING';
type ShieldMode = 'AUTO' | 'FORCE_CACHE_SHIELD' | 'LIVE_UNRESTRICTED';

interface ProviderQuota {
  source_id: string;
  display_name: string;
  kind: 'fleet' | 'router';
  cost: string | null;
  category: string | null;
  is_enabled: boolean;
  schedule_state: string;
  breaker_state: 'closed' | 'open' | 'half_open';
  breaker_remaining_seconds: number | null;
  requests_per_minute: number;
  burst_capacity: number;
  tokens_available: number | null;
  refill_per_second: number;
  bucket_utilization_pct: number | null;
  is_throttled: boolean;
  quota_remaining: number | null;
  quota_used: number | null;
  quota_limit: number | null;
  remaining_pct: number | null;
  used_pct: number | null;
  guard_state: GuardState;
  polling_mode: PollingMode;
  shield_engaged: boolean;
  base_interval_seconds: number | null;
  effective_interval_seconds: number | null;
  consecutive_failures: number;
  last_latency_ms: number | null;
  verdict_summary: string;
}

interface QuotaGuardDashboard {
  global_rpm_limit: number;
  redis_limiter_enforcing: boolean;
  shield: { mode: ShieldMode; set_by: string | null; set_at: string | null; reason: string | null };
  cache_shield_active: boolean;
  warn_fraction_pct: number;
  reserve_fraction_pct: number;
  shield_interval_multiplier: number;
  total_sources: number;
  healthy_sources: number;
  warning_sources: number;
  critical_sources: number;
  unmetered_sources: number;
  throttled_sources: number;
  shielded_sources: number;
  overall_quota_health_pct: number;
  excluded_sources: string[];
  architect: string;
  providers: ProviderQuota[];
}

const RESOURCE = 'control-panel:quota-guard';

const SHIELD_OPTIONS = [
  { value: 'AUTO', label: 'Auto', icon: 'auto_mode' },
  { value: 'FORCE_CACHE_SHIELD', label: 'Force shield', icon: 'shield' },
  { value: 'LIVE_UNRESTRICTED', label: 'Live unrestricted', icon: 'bolt' },
] as const satisfies readonly { value: ShieldMode; label: string; icon: string }[];

const GUARD: Record<GuardState, { tone: Tone; label: string }> = {
  HEALTHY: { tone: 'good', label: 'Healthy' },
  WARNING_CACHE_SHIELD: { tone: 'warning', label: 'Low quota' },
  CRITICAL_LOCKED: { tone: 'critical', label: 'Locked' },
  UNMETERED: { tone: 'neutral', label: 'Unmetered' },
};

const POLLING: Record<PollingMode, { tone: Tone; label: string }> = {
  LIVE_FULL: { tone: 'info', label: 'Live cadence' },
  CACHE_SHIELD: { tone: 'accent', label: 'Cache shield' },
  RESERVE_FAILOVER: { tone: 'critical', label: 'Reserve · failover' },
  BREAKER_HOLD: { tone: 'serious', label: 'Breaker hold' },
  NOT_POLLING: { tone: 'neutral', label: 'Not polling' },
};

const seconds = (s: number): string => (s >= 3600 ? `${(s / 3600).toFixed(1)}h` : s >= 60 ? `${Math.round(s / 60)}m` : `${Math.round(s)}s`);
const count = (n: number | null): string => (n === null ? '—' : Number.isInteger(n) ? n.toLocaleString('en-IN') : n.toFixed(1));

export const QuotaGuardPanel = () => {
  const guard = useResource(RESOURCE, () => apiClient.get<QuotaGuardDashboard>('/control-panel/quota-guard'), { intervalMs: 20_000 });
  const isAdmin = useAuthStore((s) => s.user?.role === 'ADMIN');
  const [busy, setBusy] = useState<string | null>(null);

  const setMode = async (mode: ShieldMode) => {
    setBusy('mode');
    try {
      await runMutation(() => apiClient.post<QuotaGuardDashboard>('/control-panel/quota-guard/mode', { shield_mode: mode }), {
        invalidate: [RESOURCE],
        success: (r) => `Quota shield: ${SHIELD_OPTIONS.find((o) => o.value === r.shield.mode)?.label ?? r.shield.mode}`,
        errorTitle: 'Shield mode not changed',
      });
    } finally {
      setBusy(null);
    }
  };

  const reset = async (sourceId: string) => {
    setBusy(sourceId);
    try {
      await runMutation(() => apiClient.post<QuotaGuardDashboard>('/control-panel/quota-guard/reset-bucket', { source_id: sourceId }), {
        invalidate: [RESOURCE, 'fleet'],
        success: `${sourceId}: breaker closed, bucket refilled`,
        errorTitle: 'Bucket not reset',
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel
      title="API rate-limit intelligence & quota guard"
      subtitle="BHEESHMA · token buckets & quota cache shield"
      icon="shield_lock"
      className="lg:col-span-12"
      updatedAt={guard.updatedAt}
    >
      <Async resource={guard} skeletonRows={4}>
        {(d) => (
          <div className="flex flex-col gap-5">
            {/* Summary strip */}
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
                <p className="text-xs text-stone-500 dark:text-stone-400">Global API limiter</p>
                <p className="mt-1 font-display text-xl font-semibold tabular-nums text-stone-900 dark:text-stone-100">{count(d.global_rpm_limit)} RPM</p>
                <Pill className="mt-2" tone={d.redis_limiter_enforcing ? 'good' : 'warning'} icon={d.redis_limiter_enforcing ? 'check_circle' : 'warning'}>
                  {d.redis_limiter_enforcing ? 'Enforcing (Redis)' : 'Failing open: Redis unreachable'}
                </Pill>
              </div>

              <div className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs text-stone-500 dark:text-stone-400">Quota cache shield</p>
                  <Pill tone={d.cache_shield_active ? 'accent' : 'neutral'} icon="shield">
                    {d.cache_shield_active ? `Active · ${d.shielded_sources} source${d.shielded_sources === 1 ? '' : 's'}` : 'Standby'}
                  </Pill>
                </div>
                <div className={`mt-2 ${busy === 'mode' ? 'pointer-events-none opacity-60' : ''}`} aria-busy={busy === 'mode' || undefined}>
                  {isAdmin ? (
                    <Segmented<ShieldMode> size="sm" options={SHIELD_OPTIONS} value={d.shield.mode} onChange={(m) => void setMode(m)} label="Quota cache shield mode" />
                  ) : (
                    <Pill tone="neutral">{SHIELD_OPTIONS.find((o) => o.value === d.shield.mode)?.label} · admins change this</Pill>
                  )}
                </div>
                {d.shield.set_by && d.shield.set_at && (
                  <p className="mt-2 truncate text-[11px] text-stone-400" title={d.shield.reason ?? undefined}>
                    Set by {d.shield.set_by} {formatAgo(d.shield.set_at)}
                  </p>
                )}
              </div>

              <div className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
                <p className="text-xs text-stone-500 dark:text-stone-400">Fleet quota health</p>
                <p className="mt-1 font-display text-xl font-semibold tabular-nums text-stone-900 dark:text-stone-100">{d.overall_quota_health_pct}%</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Pill tone={d.healthy_sources ? 'good' : 'neutral'}>{d.healthy_sources} healthy</Pill>
                  <Pill tone={d.warning_sources ? 'warning' : 'neutral'}>{d.warning_sources} low</Pill>
                  <Pill tone={d.critical_sources ? 'critical' : 'neutral'}>{d.critical_sources} locked</Pill>
                  <Pill tone="neutral">{d.unmetered_sources} unmetered</Pill>
                  {d.throttled_sources > 0 && <Pill tone="serious">{d.throttled_sources} throttled</Pill>}
                </div>
              </div>
            </div>

            <p className="text-xs leading-relaxed text-stone-500 dark:text-stone-400">
              Metered sources at or under {d.warn_fraction_pct}% quota (or all of them, when forced) are polled every interval ×{' '}
              {d.shield_interval_multiplier}; the desk serves stored odds in between. Under the {d.reserve_fraction_pct}% reserve a source stops
              polling and its markets fail over, whatever the mode. Free sources are never held back.
            </p>

            {/* Providers */}
            {d.providers.length === 0 ? (
              <EmptyState icon="hub" title="No data sources" detail="Fleet sources and Omni router providers appear here once configured." />
            ) : (
              <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {d.providers.map((p) => (
                  <ProviderCard key={`${p.kind}:${p.source_id}`} p={p} canReset={isAdmin && p.kind === 'fleet'} busy={busy === p.source_id} onReset={() => void reset(p.source_id)} />
                ))}
              </ul>
            )}

            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-stone-400">
              <span>{d.excluded_sources.length > 0 ? `Retired feeds hidden: ${d.excluded_sources.join(', ')}` : 'No retired feeds hidden'}</span>
              <span>Architect: {d.architect}</span>
            </div>
          </div>
        )}
      </Async>
    </Panel>
  );
};

const ProviderCard = ({ p, canReset, busy, onReset }: { p: ProviderQuota; canReset: boolean; busy: boolean; onReset: () => void }) => {
  const guard = GUARD[p.guard_state];
  const polling = POLLING[p.polling_mode];
  const bucketPct = p.tokens_available !== null && p.burst_capacity > 0 ? (p.tokens_available / p.burst_capacity) * 100 : null;

  return (
    <li className="flex flex-col gap-3 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-stone-800 dark:text-stone-100" title={p.source_id}>{p.display_name}</p>
          <p className="text-[11px] text-stone-400">
            {p.kind === 'fleet' ? `Fleet · ${p.cost ?? 'unknown cost'}` : `Router · category ${p.category ?? '—'}`} · {count(p.requests_per_minute)} RPM
          </p>
        </div>
        <Pill tone={guard.tone}>{guard.label}</Pill>
      </div>

      {p.remaining_pct !== null ? (
        <div>
          <div className="flex justify-between text-[11px] text-stone-500 dark:text-stone-400">
            <span>Quota left</span>
            <span className="tabular-nums">
              {p.remaining_pct.toFixed(1)}%{p.quota_remaining !== null && p.quota_limit !== null ? ` · ${count(p.quota_remaining)}/${count(p.quota_limit)}` : ''}
            </span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-stone-200 dark:bg-white/[0.08]" role="meter" aria-valuenow={p.remaining_pct} aria-valuemin={0} aria-valuemax={100} aria-label={`${p.display_name} quota remaining`}>
            <div
              className={`h-full rounded-full ${p.guard_state === 'CRITICAL_LOCKED' ? 'bg-rose-400' : p.guard_state === 'WARNING_CACHE_SHIELD' ? 'bg-amber-400' : 'bg-emerald-400'}`}
              style={{ width: `${Math.max(0, Math.min(100, p.remaining_pct))}%` }}
            />
          </div>
        </div>
      ) : (
        <p className="text-[11px] text-stone-400">Quota: unmetered (no quota headers received)</p>
      )}

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
        <dt className="text-stone-400">{p.kind === 'fleet' ? 'Bucket' : 'This minute'}</dt>
        <dd className="text-right tabular-nums text-stone-700 dark:text-stone-200" title={bucketPct !== null ? `${bucketPct.toFixed(0)}% available` : 'Not readable while Redis is unreachable'}>
          {p.tokens_available === null ? '—' : `${count(p.tokens_available)} / ${p.burst_capacity}`}
          {p.is_throttled && <span className="ml-1 text-rose-500">· saturated</span>}
        </dd>
        {p.base_interval_seconds !== null && (
          <>
            <dt className="text-stone-400">Cadence</dt>
            <dd className="text-right tabular-nums text-stone-700 dark:text-stone-200">
              {p.effective_interval_seconds !== null && p.effective_interval_seconds !== p.base_interval_seconds
                ? `${seconds(p.base_interval_seconds)} → ${seconds(p.effective_interval_seconds)}`
                : seconds(p.base_interval_seconds)}
            </dd>
          </>
        )}
        <dt className="text-stone-400">Breaker</dt>
        <dd className="text-right text-stone-700 dark:text-stone-200">
          {p.breaker_state === 'open' && p.breaker_remaining_seconds !== null ? `open · ${seconds(p.breaker_remaining_seconds)}` : p.breaker_state.replace('_', '-')}
          {p.consecutive_failures > 0 && <span className="ml-1 text-stone-400">· {p.consecutive_failures} fail</span>}
        </dd>
        {p.last_latency_ms !== null && (
          <>
            <dt className="text-stone-400">Last run</dt>
            <dd className="text-right tabular-nums text-stone-700 dark:text-stone-200">{p.last_latency_ms} ms</dd>
          </>
        )}
      </dl>

      <div className="mt-auto flex items-center justify-between gap-2">
        <Pill tone={polling.tone}>{polling.label}</Pill>
        {canReset && (
          <ConfirmButton size="sm" variant="ghost" icon="restart_alt" busy={busy} onConfirm={onReset} confirmLabel="Reset now?">
            Reset bucket
          </ConfirmButton>
        )}
      </div>
      <p className="text-[11px] leading-snug text-stone-500 dark:text-stone-400">{p.verdict_summary}</p>
    </li>
  );
};
