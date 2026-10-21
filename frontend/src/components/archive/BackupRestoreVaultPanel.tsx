/**
 * Group 87: the encrypted backup & integrity restore vault (SHIVAJI & TODAR MAL). Architect: Amit Ashok Kumar Patnaik.
 * Fernet-sealed snapshots chained by SHA-256, integrity checks and dry-run restores against the live schema, pinning
 * and retention. Snapshots complement pg_dump (scripts/backup.sh); nothing here restores over live data.
 */
import { useState } from 'react';
import { apiClient } from '../../api/client';
import { formatAgo, formatDateTime, formatInt } from '../../lib/format';
import { runMutation, useResource } from '../../lib/resource';
import { useAuthStore } from '../../store/useAuthStore';
import { Async, Button, ConfirmButton, EmptyState, Field, NumberInput, Panel, Pill, TextInput, Toggle } from '../../ui/kit';

type Status = 'COMPLETED' | 'VERIFIED' | 'CORRUPTED' | 'PRUNED';
type Check = 'INTEGRITY_VERIFY' | 'DRY_RUN_RESTORE';

interface BackupSnapshot {
  id: string;
  snapshot_label: string;
  trigger_mode: 'MANUAL' | 'SCHEDULED' | 'PRE_MIGRATION';
  status: Status;
  filename: string;
  encryption_algorithm: string;
  key_fingerprint: string;
  file_bytes: number;
  uncompressed_bytes: number;
  total_tables: number;
  total_rows: number;
  is_complete: boolean;
  truncated_tables: string[];
  protected_bankroll_inr: number | null;
  protected_bankroll_usd: number | null;
  plaintext_sha256: string;
  ciphertext_sha256: string;
  prev_hash: string;
  entry_hash: string;
  is_pinned: boolean;
  triggered_by: string;
  last_verified_at: string | null;
  pruned_at: string | null;
  created_at: string;
}

interface BackupAudit {
  id: string;
  snapshot_id: string | null;
  operation: Check | 'PRUNE_RETENTION';
  verdict: 'PASS' | 'FAIL';
  tables_checked: number;
  rows_verified: number;
  duration_ms: number;
  detail: string;
  triggered_by: string;
  created_at: string;
}

interface VaultDashboard {
  encryption_algorithm: string;
  backup_dir: string;
  schedule_enabled: boolean;
  schedule_hours: number;
  retention_count: number;
  max_rows_per_table: number;
  current_key_fingerprint: string;
  total_snapshots: number;
  total_storage_bytes: number;
  latest_backup_at: string | null;
  chain_intact: boolean;
  snapshots: BackupSnapshot[];
  audits: BackupAudit[];
  architect: string;
}

interface VerifyResult {
  valid: boolean;
  verdict: 'PASS' | 'FAIL';
  operation: Check;
  tables_checked: number;
  rows_verified: number;
  duration_ms: number;
  detail: string;
  missing_tables: string[];
  missing_columns: Record<string, string[]>;
  snapshot: BackupSnapshot;
}

const STATUS_TONE: Record<Status, 'good' | 'critical' | 'neutral' | 'info'> = { VERIFIED: 'good', COMPLETED: 'info', CORRUPTED: 'critical', PRUNED: 'neutral' };
const INVALIDATE = ['archive:backups', 'archive:overview'];

const formatBytes = (bytes: number): string =>
  bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : bytes < 1024 ** 3 ? `${(bytes / 1024 ** 2).toFixed(2)} MB` : `${(bytes / 1024 ** 3).toFixed(2)} GB`;

