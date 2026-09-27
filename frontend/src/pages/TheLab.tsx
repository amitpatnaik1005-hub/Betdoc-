import { useState, type ChangeEvent, type CSSProperties, type FormEvent } from 'react';
import { apiClient } from '../api/client';

// ---------- Types (mirror backend/app/schemas/math.py) ----------
export type Selection = 'HOME' | 'DRAW' | 'AWAY';

export interface MatchContextPayload {
  home_team: string;
  away_team: string;
  home_xg: number | null;
  away_xg: number | null;
  home_elo: number | null;
  away_elo: number | null;
  bookmaker_odds: Partial<Record<Selection, number>> | null;
}

export interface PredictionResult {
  home_win_prob: number;
  draw_prob: number;
  away_win_prob: number;
  most_likely_scoreline: string;
  confidence_score: number;
}

export interface ValueBetFlag {
  selection: Selection;
  true_prob: number;
  bookmaker_odds: number;
  expected_value: number;
  kelly_stake_fraction: number;
}

export interface PredictionResponse {
  prediction: PredictionResult;
  value_bets: ValueBetFlag[];
}

interface FormState {
  home_team: string;
  away_team: string;
  home_xg: string;
  away_xg: string;
  home_elo: string;
  away_elo: string;
  odds_home: string;
  odds_draw: string;
  odds_away: string;
}

interface ParsedError {
  status: number | null;
  message: string;
}

const EMPTY_FORM: FormState = {
  home_team: '', away_team: '', home_xg: '', away_xg: '',
  home_elo: '', away_elo: '', odds_home: '', odds_draw: '', odds_away: '',
};

const SAMPLE_FORM: FormState = {
  home_team: 'Arsenal', away_team: 'Chelsea', home_xg: '1.85', away_xg: '1.10',
  home_elo: '1850', away_elo: '1780', odds_home: '2.05', odds_draw: '3.60', odds_away: '3.90',
};

// ---------- Helpers ----------
const toNum = (value: string): number | null => {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
};

const pct = (v: number, digits = 1): string => `${(v * 100).toFixed(digits)}%`;

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null;

function extractDetail(data: unknown): string | null {
  if (!isRecord(data)) return typeof data === 'string' ? data : null;
  const detail = data.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (isRecord(d) && typeof d.msg === 'string' ? d.msg : JSON.stringify(d)))
      .join('; ');
  }
  return null;
}

function parseError(err: unknown): ParsedError {
  if (isRecord(err)) {
    const response = isRecord(err.response) ? err.response : null;
    let status: number | null =
      typeof response?.status === 'number' ? response.status
        : typeof err.status === 'number' ? err.status : null;
    const detail =
      extractDetail(response?.data) ?? extractDetail(err.data) ??
      extractDetail(err.body) ?? extractDetail(err);
    const rawMessage = typeof err.message === 'string' ? err.message : null;
    if (status === null && rawMessage) {
      const match = rawMessage.match(/\b([45]\d\d)\b/);
      if (match) status = Number(match[1]);
    }
    return { status, message: detail ?? rawMessage ?? 'Unexpected error' };
  }
  if (typeof err === 'string') return { status: null, message: err };
  return { status: null, message: 'Unexpected error' };
}

function buildPayload(form: FormState): MatchContextPayload {
  const odds: Partial<Record<Selection, number>> = {};
  const h = toNum(form.odds_home);
  const d = toNum(form.odds_draw);
  const a = toNum(form.odds_away);
  if (h !== null) odds.HOME = h;
  if (d !== null) odds.DRAW = d;
  if (a !== null) odds.AWAY = a;

  return {
    home_team: form.home_team.trim() || 'Home',
    away_team: form.away_team.trim() || 'Away',
    home_xg: toNum(form.home_xg),
    away_xg: toNum(form.away_xg),
    home_elo: toNum(form.home_elo),
    away_elo: toNum(form.away_elo),
    bookmaker_odds: Object.keys(odds).length > 0 ? odds : null,
  };
}

