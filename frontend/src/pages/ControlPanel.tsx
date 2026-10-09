import { lazy, Suspense, useEffect, useState, type FormEvent } from 'react';
import { apiClient } from '../api/client';
import { StakeCapPanel } from '../components/arena/StakeCapPanel';
import { RiskManagement } from '../components/cfo/RiskManagement';
import { SniperTerminal } from '../components/sniper/SniperTerminal';
import { ActivePortfolio } from '../components/portfolio/ActivePortfolio';
import { HiveBots } from '../components/hive/HiveBots';
import { FleetCommand } from '../components/fleet/FleetCommand';
import { type ControlSettings, emergencyStop, resumeTrading, useControls, useExchanges } from '../lib/api';
import { formatAgo, formatINR, formatRatioPct, humanize } from '../lib/format';
import { runMutation, useResource } from '../lib/resource';
import { useSystemStore } from '../store/useSystemStore';
import { CommanderHero, MOTIFS } from '../ui/hero';
import { Async, Button, ConfirmButton, EmptyState, Field, KeyValues, NumberInput, Page, Panel, Segmented, Pill, Select, Skeleton, StatusBadge, TextInput, Toggle, num } from '../ui/kit';

// ---------------------------------------------------------------------------
// CONTRACTS
// ---------------------------------------------------------------------------
interface BookmakerConfig { id: string; name: string; is_active: boolean; base_url: string | null; priority_rank: number; has_api_key: boolean; updated_at: string }
interface SportConfig { id: string; sport_name: string; is_active: boolean; config: Record<string, unknown>; updated_at: string }
interface OmniHealth { generated_at: string; total: number; active: number; degraded: number; open_circuits: number; redis_available: boolean; providers: { provider_id: string; provider_name: string; category_code: string; is_active: boolean; health_status: string; breaker_state: string; recent_failures: number | null }[] }

// The Lab carries the charting library: loaded when its tab first opens, not with the Control Panel
const QuantLab = lazy(() => import('../components/lab/QuantLab').then((m) => ({ default: m.QuantLab })));

const SPORTS = ['cricket', 'basketball', 'tennis'] as const;
type ControlTab = 'system' | 'risk' | 'portfolio' | 'hive' | 'lab' | 'terminal';
const TABS = [
  { value: 'system', label: 'System', icon: 'tune' },
  { value: 'risk', label: 'Risk management', icon: 'shield_lock' },
  { value: 'portfolio', label: 'Active portfolio', icon: 'monitoring' },
  { value: 'hive', label: 'Hive bots', icon: 'hub' },
  { value: 'lab', label: 'The Lab', icon: 'science' },
  { value: 'terminal', label: 'Execution terminal', icon: 'terminal' },
] as const;
const EXCHANGES = ['Pinnacle', 'Betfair'] as const;

