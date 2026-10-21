/**
 * Group 86: the emergency fortress (KAUTILYA, CHANAKYA & SHIVAJI). Architect: Amit Ashok Kumar Patnaik.
 * The global kill switch, the Parimatch and 1xBet safety locks (the real-money gateway refuses a locked book), the
 * auto-drawdown and loss-streak breaker, and the SHA-256 audit trail, re-verified on every read.
 * Figures are measured from the ledgers; an unmeasured bankroll reads "unmeasured" and USD needs a fresh rate.
 */
import { useState } from 'react';
import { apiClient } from '../../api/client';
import { formatDateTime } from '../../lib/format';
import { runMutation, useResource } from '../../lib/resource';
import { useAuthStore } from '../../store/useAuthStore';
import { Async, Button, ConfirmButton, EmptyState, Field, NumberInput, Panel, Pill, TextInput } from '../../ui/kit';

type LockoutMode = 'ARMED' | 'MANUAL_KILL' | 'AUTO_DRAWDOWN_TRIPPED' | 'STREAK_COOLDOWN';

interface EmergencyState {
  id: number;
  global_kill_engaged: boolean;
  lockout_mode: LockoutMode;
  parimatch_locked: boolean;
  onexbet_locked: boolean;
  auto_kill_drawdown_pct: number;
  auto_kill_window_hours: number;
  max_consecutive_losses: number;
  reference_bankroll_inr: number | null;
  reference_bankroll_usd: number | null;
  current_drawdown_inr: number;
  current_drawdown_usd: number | null;
  current_drawdown_pct: number | null;
  current_consecutive_losses: number;
  last_triggered_by: string | null;
  last_trigger_reason: string | null;
  cooldown_expires_at: string | null;
  updated_at: string;
}

interface EmergencyAudit {
  id: string;
  event_type: 'GLOBAL_KILL_ENGAGED' | 'GLOBAL_KILL_RESUMED' | 'AUTO_DRAWDOWN_TRIPPED' | 'STREAK_LOCKOUT_TRIPPED' | 'BOOKMAKER_LOCKED' | 'BOOKMAKER_UNLOCKED';
  target_scope: 'GLOBAL' | 'PARIMATCH' | '1XBET';
  triggered_by: string;
  reason: string;
  drawdown_pct_snapshot: number | null;
  protected_capital_inr: number | null;
  protected_capital_usd: number | null;
  prev_hash: string;
  entry_hash: string;
  created_at: string;
}

interface EmergencyDashboard {
  state: EmergencyState;
  audits: EmergencyAudit[];
  chain_intact: boolean;
  architect: string;
}

interface AutoDrawdownCheck {
  tripped: boolean;
  lockout_mode: LockoutMode;
  drawdown_pct: number | null;
  verdict_summary: string;
  measured: boolean;
}

const BOOKS = [
  { key: 'PARIMATCH' as const, label: 'Parimatch', style: { backgroundColor: '#F8FF13', color: '#0B0E14' }, field: 'parimatch_locked' as const },
  { key: '1XBET' as const, label: '1xBet', style: { backgroundColor: '#007ACC', color: '#FFFFFF' }, field: 'onexbet_locked' as const },
];
const INVALIDATE = ['control-panel:emergency-dashboard', 'system', 'control-panel', 'dashboard'];