// ---------- Styles ----------
const s: Record<string, CSSProperties> = {
  page: { maxWidth: 960, margin: '0 auto', padding: 24, fontFamily: 'Inter, system-ui, sans-serif', color: '#e5e7eb' },
  header: { marginBottom: 20 },
  title: { fontSize: 28, fontWeight: 800, margin: 0, letterSpacing: -0.5 },
  subtitle: { color: '#9ca3af', marginTop: 4 },
  card: { background: '#111827', border: '1px solid #1f2937', borderRadius: 14, padding: 20, marginBottom: 20 },
  sectionLabel: { fontSize: 12, textTransform: 'uppercase', letterSpacing: 1.2, color: '#818cf8', fontWeight: 700, margin: '14px 0 8px' },
  grid2: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 },
  grid3: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 },
  label: { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, color: '#d1d5db' },
  input: { background: '#0b1220', border: '1px solid #374151', borderRadius: 8, padding: '9px 11px', color: '#f9fafb', fontSize: 14, outline: 'none' },
  actions: { display: 'flex', gap: 10, marginTop: 18, flexWrap: 'wrap' },
  primary: { background: 'linear-gradient(135deg,#6366f1,#8b5cf6)', color: '#fff', border: 'none', borderRadius: 8, padding: '10px 18px', fontWeight: 700, cursor: 'pointer' },
  secondary: { background: 'transparent', color: '#c7d2fe', border: '1px solid #4f46e5', borderRadius: 8, padding: '10px 16px', cursor: 'pointer' },
  error: { background: '#3f1d1d', border: '1px solid #b91c1c', color: '#fecaca', borderRadius: 10, padding: 14, marginBottom: 20 },
  errorTitle: { fontWeight: 800, marginBottom: 4 },
  probRow: { marginBottom: 14 },
  probHead: { display: 'flex', justifyContent: 'space-between', fontSize: 14, marginBottom: 6 },
  barTrack: { height: 12, background: '#1f2937', borderRadius: 999, overflow: 'hidden' },
  stats: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 18 },
  stat: { background: '#0b1220', border: '1px solid #1f2937', borderRadius: 10, padding: 14 },
  statLabel: { fontSize: 12, color: '#9ca3af', textTransform: 'uppercase', letterSpacing: 1 },
  statValue: { fontSize: 24, fontWeight: 800, marginTop: 4 },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 14 },
  th: { textAlign: 'left', padding: '8px 10px', color: '#9ca3af', borderBottom: '1px solid #374151', fontWeight: 600 },
  td: { padding: '10px', borderBottom: '1px solid #1f2937' },
  badge: { background: '#064e3b', color: '#6ee7b7', borderRadius: 6, padding: '2px 8px', fontWeight: 700, fontSize: 12 },
  muted: { color: '#9ca3af', fontSize: 14 },
};

const barFill = (value: number, color: string): CSSProperties => ({
  width: `${Math.max(0, Math.min(100, value * 100))}%`,
  height: '100%',
  background: color,
  borderRadius: 999,
  transition: 'width 400ms ease',
});