// ---------------------------------------------------------------------------
// GLOBAL OVERRIDES (risk limits + automation)
// ---------------------------------------------------------------------------
const GlobalOverrides = ({ settings }: { settings: ControlSettings }) => {
  const [d, setD] = useState({
    max_bet_size: String(settings.max_bet_size), max_daily_exposure: String(settings.max_daily_exposure), global_stop_loss: String(settings.global_stop_loss),
    default_kelly_fraction: String(settings.default_kelly_fraction), research_frequency_minutes: String(settings.research_frequency_minutes), bots_enabled: settings.bots_enabled,
  });
  useEffect(() => {
    setD({
      max_bet_size: String(settings.max_bet_size), max_daily_exposure: String(settings.max_daily_exposure), global_stop_loss: String(settings.global_stop_loss),
      default_kelly_fraction: String(settings.default_kelly_fraction), research_frequency_minutes: String(settings.research_frequency_minutes), bots_enabled: settings.bots_enabled,
    });
  }, [settings]);

  const save = (e: FormEvent) => {
    e.preventDefault();
    void runMutation(() => apiClient.patch<ControlSettings>('/control-panel', {
      max_bet_size: num(d.max_bet_size), max_daily_exposure: num(d.max_daily_exposure), global_stop_loss: num(d.global_stop_loss),
      default_kelly_fraction: num(d.default_kelly_fraction), research_frequency_minutes: Math.round(num(d.research_frequency_minutes)), bots_enabled: d.bots_enabled,
    }), { invalidate: ['system', 'control-panel', 'commanders', 'dashboard'], success: 'Global limits saved and enforced on the next order', errorTitle: 'Limits rejected' });
  };

  return (
    <Panel title="Global overrides" icon="tune" className="lg:col-span-7" subtitle={`updated ${formatAgo(settings.updated_at)}`}>
      <form onSubmit={save} className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label="Max bet size ₹" hint="Every order above this is refused by execution."><NumberInput min="0" value={d.max_bet_size} onChange={(e) => setD({ ...d, max_bet_size: e.target.value })} /></Field>
        <Field label="Max daily exposure ₹" hint="0 = trading halted (what the kill switch sets)."><NumberInput min="0" value={d.max_daily_exposure} onChange={(e) => setD({ ...d, max_daily_exposure: e.target.value })} /></Field>
        <Field label="Global stop-loss ₹"><NumberInput min="0" value={d.global_stop_loss} onChange={(e) => setD({ ...d, global_stop_loss: e.target.value })} /></Field>
        <Field label="Default Kelly fraction" hint="Used by the terminal and the Oracle allocator."><NumberInput min="0" max="1" step="0.05" value={d.default_kelly_fraction} onChange={(e) => setD({ ...d, default_kelly_fraction: e.target.value })} /></Field>
        <Field label="Research cadence (min)"><NumberInput min="1" value={d.research_frequency_minutes} onChange={(e) => setD({ ...d, research_frequency_minutes: e.target.value })} /></Field>
        <div className="flex items-center justify-between gap-3 rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/[0.03]">
          <div>
            <p className="text-sm font-semibold text-stone-800 dark:text-stone-100">Automated bots</p>
            <p className="text-[11px] text-stone-500 dark:text-stone-400">Commander automation (manual orders unaffected).</p>
          </div>
          <Toggle label="Automated bots" checked={d.bots_enabled} onChange={(v) => setD({ ...d, bots_enabled: v })} />
        </div>
        <Button type="submit" variant="primary" icon="save" className="sm:col-span-2">Save overrides</Button>
      </form>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// CREDENTIALS (write-only)
// ---------------------------------------------------------------------------
const Credentials = ({ settings }: { settings: ControlSettings }) => {
  const [keys, setKeys] = useState({ news_api_key: '', omniroute_url: '' });
  const save = (field: keyof typeof keys) => {
    const value = keys[field].trim();
    if (!value) return;
    void runMutation(() => apiClient.patch('/control-panel', { [field]: value }), {
      invalidate: ['system', 'control-panel'], success: `${humanize(field)} updated`, errorTitle: 'Rejected',
    }).then((ok) => ok && setKeys((k) => ({ ...k, [field]: '' })));
  };
  const row = (field: keyof typeof keys, label: string, current: string | null, placeholder: string) => (
    <div className="flex items-end gap-2">
      <Field label={label} className="flex-1" hint={current ? `Stored: ${current}` : 'Not set'}>
        <TextInput type={field === 'omniroute_url' ? 'url' : 'password'} value={keys[field]} onChange={(e) => setKeys({ ...keys, [field]: e.target.value })} placeholder={placeholder} autoComplete="off" />
      </Field>
      <Button icon="save" disabled={!keys[field].trim()} onClick={() => save(field)} className="mb-5">Save</Button>
    </div>
  );
  return (
    <Panel title="Integrations" icon="key" className="lg:col-span-5" subtitle="write-only; never echoed back">
      <div className="flex flex-col gap-1">
        {row('news_api_key', 'News API key', settings.news_api_key, 'paste to replace')}
        {row('omniroute_url', 'OmniRoute URL', settings.omniroute_url, 'https://…')}
        <p className="flex items-center gap-1.5 text-[11px] text-stone-400 dark:text-stone-500">
          <span className="material-symbols-outlined text-[14px]">hub</span>
          Data-source keys (The Odds API) live in Fleet Command below, vault-encrypted and used by ingestion.
        </p>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// EXCHANGE ACCOUNTS
// ---------------------------------------------------------------------------
const ExchangeAccounts = () => {
  const exchanges = useExchanges();
  const [form, setForm] = useState({ exchange_name: 'Pinnacle', api_key: '', api_secret: '' });
  const [busy, setBusy] = useState(false);
  const link = async (e: FormEvent) => {
    e.preventDefault();
    if (!form.api_key || !form.api_secret) return;
    setBusy(true);
    const ok = await runMutation(() => apiClient.post('/exchanges', form), { invalidate: ['exchanges'], success: `${form.exchange_name} account linked`, errorTitle: 'Could not link account' });
    setBusy(false);
    if (ok) setForm({ ...form, api_key: '', api_secret: '' });
  };
  return (
    <Panel title="Exchange API integrations" icon="cable" className="lg:col-span-6" updatedAt={exchanges.updatedAt} subtitle="credentials encrypted at rest">
      <div className="flex flex-col gap-4">
        <Async resource={exchanges} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="cable" title="No exchange linked" detail="Link an account to route orders. Every adapter currently executes on the paper exchange." />}>
          {(rows) => (
            <ul className="flex flex-col gap-2">
              {rows.map((a) => (
                <li key={a.id} className="flex items-center justify-between gap-3 rounded-xl bg-stone-50 px-3 py-2.5 dark:bg-white/[0.03]">
                  <span className="flex items-center gap-2"><span className="text-sm font-semibold text-stone-800 dark:text-stone-100">{a.exchange_name}</span><Pill tone="info">paper</Pill></span>
                  <span className="flex items-center gap-2">
                    <StatusBadge status={a.is_active ? 'CONNECTED' : 'DISABLED'} />
                    {a.is_active && (
                      <ConfirmButton size="sm" variant="ghost" confirmLabel="Deactivate?" onConfirm={() => void runMutation(() => apiClient.patch(`/exchanges/${a.id}/deactivate`), { invalidate: ['exchanges'], success: `${a.exchange_name} deactivated` })}>
                        Deactivate
                      </ConfirmButton>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Async>
        <form onSubmit={link} className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
          <Field label="Exchange"><Select value={form.exchange_name} onChange={(e) => setForm({ ...form, exchange_name: e.target.value })}>{EXCHANGES.map((x) => <option key={x}>{x}</option>)}</Select></Field>
          <Field label="API key"><TextInput type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} autoComplete="off" /></Field>
          <Field label="API secret"><TextInput type="password" value={form.api_secret} onChange={(e) => setForm({ ...form, api_secret: e.target.value })} autoComplete="off" /></Field>
          <Button type="submit" variant="primary" icon="link" busy={busy} disabled={!form.api_key || !form.api_secret} className="sm:col-span-3">Link account</Button>
        </form>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// BOOKMAKER ROUTING TABLE
// ---------------------------------------------------------------------------
const Bookmakers = () => {
  const books = useResource('bookmakers:config', () => apiClient.get<BookmakerConfig[]>('/bookmakers/config'));
  const [form, setForm] = useState({ name: '', priority_rank: '100', base_url: '' });
  const add = (e: FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) return;
    void runMutation(() => apiClient.post('/bookmakers/config', { name: form.name.trim(), is_active: true, priority_rank: Math.round(num(form.priority_rank)) || 100, base_url: form.base_url.trim() || null }), {
      invalidate: ['bookmakers'], success: `${form.name} added to routing`, errorTitle: 'Bookmaker rejected',
    }).then((ok) => ok && setForm({ name: '', priority_rank: '100', base_url: '' }));
  };
  const toggle = (b: BookmakerConfig) => runMutation(() => apiClient.put(`/bookmakers/${encodeURIComponent(b.name)}/config`, { is_active: !b.is_active }), {
    invalidate: ['bookmakers'], success: `${b.name} ${b.is_active ? 'disabled' : 'enabled'}`,
  });
  return (
    <Panel title="Bookmaker routing" icon="alt_route" className="lg:col-span-6" updatedAt={books.updatedAt} subtitle="used by the Phantom odds router">
      <div className="flex flex-col gap-4">
        <Async resource={books} isEmpty={(r) => r.length === 0} empty={<EmptyState icon="alt_route" title="No bookmakers configured" detail="With none configured, routing considers every quoting book." />}>
          {(rows) => (
            <ul className="flex flex-col gap-2">
              {[...rows].sort((a, b) => a.priority_rank - b.priority_rank).map((b) => (
                <li key={b.id} className="flex items-center justify-between gap-3 rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/[0.03]">
                  <span className="min-w-0"><span className="block truncate text-sm font-semibold text-stone-800 dark:text-stone-100">{b.name}</span><span className="text-[11px] text-stone-400">priority {b.priority_rank}{b.has_api_key ? ' · key stored' : ''}</span></span>
                  <Toggle label={`${b.name} active`} checked={b.is_active} onChange={() => void toggle(b)} />
                </li>
              ))}
            </ul>
          )}
        </Async>
        <form onSubmit={add} className="grid grid-cols-1 gap-2.5 sm:grid-cols-[1fr_100px_1fr_auto] sm:items-end">
          <Field label="Name"><TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Pinnacle" /></Field>
          <Field label="Priority"><NumberInput value={form.priority_rank} onChange={(e) => setForm({ ...form, priority_rank: e.target.value })} /></Field>
          <Field label="Base URL"><TextInput type="url" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="optional" /></Field>
          <Button type="submit" icon="add" disabled={!form.name.trim()}>Add</Button>
        </form>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// MULTI-SPORT ENGINE
// ---------------------------------------------------------------------------
const SportsEngine = () => {
  const [sport, setSport] = useState<(typeof SPORTS)[number]>('cricket');
  const cfg = useResource(`sports:${sport}`, () => apiClient.get<SportConfig>(`/sports/${sport}/config`));
  const [json, setJson] = useState('');
  const [calc, setCalc] = useState<Record<string, string>>({ resources: '62.5', target: '280', home_rating: '112', away_rating: '108', home_pace: '99', away_pace: '101', league_pace: '100', serve: '0.64', elo: '1850', opp_elo: '1780' });
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  useEffect(() => { if (cfg.data) setJson(JSON.stringify(cfg.data.config, null, 2)); setResult(null); }, [cfg.data]);

  const saveConfig = () => {
    let parsed: unknown;
    try { parsed = JSON.parse(json); } catch { void runMutation(() => Promise.reject(new Error('Config is not valid JSON')), { errorTitle: 'Invalid config' }); return; }
    void runMutation(() => apiClient.put(`/sports/${sport}/config`, { is_active: cfg.data?.is_active ?? true, config: parsed }), { invalidate: ['sports'], success: `${sport} model config saved`, errorTitle: 'Config rejected' });
  };
  const toggle = (v: boolean) => cfg.data && void runMutation(() => apiClient.put(`/sports/${sport}/config`, { is_active: v, config: cfg.data!.config }), { invalidate: ['sports'], success: `${sport} ${v ? 'enabled' : 'disabled'}` });
  const run = async () => {
    const f = (k: string) => num(calc[k]);
    const call = sport === 'cricket'
      ? () => apiClient.post<Record<string, unknown>>('/sports/cricket/calculate-dls', { resources_left_pct: f('resources'), original_target: f('target') })
      : sport === 'basketball'
        ? () => apiClient.post<Record<string, unknown>>('/sports/basketball/calculate-spread', { home_rating: f('home_rating'), away_rating: f('away_rating'), home_pace: f('home_pace'), away_pace: f('away_pace'), league_avg_pace: f('league_pace') })
        : () => apiClient.post<Record<string, unknown>>('/sports/tennis/calculate-game-prob', { base_serve_prob: f('serve'), player_surface_elo: f('elo'), opponent_surface_elo: f('opp_elo') });
    const res = await runMutation(call, { errorTitle: 'Model rejected the inputs' });
    if (res) setResult(res);
  };
  const input = (k: string, label: string) => <Field key={k} label={label}><NumberInput value={calc[k]} onChange={(e) => setCalc({ ...calc, [k]: e.target.value })} /></Field>;

  return (
    <Panel title="Multi-sport engine" icon="sports" className="lg:col-span-12" updatedAt={cfg.updatedAt}>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Segmented label="Sport" value={sport} onChange={setSport} options={SPORTS.map((s) => ({ value: s, label: <span className="capitalize">{s}</span> }))} />
        {cfg.data && <span className="flex items-center gap-2 text-xs text-stone-500">Model active <Toggle label={`${sport} active`} checked={cfg.data.is_active} onChange={(v) => toggle(v)} /></span>}
      </div>
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Async resource={cfg}>
          {() => (
            <div className="flex flex-col gap-2">
              <Field label="Model parameters (JSON)">
                <textarea value={json} onChange={(e) => setJson(e.target.value)} rows={9} spellCheck={false} className="w-full rounded-xl bg-white px-3 py-2 font-mono text-xs text-stone-800 ring-1 ring-inset ring-stone-900/10 focus:outline-none focus:ring-2 focus:ring-[var(--accent)] dark:bg-white/[0.04] dark:text-stone-100 dark:ring-white/10" />
              </Field>
              <Button icon="save" onClick={saveConfig}>Save parameters</Button>
            </div>
          )}
        </Async>
        <div className="flex flex-col gap-3 rounded-xl bg-stone-50 p-4 dark:bg-white/[0.03]">
          <p className="text-sm font-semibold capitalize text-stone-800 dark:text-stone-100">{sport === 'cricket' ? 'DLS par score' : sport === 'basketball' ? 'Pace-adjusted spread' : 'Service game probability'}</p>
          <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
            {sport === 'cricket' && [input('resources', 'Resources left %'), input('target', 'Original target')]}
            {sport === 'basketball' && [input('home_rating', 'Home rating'), input('away_rating', 'Away rating'), input('home_pace', 'Home pace'), input('away_pace', 'Away pace'), input('league_pace', 'League pace')]}
            {sport === 'tennis' && [input('serve', 'Base serve prob'), input('elo', 'Player surface Elo'), input('opp_elo', 'Opponent Elo')]}
          </div>
          <Button variant="primary" icon="calculate" onClick={() => void run()}>Calculate</Button>
          {result && <KeyValues items={Object.entries(result).map(([k, v]) => ({ label: humanize(k), value: typeof v === 'number' ? (v > 0 && v < 1 ? formatRatioPct(v) : v.toFixed(2)) : String(v) }))} />}
        </div>
      </div>
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// OMNI ADMIN (token-gated)
// ---------------------------------------------------------------------------
const OmniAdmin = () => {
  const [token, setToken] = useState('');
  const [health, setHealth] = useState<OmniHealth | null>(null);
  const [error, setError] = useState('');
  const load = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    // The admin token is a server secret: sent once in a header, never stored.
    const res = await fetch(`${import.meta.env.VITE_API_BASE_URL || '/api/v1'}/admin/omni/health`, { headers: { 'X-Omni-Admin-Token': token } });
    if (!res.ok) {
      setHealth(null);
      setError(res.status === 401 ? 'Invalid admin token' : res.status === 503 ? 'Omni admin is not configured on the server' : `HTTP ${res.status}`);
      return;
    }
    setHealth(await res.json());
  };
  return (
    <Panel title="Omni-ingestion providers" icon="hub" className="lg:col-span-12" subtitle="admin token required">
      <form onSubmit={load} className="mb-4 flex items-end gap-2">
        <Field label="X-Omni-Admin-Token" className="max-w-sm flex-1"><TextInput type="password" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" /></Field>
        <Button type="submit" icon="visibility" disabled={!token} className="mb-0">Load health</Button>
      </form>
      {error && <p className="text-xs text-rose-600">{error}</p>}
      {health ? (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            <Pill tone="neutral">{health.total} providers</Pill>
            <Pill tone="good">{health.active} active</Pill>
            <Pill tone={health.degraded ? 'warning' : 'neutral'}>{health.degraded} degraded</Pill>
            <Pill tone={health.open_circuits ? 'critical' : 'neutral'}>{health.open_circuits} open circuits</Pill>
            <Pill tone={health.redis_available ? 'good' : 'critical'}>Redis {health.redis_available ? 'up' : 'down'}</Pill>
          </div>
          {health.providers.length === 0 ? <EmptyState icon="hub" title="No providers registered" detail="Register providers through POST /api/v1/admin/omni/providers." /> : (
            <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {health.providers.map((p) => (
                <li key={p.provider_id} className="flex items-center justify-between gap-2 rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/[0.03]">
                  <span className="min-w-0"><span className="block truncate text-sm font-semibold">{p.provider_name}</span><span className="text-[11px] text-stone-400">category {p.category_code} · breaker {p.breaker_state}</span></span>
                  <StatusBadge status={p.is_active ? p.health_status : 'DISABLED'} />
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : !error && <p className="text-xs text-stone-400">Provider status, circuit breakers and Redis availability for the ingestion workers.</p>}
    </Panel>
  );
};

// ---------------------------------------------------------------------------
// NAMED EXPORT: CONTROL PANEL
// ---------------------------------------------------------------------------
export const ControlPanel = () => {
  const controls = useControls();
  const halted = useSystemStore((s) => s.halted);
  const s = controls.data;
  const [resumeAt, setResumeAt] = useState('500');
  const [tab, setTab] = useState<ControlTab>('system');

  return (
    <Page>
      <CommanderHero
        commander="KAUTILYA"
        headline={halted ? 'SYSTEM HALTED. Every order is refused until trading resumes.' : `SYSTEM CONTROL. Max bet ${formatINR(s?.max_bet_size)}, daily exposure ${formatINR(s?.max_daily_exposure)}, Kelly ×${s?.default_kelly_fraction ?? '—'}.`}
        motif={MOTIFS.sliders}
        detail={s ? <>{s.app_version} · {s.build_info} · operator {s.developer_name}{s.last_emergency_stop_at ? ` · last emergency stop ${formatAgo(s.last_emergency_stop_at)}` : ''}</> : undefined}
        actions={
          halted ? (
            <div className="flex flex-wrap items-end gap-2">
              <Field label="Resume with daily exposure ₹"><NumberInput value={resumeAt} min="1" onChange={(e) => setResumeAt(e.target.value)} className="w-40" /></Field>
              <Button variant="primary" icon="play_circle" disabled={!(num(resumeAt) > 0)} onClick={() => void resumeTrading(num(resumeAt))}>Resume trading</Button>
            </div>
          ) : (
            <ConfirmButton variant="danger" icon="emergency_home" confirmLabel="Halt all trading?" onConfirm={() => void emergencyStop()} className="px-6 py-3 text-base">
              Global kill switch
            </ConfirmButton>
          )
        }
      />
      <div className="lg:col-span-12">
        <Segmented<ControlTab> options={TABS} value={tab} onChange={setTab} label="Control Panel sections" />
      </div>
      {tab === 'system' ? (
        <>
          <Async resource={controls} skeletonRows={4}>
            {(settings) => (
              <>
                <GlobalOverrides settings={settings} />
                <Credentials settings={settings} />
              </>
            )}
          </Async>
          <FleetCommand />
          <ExchangeAccounts />
          <Bookmakers />
          <SportsEngine />
          <OmniAdmin />
        </>
      ) : tab === 'terminal' ? (
        <SniperTerminal />
      ) : tab === 'portfolio' ? (
        <ActivePortfolio />
      ) : tab === 'hive' ? (
        <HiveBots />
      ) : tab === 'lab' ? (
        <Suspense fallback={<div className="lg:col-span-12"><Skeleton rows={6} /></div>}>
          <QuantLab />
        </Suspense>
      ) : (
        <>
          <RiskManagement />
          <Async resource={controls} skeletonRows={2}>
            {(settings) => <StakeCapPanel settings={settings} />}
          </Async>
        </>
      )}
    </Page>
  );
};