export const BackupRestoreVaultPanel = () => {
  const isAdmin = useAuthStore((s) => s.user?.role === 'ADMIN');
  const [currency, setCurrency] = useState<'INR' | 'USD'>('INR');
  const [showGuide, setShowGuide] = useState(true);
  const [label, setLabel] = useState('Pre-match ledger checkpoint');
  const [pinOnCreate, setPinOnCreate] = useState(false);
  const [retention, setRetention] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<VerifyResult | null>(null);

  const vault = useResource('archive:backups', () => apiClient.get<VaultDashboard>('/archive/backups'), { intervalMs: 30_000 });

  const money = (inr: number | null, usd: number | null) => {
    if (inr === null) return '— (unmeasured)';
    const rupees = `₹${Number(inr).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
    if (currency === 'INR') return rupees;
    return usd === null ? `${rupees} (no USD rate)` : `$${Number(usd).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  const create = async () => {
    setBusy('create');
    await runMutation(() => apiClient.post('/archive/backups/create', { snapshot_label: label.trim() || 'Manual encrypted snapshot', is_pinned: pinOnCreate }),
      { invalidate: INVALIDATE, success: 'Snapshot sealed with Fernet and chained', errorTitle: 'Backup failed' });
    setBusy(null);
  };
  const check = async (id: string, operation: Check) => {
    setBusy(`${operation}:${id}`);
    const res = await runMutation(() => apiClient.post<VerifyResult>(`/archive/backups/${id}/verify`, { operation }),
      { invalidate: INVALIDATE, success: (r) => `${operation === 'DRY_RUN_RESTORE' ? 'Dry-run restore' : 'Integrity check'}: ${r.verdict}`, errorTitle: 'Verification failed' });
    if (res) setLastResult(res);
    setBusy(null);
  };
  const pin = (id: string, pinned: boolean) => runMutation(() => apiClient.post(`/archive/backups/${id}/pin`, { is_pinned: pinned }),
    { invalidate: ['archive:backups'], success: pinned ? 'Pinned: retention will never prune it' : 'Unpinned', errorTitle: 'Could not change the pin' });
  const prune = (fallback: number) => runMutation(() => apiClient.post('/archive/backups/prune', { retention_count: Math.max(1, Math.min(365, Math.round(Number(retention) || fallback))) }),
    { invalidate: INVALIDATE, success: 'Retention applied to unpinned snapshots', errorTitle: 'Retention failed' });

  return (
    <Panel
      title="Encrypted Backup & Integrity Restore Vault (SHIVAJI · TODAR MAL)"
      icon="backup"
      className="lg:col-span-12"
      updatedAt={vault.updatedAt}
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
        <div className="mb-5 rounded-xl border border-emerald-500/30 bg-emerald-500/[0.06] p-3.5 text-xs text-stone-700 dark:text-stone-200">
          <p className="pb-1.5 font-bold uppercase tracking-wider text-emerald-700 dark:text-emerald-400">How the backup vault works</p>
          <ul className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <li className="rounded-lg bg-white/70 p-2.5 dark:bg-black/25">
              <strong className="text-stone-900 dark:text-white">1. Sealed snapshots</strong>
              <p className="mt-0.5 text-[11px] leading-relaxed text-stone-600 dark:text-stone-300">Every table is compressed and sealed with <strong>Fernet (AES-128-CBC + HMAC-SHA256)</strong>; each manifest is chained to the previous one by SHA-256. Scheduled snapshots run on the beat.</p>
            </li>
            <li className="rounded-lg bg-white/70 p-2.5 dark:bg-black/25">
              <strong className="text-stone-900 dark:text-white">2. Verify & dry-run restore</strong>
              <p className="mt-0.5 text-[11px] leading-relaxed text-stone-600 dark:text-stone-300"><strong>Verify</strong> checks the file, the seal and the content digests. <strong>Dry-run restore</strong> also checks every table and column against today's database. Nothing is written.</p>
            </li>
            <li className="rounded-lg bg-white/70 p-2.5 dark:bg-black/25">
              <strong className="text-stone-900 dark:text-white">3. Pins & retention</strong>
              <p className="mt-0.5 text-[11px] leading-relaxed text-stone-600 dark:text-stone-300">Pin milestones (before a tournament or an upgrade): retention deletes old unpinned files only, and keeps their manifests so the chain stays checkable.</p>
            </li>
          </ul>
        </div>
      )}

      <Async resource={vault} empty={<EmptyState icon="backup" title="Backup vault initializing" />}>
        {(data) => (
          <div className="flex flex-col gap-5">
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
              <div className="flex flex-col justify-between gap-3 rounded-xl border border-stone-200/80 p-4 dark:border-white/10 lg:col-span-5">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Vault posture</span>
                    <Pill tone={data.chain_intact ? 'good' : 'critical'} icon={data.chain_intact ? 'verified_user' : 'gpp_bad'}>{data.chain_intact ? 'chain verified' : 'CHAIN BROKEN'}</Pill>
                    <Pill tone={data.schedule_enabled ? 'good' : 'warning'}>{data.schedule_enabled ? `every ${data.schedule_hours}h` : 'schedule off'}</Pill>
                  </div>
                  <p className="mt-1.5 text-xs text-stone-600 dark:text-stone-300">
                    <strong>{formatInt(data.total_snapshots)}</strong> live snapshots ({formatBytes(data.total_storage_bytes)}) · keep newest <strong>{data.retention_count}</strong> · cap {formatInt(data.max_rows_per_table)} rows/table
                  </p>
                  <p className="mt-1 text-[11px] text-stone-500 dark:text-stone-400">
                    Latest: {data.latest_backup_at ? formatAgo(data.latest_backup_at) : 'none yet'} · key <span className="font-mono">{data.current_key_fingerprint}</span> · <span className="font-mono">{data.backup_dir}</span>
                  </p>
                </div>
                {isAdmin && (
                  <div className="flex flex-wrap items-end gap-2 pt-1">
                    <Field label="Keep newest" className="w-32">
                      <NumberInput value={retention} placeholder={String(data.retention_count)} min="1" max="365" onChange={(e) => setRetention(e.target.value)} />
                    </Field>
                    <ConfirmButton size="sm" variant="ghost" icon="delete_sweep" confirmLabel="Delete old unpinned files?" onConfirm={() => void prune(data.retention_count)}>Apply retention</ConfirmButton>
                  </div>
                )}
              </div>

              <div className="flex flex-col justify-between gap-3 rounded-xl border border-stone-200/80 p-4 dark:border-white/10 lg:col-span-7">
                <span className="text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Seal a snapshot now</span>
                {isAdmin ? (
                  <div className="flex flex-wrap items-end gap-3">
                    <Field label="Label" className="min-w-[220px] flex-1">
                      <TextInput value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Before the IPL playoffs" />
                    </Field>
                    <Toggle label="Pin it" checked={pinOnCreate} onChange={setPinOnCreate} />
                    <Button variant="primary" icon="enhanced_encryption" busy={busy === 'create'} onClick={() => void create()}>Create encrypted backup</Button>
                  </div>
                ) : (
                  <p className="text-xs text-stone-500">An administrator seals, verifies and prunes snapshots.</p>
                )}
                {lastResult && (
                  <div className={`rounded-lg p-2.5 text-xs ${lastResult.valid ? 'bg-emerald-500/15 text-emerald-800 dark:text-emerald-200' : 'bg-rose-500/15 text-rose-800 dark:text-rose-200'}`}>
                    <strong>{lastResult.operation.replace(/_/g, ' ')} · {lastResult.verdict} in {lastResult.duration_ms} ms:</strong> {lastResult.detail}
                    {lastResult.missing_tables.length > 0 && <p className="mt-1">Missing tables: <span className="font-mono">{lastResult.missing_tables.join(', ')}</span></p>}
                    {Object.entries(lastResult.missing_columns).map(([t, cols]) => <p key={t} className="mt-0.5">{t}: missing <span className="font-mono">{cols.join(', ')}</span></p>)}
                  </div>
                )}
              </div>
            </div>

            <div>
              <span className="mb-2 block text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Snapshots ({data.snapshots.length})</span>
              {data.snapshots.length === 0 ? (
                <EmptyState icon="enhanced_encryption" title="No snapshots yet" detail="Seal one above, or wait for the scheduled snapshot." />
              ) : (
                <div className="overflow-x-auto rounded-xl border border-stone-200/80 dark:border-white/10">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-stone-100/80 text-[11px] uppercase text-stone-500 dark:bg-white/[0.04] dark:text-stone-400">
                      <tr>{['Created', 'Label & file', 'Status', 'Tables / rows', 'Size', 'Protected bankroll', 'Chain', ''].map((h) => <th key={h} className="px-3 py-2">{h}</th>)}</tr>
                    </thead>
                    <tbody className="divide-y divide-stone-200/60 dark:divide-white/5">
                      {data.snapshots.map((s) => {
                        const otherKey = s.key_fingerprint !== data.current_key_fingerprint;
                        return (
                          <tr key={s.id} className="hover:bg-stone-50/80 dark:hover:bg-white/[0.02]">
                            <td className="whitespace-nowrap px-3 py-2 text-[11px] text-stone-500">{formatDateTime(s.created_at)}<div className="text-stone-400">{s.trigger_mode.toLowerCase()} · {s.triggered_by}</div></td>
                            <td className="px-3 py-2">
                              <div className="flex items-center gap-1.5 font-semibold text-stone-900 dark:text-stone-100">{s.snapshot_label}{s.is_pinned && <Pill tone="accent" icon="keep">pinned</Pill>}</div>
                              <div className="font-mono text-[10px] text-stone-400">{s.filename}</div>
                            </td>
                            <td className="px-3 py-2">
                              <div className="flex flex-wrap gap-1">
                                <Pill tone={STATUS_TONE[s.status]}>{s.status}</Pill>
                                {!s.is_complete && <Pill tone="warning" icon="content_cut">partial: {s.truncated_tables.join(', ')}</Pill>}
                                {otherKey && s.status !== 'PRUNED' && <Pill tone="warning" icon="key">other key {s.key_fingerprint}</Pill>}
                              </div>
                            </td>
                            <td className="px-3 py-2 font-mono">{formatInt(s.total_tables)} / {formatInt(s.total_rows)}</td>
                            <td className="px-3 py-2 font-mono">{s.status === 'PRUNED' ? '—' : formatBytes(s.file_bytes)}</td>
                            <td className="px-3 py-2 font-mono font-semibold text-emerald-600 dark:text-emerald-400">{money(s.protected_bankroll_inr, s.protected_bankroll_usd)}</td>
                            <td className="px-3 py-2 font-mono text-[10px] text-stone-400" title={s.entry_hash}>#{s.entry_hash.slice(0, 12)}…</td>
                            <td className="px-3 py-2">
                              {isAdmin && (
                                <div className="flex flex-wrap items-center gap-1.5">
                                  {s.status !== 'PRUNED' && (
                                    <>
                                      <Button size="sm" variant="ghost" icon="fact_check" busy={busy === `INTEGRITY_VERIFY:${s.id}`} onClick={() => void check(s.id, 'INTEGRITY_VERIFY')}>Verify</Button>
                                      <Button size="sm" variant="ghost" icon="settings_backup_restore" busy={busy === `DRY_RUN_RESTORE:${s.id}`} onClick={() => void check(s.id, 'DRY_RUN_RESTORE')}>Dry-run restore</Button>
                                    </>
                                  )}
                                  <Button size="sm" variant="ghost" icon={s.is_pinned ? 'keep_off' : 'keep'} onClick={() => void pin(s.id, !s.is_pinned)}>{s.is_pinned ? 'Unpin' : 'Pin'}</Button>
                                </div>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {data.audits.length > 0 && (
              <div>
                <span className="mb-2 block text-xs font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">Checks & retention log</span>
                <ul className="flex flex-col gap-1.5">
                  {data.audits.slice(0, 8).map((a) => (
                    <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-stone-50 px-3 py-2 text-[11px] dark:bg-white/[0.03]">
                      <span className="flex min-w-0 items-center gap-2">
                        <Pill tone={a.verdict === 'PASS' ? 'good' : 'critical'}>{a.verdict}</Pill>
                        <span className="font-semibold">{a.operation.replace(/_/g, ' ')}</span>
                        <span className="truncate text-stone-500" title={a.detail}>{a.detail}</span>
                      </span>
                      <span className="shrink-0 text-stone-400">{a.triggered_by} · {formatAgo(a.created_at)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </Async>
    </Panel>
  );
};