export const EmergencyKillSwitchPanel = () => {
  const operator = useAuthStore((s) => s.user?.username) ?? 'Amit Ashok Kumar Patnaik';
  const [currency, setCurrency] = useState<'INR' | 'USD'>('INR');
  const [showGuide, setShowGuide] = useState(true);
  const [killReason, setKillReason] = useState('Manual operator emergency circuit breaker engaged to protect bankroll.');
  const [resumeExposure, setResumeExposure] = useState('5000');
  const [sim, setSim] = useState({ reference_bankroll_inr: '', rolling_loss_inr: '', threshold_drawdown_pct: '', window_hours: '', consecutive_losses: '', max_consecutive_losses: '' });
  const [lastCheck, setLastCheck] = useState<AutoDrawdownCheck | null>(null);

  const dashboard = useResource('control-panel:emergency-dashboard', () => apiClient.get<EmergencyDashboard>('/control-panel/emergency-dashboard'), { intervalMs: 15_000 });

  const money = (inr: number | null, usd: number | null) => {
    if (inr === null) return '— (unmeasured)';
    const rupees = `₹${Number(inr).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
    if (currency === 'INR') return rupees;
    return usd === null ? `${rupees} (no USD rate)` : `$${Number(usd).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  const kill = () => runMutation(() => apiClient.post('/control-panel/emergency-kill', { triggered_by: operator, reason: killReason.trim() || 'Manual operator emergency kill switch engaged.' }),
    { invalidate: INVALIDATE, success: 'GLOBAL KILL SWITCH ENGAGED: bots off, exposure zeroed, both books locked', errorTitle: 'Emergency kill switch failed' });
  const resume = () => runMutation(() => apiClient.post('/control-panel/emergency-resume', {
    triggered_by: operator, reason: 'Operator verified bankroll and market stability; resuming BetDoc operations.', restore_daily_exposure_inr: Math.max(100, Number(resumeExposure) || 5000),
  }), { invalidate: INVALIDATE, success: 'Fortress unlocked and daily INR exposure restored', errorTitle: 'Could not resume operations' });
  const lockBook = (bookmaker: 'PARIMATCH' | '1XBET', locked: boolean) => runMutation(() => apiClient.post('/control-panel/bookmaker-lock', {
    bookmaker, locked, triggered_by: operator,
    reason: locked ? `${bookmaker} safety freeze engaged (odds drift / margin spike protection).` : `${bookmaker} safety freeze lifted; bookmaker verified healthy.`,
  }), { invalidate: ['control-panel:emergency-dashboard', 'bookmakers'], success: `${bookmaker} ${locked ? 'LOCKED' : 'UNLOCKED'}`, errorTitle: 'Bookmaker lock failed' });

  const runCheck = async () => {
    const body: Record<string, number> = {};
    const put = (key: keyof typeof sim, lo: number, hi: number, int = false) => {
      if (sim[key].trim() === '') return;
      const v = Math.max(lo, Math.min(hi, Number(sim[key])));
      if (Number.isFinite(v)) body[key] = int ? Math.round(v) : v;
    };
    put('reference_bankroll_inr', 1, 1e12);
    put('rolling_loss_inr', 0, 1e12);
    put('threshold_drawdown_pct', 1, 95);
    put('window_hours', 1, 168, true);
    put('consecutive_losses', 0, 25, true);
    put('max_consecutive_losses', 1, 25, true);
    const res = await runMutation(() => apiClient.post<AutoDrawdownCheck>('/control-panel/auto-drawdown-check', body),
      { invalidate: INVALIDATE, success: 'Circuit breaker evaluated', errorTitle: 'Circuit breaker check failed' });
    if (res) setLastCheck(res);
  };

  return (
    <Panel
      title="Emergency Kill Switch & Auto-Drawdown Circuit Breaker (KAUTILYA · CHANAKYA · SHIVAJI)"
      icon="emergency_home"
      className="lg:col-span-12"
      updatedAt={dashboard.updatedAt}
      subtitle="Architect: Amit Ashok Kumar Patnaik"
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-lg bg-stone-100 p-0.5 text-xs font-semibold dark:bg-white/[0.06]">
            {(['INR', 'USD'] as const).map((c) => (
              <button key={c} type="button" onClick={() => setCurrency(c)}
                      className={`rounded-md px-2.5 py-1 transition ${currency === c ? (c === 'INR' ? 'bg-emerald-600' : 'bg-sky-600') + ' text-white shadow-sm' : 'text-stone-600 hover:text-stone-900 dark:text-stone-300'}`}>
                {c === 'INR' ? '₹ INR (primary)' : '$ USD (secondary)'}
              </button>
            ))}
          </div>
          <Button size="sm" variant="ghost" icon="help" onClick={() => setShowGuide((g) => !g)}>{showGuide ? 'Hide guide' : 'Show guide'}</Button>
        </div>
      }
    >
      {showGuide && (
        <div className="mb-5 rounded-xl border border-rose-500/30 bg-rose-500/[0.06] p-3.5 text-xs text-stone-700 dark:text-stone-200">
          <p className="pb-1.5 font-bold uppercase tracking-wider text-rose-700 dark:text-rose-400">How the emergency fortress works</p>
          <ul className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <li className="rounded-lg bg-white/70 p-2.5 dark:bg-black/25">
              <strong className="text-stone-900 dark:text-white">1. Global kill switch</strong>
              <p className="mt-0.5 text-[11px] leading-relaxed text-stone-600 dark:text-stone-300">One click stops every bot, sets daily exposure to <strong>₹0</strong> and locks both books until you resume.</p>
            </li>
            <li className="rounded-lg bg-white/70 p-2.5 dark:bg-black/25">
              <strong className="text-stone-900 dark:text-white">2. Parimatch & 1xBet safety locks</strong>
              <p className="mt-0.5 text-[11px] leading-relaxed text-stone-600 dark:text-stone-300">Lock one book (suspended markets, margin spikes) and keep the other: the real-money gateway refuses tickets for a locked book.</p>
            </li>
            <li className="rounded-lg bg-white/70 p-2.5 dark:bg-black/25">
              <strong className="text-stone-900 dark:text-white">3. Auto-drawdown & streak breaker</strong>
              <p className="mt-0.5 text-[11px] leading-relaxed text-stone-600 dark:text-stone-300">Trips the kill switch when the window's measured loss reaches the cap share of your bankroll, or the loss streak its limit. Every event is SHA-256 chained.</p>
            </li>
          </ul>
        </div>
      )}

      <Async resource={dashboard} empty={<EmptyState icon="emergency_home" title="Emergency fortress initializing" />}>
        {(data) => {
          const st = data.state;
          const protectedInr = st.reference_bankroll_inr === null ? null : Math.max(0, st.reference_bankroll_inr - st.current_drawdown_inr);
          const protectedUsd = st.reference_bankroll_usd === null || st.current_drawdown_usd === null ? null : Math.max(0, st.reference_bankroll_usd - st.current_drawdown_usd);
          return (
            <div className="flex flex-col gap-5">
              <div className={`rounded-2xl border p-4 ${st.global_kill_engaged ? 'border-rose-500/60 bg-rose-500/10' : 'border-emerald-500/40 bg-emerald-500/[0.05]'}`}>
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-black uppercase tracking-wider text-stone-900 dark:text-white">
                        {st.global_kill_engaged ? '🚨 Global emergency lockout engaged' : '🟢 Emergency fortress armed'}
                      </span>
                      <Pill tone={st.global_kill_engaged ? 'critical' : 'good'}>{st.lockout_mode.replace(/_/g, ' ')}</Pill>
                    </div>
                    <p className="mt-1 text-xs text-stone-600 dark:text-stone-300">
                      Protected bankroll <strong className="font-mono text-stone-900 dark:text-white">{money(protectedInr, protectedUsd)}</strong> of {money(st.reference_bankroll_inr, st.reference_bankroll_usd)}
                      {' '}· {st.auto_kill_window_hours}h drawdown <strong className="font-mono text-rose-600 dark:text-rose-400">{st.current_drawdown_pct === null ? '—' : `${st.current_drawdown_pct.toFixed(2)}%`}</strong>
                      {' '}(cap {st.auto_kill_drawdown_pct.toFixed(2)}%) · loss streak <strong>{st.current_consecutive_losses}/{st.max_consecutive_losses}</strong>
                      {st.cooldown_expires_at && st.global_kill_engaged ? <> · cool-down until {formatDateTime(st.cooldown_expires_at)}</> : null}
                    </p>
                    {st.last_trigger_reason && <p className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">Last event ({st.last_triggered_by}): {st.last_trigger_reason}</p>}
                  </div>
                  {st.global_kill_engaged ? (
                    <div className="flex flex-wrap items-end gap-2">
                      <Field label="Restore daily exposure (₹)" className="w-44">
                        <NumberInput value={resumeExposure} min="100" onChange={(e) => setResumeExposure(e.target.value)} />
                      </Field>
                      <ConfirmButton variant="primary" icon="play_circle" confirmLabel="Confirm resume?" onConfirm={() => void resume()}>Unlock & resume</ConfirmButton>
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-end gap-2">
                      <Field label="Reason" className="w-64">
                        <TextInput value={killReason} onChange={(e) => setKillReason(e.target.value)} placeholder="Reason for the halt" />
                      </Field>
                      <ConfirmButton variant="danger" icon="emergency_home" confirmLabel="Confirm global kill?" onConfirm={() => void kill()}>Engage global kill switch</ConfirmButton>
                    </div>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
                <div className="flex flex-col gap-3 rounded-xl border border-stone-200/80 p-4 dark:border-white/10 lg:col-span-5">
                  <span className="text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Per-bookmaker safety locks</span>
                  {BOOKS.map((b) => {
                    const locked = st[b.field];
                    return (
                      <div key={b.key} className="flex items-center justify-between gap-3 rounded-xl border border-stone-200/70 bg-stone-50/70 p-3 dark:border-white/10 dark:bg-white/[0.02]">
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="rounded px-2 py-0.5 text-xs font-extrabold" style={b.style}>{b.label}</span>
                            <Pill tone={locked ? 'critical' : 'good'}>{locked ? 'LOCKED' : 'ACTIVE'}</Pill>
                          </div>
                          <p className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">{locked ? `Real-money tickets for ${b.label} are refused.` : `${b.label} takes tickets as normal.`}</p>
                        </div>
                        <Button size="sm" variant={locked ? 'primary' : 'ghost'} icon={locked ? 'lock_open' : 'lock'} onClick={() => void lockBook(b.key, !locked)}>
                          {locked ? 'Unlock' : 'Lock book'}
                        </Button>
                      </div>
                    );
                  })}
                </div>

                <div className="flex flex-col gap-3 rounded-xl border border-stone-200/80 p-4 dark:border-white/10 lg:col-span-7">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Auto-drawdown & loss-streak breaker (blank = measured / configured)</span>
                    <ConfirmButton size="sm" variant="primary" icon="shield_lock" confirmLabel="Enforce: a breach halts trading" onConfirm={() => void runCheck()}>Evaluate & enforce</ConfirmButton>
                  </div>
                  <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
                    {([
                      ['reference_bankroll_inr', 'Reference bankroll ₹', 'measured from ledgers'],
                      ['rolling_loss_inr', 'Rolling loss ₹', 'measured from ledgers'],
                      ['consecutive_losses', 'Consecutive losses', 'measured from ledgers'],
                      ['threshold_drawdown_pct', 'Auto-kill threshold %', `${st.auto_kill_drawdown_pct}`],
                      ['window_hours', 'Window (hours)', `${st.auto_kill_window_hours}`],
                      ['max_consecutive_losses', 'Max loss streak', `${st.max_consecutive_losses}`],
                    ] as const).map(([key, label, placeholder]) => (
                      <Field key={key} label={label}>
                        <NumberInput value={sim[key]} placeholder={placeholder} onChange={(e) => setSim({ ...sim, [key]: e.target.value })} />
                      </Field>
                    ))}
                  </div>
                  {lastCheck && (
                    <div className={`rounded-lg p-2.5 text-xs ${lastCheck.tripped ? 'bg-rose-500/15 text-rose-800 dark:text-rose-200' : 'bg-emerald-500/15 text-emerald-800 dark:text-emerald-200'}`}>
                      <strong>{lastCheck.lockout_mode.replace(/_/g, ' ')}{lastCheck.measured ? '' : ' (hand-entered figures)'}:</strong> {lastCheck.verdict_summary}
                    </div>
                  )}
                </div>
              </div>

              <div>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">SHA-256 emergency audit trail ({data.audits.length} latest)</span>
                  <Pill tone={data.chain_intact ? 'good' : 'critical'} icon={data.chain_intact ? 'verified_user' : 'gpp_bad'}>{data.chain_intact ? 'chain verified' : 'CHAIN BROKEN: a record was altered'}</Pill>
                </div>
                {data.audits.length === 0 ? (
                  <EmptyState icon="verified_user" title="No emergency events yet" detail="Every kill, automatic trip, book lock and resume is recorded here, each chained to the one before by SHA-256." />
                ) : (
                  <div className="overflow-x-auto rounded-xl border border-stone-200/80 dark:border-white/10">
                    <table className="w-full text-left text-xs">
                      <thead className="bg-stone-100/80 text-[11px] uppercase text-stone-500 dark:bg-white/[0.04] dark:text-stone-400">
                        <tr>{['Time', 'Event', 'Scope', 'By', 'Drawdown', 'Protected capital', 'Reason', 'SHA-256'].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}</tr>
                      </thead>
                      <tbody className="divide-y divide-stone-200/60 dark:divide-white/5">
                        {data.audits.map((a) => (
                          <tr key={a.id} className="hover:bg-stone-50/80 dark:hover:bg-white/[0.02]">
                            <td className="whitespace-nowrap px-3 py-2 text-[11px] text-stone-500">{formatDateTime(a.created_at)}</td>
                            <td className="px-3 py-2"><Pill tone={a.event_type.includes('RESUMED') || a.event_type.includes('UNLOCKED') ? 'good' : 'critical'}>{a.event_type.replace(/_/g, ' ')}</Pill></td>
                            <td className="px-3 py-2 font-semibold">{a.target_scope}</td>
                            <td className="px-3 py-2 font-medium text-stone-800 dark:text-stone-200">{a.triggered_by}</td>
                            <td className="px-3 py-2 font-mono">{a.drawdown_pct_snapshot === null ? '—' : `${a.drawdown_pct_snapshot.toFixed(2)}%`}</td>
                            <td className="px-3 py-2 font-mono font-semibold text-emerald-600 dark:text-emerald-400">{money(a.protected_capital_inr, a.protected_capital_usd)}</td>
                            <td className="max-w-xs truncate px-3 py-2 text-stone-600 dark:text-stone-300" title={a.reason}>{a.reason}</td>
                            <td className="px-3 py-2 font-mono text-[10px] text-stone-400" title={a.entry_hash}>#{a.entry_hash.slice(0, 12)}…</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          );
        }}
      </Async>
    </Panel>
  );
};