// ---------- Component ----------
export default function TheLab() {
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ParsedError | null>(null);
  const [result, setResult] = useState<PredictionResponse | null>(null);
  const [lastPayload, setLastPayload] = useState<MatchContextPayload | null>(null);

  const update = (key: keyof FormState) => (e: ChangeEvent<HTMLInputElement>) =>
    setForm((prev) => ({ ...prev, [key]: e.target.value }));

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const payload = buildPayload(form);
    setLoading(true);
    setError(null);
    setResult(null);
    setLastPayload(payload);
    try {
      const response = await apiClient.post<PredictionResponse>('/engine/predict', payload);
      setResult(response);
    } catch (err: unknown) {
      setError(parseError(err));
    } finally {
      setLoading(false);
    }
  };

  const oddsCount = lastPayload?.bookmaker_odds ? Object.keys(lastPayload.bookmaker_odds).length : 0;

  const field = (key: keyof FormState, label: string, placeholder: string, step = 'any') => (
    <label style={s.label}>
      {label}
      <input
        style={s.input}
        type={key.includes('team') ? 'text' : 'number'}
        step={step}
        min={key.includes('team') ? undefined : 0}
        placeholder={placeholder}
        value={form[key]}
        onChange={update(key)}
      />
    </label>
  );

  return (
    <div style={s.page}>
      <header style={s.header}>
        <h1 style={s.title}>🧪 PANINI Test Bench</h1>
        <p style={s.subtitle}>Poisson · Dixon-Coles · Elo ensemble with 3-way value detection</p>
      </header>

      <form style={s.card} onSubmit={handleSubmit}>
        <div style={s.sectionLabel}>Fixture</div>
        <div style={s.grid2}>
          {field('home_team', 'Home team', 'Arsenal')}
          {field('away_team', 'Away team', 'Chelsea')}
        </div>

        <div style={s.sectionLabel}>Expected goals (xG)</div>
        <div style={s.grid2}>
          {field('home_xg', 'Home xG', '1.85', '0.01')}
          {field('away_xg', 'Away xG', '1.10', '0.01')}
        </div>

        <div style={s.sectionLabel}>Elo ratings</div>
        <div style={s.grid2}>
          {field('home_elo', 'Home Elo', '1850', '1')}
          {field('away_elo', 'Away Elo', '1780', '1')}
        </div>

        <div style={s.sectionLabel}>Bookmaker odds (decimal)</div>
        <div style={s.grid3}>
          {field('odds_home', 'HOME', '2.05', '0.01')}
          {field('odds_draw', 'DRAW', '3.60', '0.01')}
          {field('odds_away', 'AWAY', '3.90', '0.01')}
        </div>

        <div style={s.actions}>
          <button type="submit" style={{ ...s.primary, opacity: loading ? 0.6 : 1 }} disabled={loading}>
            {loading ? 'Running PANINI…' : 'Run Prediction'}
          </button>
          <button type="button" style={s.secondary} onClick={() => setForm(SAMPLE_FORM)} disabled={loading}>
            Load sample
          </button>
          <button
            type="button"
            style={s.secondary}
            onClick={() => { setForm(EMPTY_FORM); setResult(null); setError(null); }}
            disabled={loading}
          >
            Reset
          </button>
        </div>
      </form>

      {error && (
        <div style={s.error} role="alert">
          <div style={s.errorTitle}>
            {error.status === 400 ? '⚠️ HTTP 400: Bad Request'
              : error.status ? `Error ${error.status}` : 'Request failed'}
          </div>
          <div>{error.message}</div>
          {error.status === 400 && (
            <div style={{ marginTop: 6, fontSize: 13 }}>
              Provide both xG values and/or both Elo ratings so at least one model can run.
            </div>
          )}
        </div>
      )}

      {result && (
        <>
          <section style={s.card}>
            <div style={s.sectionLabel}>Ensemble probabilities</div>
            {([
              ['Home win', result.prediction.home_win_prob, '#22c55e'],
              ['Draw', result.prediction.draw_prob, '#eab308'],
              ['Away win', result.prediction.away_win_prob, '#3b82f6'],
            ] as const).map(([label, value, color]) => (
              <div key={label} style={s.probRow}>
                <div style={s.probHead}>
                  <span>{label}</span>
                  <strong>{pct(value)}</strong>
                </div>
                <div style={s.barTrack}><div style={barFill(value, color)} /></div>
              </div>
            ))}
            <div style={s.stats}>
              <div style={s.stat}>
                <div style={s.statLabel}>Most likely scoreline</div>
                <div style={s.statValue}>{result.prediction.most_likely_scoreline}</div>
              </div>
              <div style={s.stat}>
                <div style={s.statLabel}>Confidence</div>
                <div style={s.statValue}>{pct(result.prediction.confidence_score)}</div>
              </div>
            </div>
          </section>

          <section style={s.card}>
            <div style={s.sectionLabel}>Value bets (EV &gt; 2%)</div>
            {result.value_bets.length > 0 ? (
              <table style={s.table}>
                <thead>
                  <tr>
                    <th style={s.th}>Selection</th>
                    <th style={s.th}>Model prob</th>
                    <th style={s.th}>Odds</th>
                    <th style={s.th}>Implied</th>
                    <th style={s.th}>EV</th>
                    <th style={s.th}>Kelly stake</th>
                  </tr>
                </thead>
                <tbody>
                  {result.value_bets.map((bet) => (
                    <tr key={bet.selection}>
                      <td style={s.td}><span style={s.badge}>{bet.selection}</span></td>
                      <td style={s.td}>{pct(bet.true_prob)}</td>
                      <td style={s.td}>{bet.bookmaker_odds.toFixed(2)}</td>
                      <td style={s.td}>{pct(1 / bet.bookmaker_odds)}</td>
                      <td style={{ ...s.td, color: '#6ee7b7', fontWeight: 700 }}>+{pct(bet.expected_value, 2)}</td>
                      <td style={s.td}>{pct(bet.kelly_stake_fraction, 2)} of bankroll</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p style={s.muted}>
                {oddsCount === 0 ? 'No bookmaker odds supplied.'
                  : oddsCount < 3 ? 'Value detection skipped: HOME, DRAW and AWAY odds are all required.'
                  : 'No selection clears the +2% EV threshold.'}
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
