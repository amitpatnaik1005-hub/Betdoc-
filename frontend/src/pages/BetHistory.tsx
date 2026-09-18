/**
 * frontend/src/pages/BetHistory.tsx — BetDoc Trade Ledger.
 *
 * Monetary invariant: every amount crossing the API boundary is an integer
 * count of paise (1/100 ₹). Rupee floats exist only at the formatting layer.
 *
 * Wire discipline: the backend emits raw `sport_type` strings (including
 * "other" and odds-feed keys like "soccer_epl"). Rows are validated at the
 * wire shape only, then normalised into the domain `Sport` union, so an
 * unrecognised sport degrades to `other` instead of silently dropping the row.
 */
import { useEffect, useMemo, useState, type ReactElement } from 'react';
import { AnimatePresence, motion } from 'framer-motion';

/* --------------------------------------------------------------- API base */

/**
 * Default is relative so the Vite dev proxy (and any same-origin production
 * reverse proxy) serves the API without CORS. Override with
 * VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1/board to hit FastAPI directly,
 * which then requires CORSMiddleware on the backend.
 */
export const API_BASE: string = (
  import.meta.env.VITE_API_BASE_URL ?? '/api/v1/board'
).replace(/\/+$/, '');

/* ------------------------------------------------------------------ types */

export const BET_STATUSES = [
  'pending',
  'won',
  'lost',
  'void',
  'half_won',
  'half_lost',
] as const;

export type BetStatus = (typeof BET_STATUSES)[number];

export const SPORTS = [
  'cricket',
  'football',
  'tennis',
  'basketball',
  'esports',
  'other',
] as const;

export type Sport = (typeof SPORTS)[number];

export interface BetRecord {
  readonly id: string;
  /** ISO-8601 instant the wager was struck. */
  readonly placed_at: string;
  /** ISO-8601 instant of settlement; null while pending. */
  readonly settled_at: string | null;
  /** Normalised domain sport, never a raw feed key. */
  readonly sport: Sport;
  readonly event_name: string;
  readonly market: string;
  readonly selection: string;
  readonly model_name: string;
  /** Model's estimated edge at strike time, in percentage points. */
  readonly model_edge_pct: number;
  /** Integer paise. Never a float. */
  readonly stake_paise: number;
  /** European/decimal odds at strike time. */
  readonly odds_decimal: number;
  readonly status: BetStatus;
  /**
   * Authoritative gross return in paise from the settlement engine.
   * Null while pending, or when the backend defers to client derivation.
   */
  readonly payout_paise: number | null;
}

/** Exactly what FastAPI serialises: `sport` is an unconstrained string. */
interface BetRecordWire {
  readonly id: string;
  readonly placed_at: string;
  readonly settled_at: string | null;
  readonly sport: string;
  readonly event_name: string;
  readonly market: string;
  readonly selection: string;
  readonly model_name: string;
  readonly model_edge_pct: number;
  readonly stake_paise: number;
  readonly odds_decimal: number;
  readonly status: BetStatus;
  readonly payout_paise: number | null;
}

interface Settlement {
  /** Gross return including stake. For pending rows this is the potential return. */
  readonly returnPaise: number;
  /** Net profit/loss. Zero for pending and void rows. */
  readonly profitPaise: number;
}

interface LedgerKpis {
  readonly grossExposurePaise: number;
  readonly netPnlPaise: number;
  readonly strikeRate: number | null;
  readonly totalVolumePaise: number;
  readonly openCount: number;
  readonly settledCount: number;
  readonly roi: number | null;
}

/* -------------------------------------------------------- sport normaliser */

/** Feed and backend aliases mapped onto the domain union. */
const SPORT_ALIASES: Readonly<Record<string, Sport>> = {
  cricket: 'cricket',
  football: 'football',
  soccer: 'football',
  soccer_epl: 'football',
  soccer_uefa_champs_league: 'football',
  epl: 'football',
  laliga: 'football',
  seriea: 'football',
  tennis: 'tennis',
  tennis_atp: 'tennis',
  tennis_wta: 'tennis',
  basketball: 'basketball',
  basketball_nba: 'basketball',
  nba: 'basketball',
  esports: 'esports',
  esports_lol: 'esports',
  esports_csgo: 'esports',
  other: 'other',
};

const isSport = (value: string): value is Sport =>
  (SPORTS as readonly string[]).includes(value);

/**
 * Collapses any backend sport key to a domain `Sport`.
 * Unknown keys resolve to `other` rather than invalidating the record.
 */
export const normaliseSport = (raw: string): Sport => {
  const key = raw.trim().toLowerCase().replace(/[\s-]+/g, '_');
  if (key.length === 0) return 'other';
  if (isSport(key)) return key;

  const direct = SPORT_ALIASES[key];
  if (direct !== undefined) return direct;

  // Prefix match handles compound feed keys, e.g. "soccer_spain_la_liga".
  for (const [alias, sport] of Object.entries(SPORT_ALIASES)) {
    if (key.startsWith(`${alias}_`)) return sport;
  }
  return 'other';
};

/* -------------------------------------------------------- money primitives */

/** Integer paise → rupee decimal. Exported for the header bankroll widget. */
export const paiseToRupees = (paise: number): number => paise / 100;

const INR = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** ₹1,00,000.00 — Indian grouping, always two decimals. */
export const formatINR = (rupees: number): string =>
  INR.format(Number.isFinite(rupees) ? rupees : 0);

/** Signed variant for P&L cells: +₹12,450.00 / -₹3,200.00 */
export const formatSignedINR = (rupees: number): string => {
  const safe = Number.isFinite(rupees) ? rupees : 0;
  const sign = safe > 0 ? '+' : safe < 0 ? '-' : '';
  return `${sign}${INR.format(Math.abs(safe))}`;
};

/** Decimal odds, invariably two places: 1.90, 2.05, 11.00 */
export const formatOdds = (odds: number): string =>
  (Number.isFinite(odds) ? odds : 0).toFixed(2);

const PCT = new Intl.NumberFormat('en-IN', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const formatPct = (ratio: number | null): string =>
  ratio === null ? '—' : `${PCT.format(ratio * 100)}%`;

const DATE_FMT = new Intl.DateTimeFormat('en-IN', {
  day: '2-digit',
  month: 'short',
  timeZone: 'Asia/Kolkata',
});

const TIME_FMT = new Intl.DateTimeFormat('en-IN', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'Asia/Kolkata',
});

/* ------------------------------------------------------- settlement engine */

const halve = (paise: number): number => Math.round(paise / 2);
const grossAt = (stakePaise: number, odds: number): number => Math.round(stakePaise * odds);

/**
 * Derives the return/profit pair for a wager.
 * `payout_paise` from the backend always wins for settled rows; local
 * derivation is the fallback when the backend defers.
 */
export const settlementOf = (bet: BetRecord): Settlement => {
  const stake = bet.stake_paise;

  if (bet.status !== 'pending' && bet.payout_paise !== null) {
    return { returnPaise: bet.payout_paise, profitPaise: bet.payout_paise - stake };
  }

  switch (bet.status) {
    case 'pending':
      return { returnPaise: grossAt(stake, bet.odds_decimal), profitPaise: 0 };
    case 'won': {
      const gross = grossAt(stake, bet.odds_decimal);
      return { returnPaise: gross, profitPaise: gross - stake };
    }
    case 'half_won': {
      // Asian line split: half the stake wins at odds, half is refunded.
      const half = halve(stake);
      const gross = half + grossAt(stake - half, bet.odds_decimal);
      return { returnPaise: gross, profitPaise: gross - stake };
    }
    case 'half_lost': {
      // Half the stake is lost, half refunded — no upside.
      const refund = stake - halve(stake);
      return { returnPaise: refund, profitPaise: refund - stake };
    }
    case 'lost':
      return { returnPaise: 0, profitPaise: -stake };
    case 'void':
      return { returnPaise: stake, profitPaise: 0 };
    default: {
      const exhaustive: never = bet.status;
      throw new Error(`Unhandled bet status: ${String(exhaustive)}`);
    }
  }
};

const computeKpis = (bets: readonly BetRecord[]): LedgerKpis => {
  let grossExposurePaise = 0;
  let netPnlPaise = 0;
  let totalVolumePaise = 0;
  let settledStakePaise = 0;
  let openCount = 0;
  let settledCount = 0;
  let decidedCount = 0;
  let winUnits = 0;

  for (const bet of bets) {
    totalVolumePaise += bet.stake_paise;

    if (bet.status === 'pending') {
      grossExposurePaise += bet.stake_paise;
      openCount += 1;
      continue;
    }

    settledCount += 1;
    netPnlPaise += settlementOf(bet).profitPaise;

    // Voids are stake-neutral and excluded from strike rate / ROI denominators.
    if (bet.status === 'void') continue;

    settledStakePaise += bet.stake_paise;
    decidedCount += 1;
    if (bet.status === 'won') winUnits += 1;
    else if (bet.status === 'half_won') winUnits += 0.5;
    else if (bet.status === 'half_lost') winUnits += 0.5;
  }

  return {
    grossExposurePaise,
    netPnlPaise,
    totalVolumePaise,
    openCount,
    settledCount,
    strikeRate: decidedCount > 0 ? winUnits / decidedCount : null,
    roi: settledStakePaise > 0 ? netPnlPaise / settledStakePaise : null,
  };
};

/* ---------------------------------------------------------------- mock data */

const MOCK_BETS: readonly BetRecord[] = [
  {
    id: 'BD-24817',
    placed_at: '2026-09-14T09:12:44Z',
    settled_at: null,
    sport: 'cricket',
    event_name: 'India vs Australia · 2nd ODI',
    market: 'Match Winner',
    selection: 'India',
    model_name: 'poisson-hier-v4',
    model_edge_pct: 4.8,
    stake_paise: 2_500_000,
    odds_decimal: 1.87,
    status: 'pending',
    payout_paise: null,
  },
  {
    id: 'BD-24805',
    placed_at: '2026-09-13T18:40:10Z',
    settled_at: null,
    sport: 'football',
    event_name: 'Arsenal vs Man City · EPL',
    market: 'Asian Handicap',
    selection: 'Arsenal +0.25',
    model_name: 'dixon-coles-v7',
    model_edge_pct: 3.1,
    stake_paise: 1_200_000,
    odds_decimal: 2.04,
    status: 'pending',
    payout_paise: null,
  },
  {
    id: 'BD-24762',
    placed_at: '2026-09-12T14:05:00Z',
    settled_at: '2026-09-12T17:58:21Z',
    sport: 'football',
    event_name: 'Real Madrid vs Sevilla · LaLiga',
    market: 'Over/Under 2.5',
    selection: 'Over 2.5',
    model_name: 'dixon-coles-v7',
    model_edge_pct: 6.2,
    stake_paise: 3_000_000,
    odds_decimal: 1.95,
    status: 'won',
    payout_paise: 5_850_000,
  },
  {
    id: 'BD-24741',
    placed_at: '2026-09-11T11:22:35Z',
    settled_at: '2026-09-11T15:10:02Z',
    sport: 'tennis',
    event_name: 'Sinner vs Alcaraz · ATP Finals',
    market: 'Set Betting',
    selection: 'Alcaraz 2-1',
    model_name: 'elo-bayes-v2',
    model_edge_pct: 8.4,
    stake_paise: 800_000,
    odds_decimal: 3.60,
    status: 'lost',
    payout_paise: 0,
  },
  {
    id: 'BD-24718',
    placed_at: '2026-09-10T16:47:12Z',
    settled_at: '2026-09-10T19:31:44Z',
    sport: 'football',
    event_name: 'Inter vs Juventus · Serie A',
    market: 'Asian Handicap',
    selection: 'Inter -0.75',
    model_name: 'dixon-coles-v7',
    model_edge_pct: 2.7,
    stake_paise: 2_000_000,
    odds_decimal: 1.92,
    status: 'half_won',
    payout_paise: 2_920_000,
  },
  {
    id: 'BD-24690',
    placed_at: '2026-09-09T07:15:58Z',
    settled_at: '2026-09-09T11:02:09Z',
    sport: 'cricket',
    event_name: 'Mumbai Indians vs CSK · IPL',
    market: 'Top Batsman',
    selection: 'S. Gill',
    model_name: 'poisson-hier-v4',
    model_edge_pct: 11.3,
    stake_paise: 450_000,
    odds_decimal: 6.50,
    status: 'lost',
    payout_paise: 0,
  },
  {
    id: 'BD-24655',
    placed_at: '2026-09-08T20:03:27Z',
    settled_at: '2026-09-08T22:44:55Z',
    sport: 'basketball',
    event_name: 'Lakers vs Celtics · NBA',
    market: 'Spread',
    selection: 'Celtics -3.5',
    model_name: 'pace-adj-mcmc-v1',
    model_edge_pct: 1.9,
    stake_paise: 1_750_000,
    odds_decimal: 1.90,
    status: 'half_lost',
    payout_paise: 875_000,
  },
  {
    id: 'BD-24601',
    placed_at: '2026-09-07T12:30:00Z',
    settled_at: '2026-09-07T12:58:13Z',
    sport: 'esports',
    event_name: 'G2 vs T1 · Worlds Group B',
    market: 'Map 1 Winner',
    selection: 'T1',
    model_name: 'elo-bayes-v2',
    model_edge_pct: 5.5,
    stake_paise: 600_000,
    odds_decimal: 1.66,
    status: 'void',
    payout_paise: 600_000,
  },
  {
    id: 'BD-24588',
    placed_at: '2026-09-06T15:19:41Z',
    settled_at: '2026-09-06T18:05:30Z',
    sport: 'cricket',
    event_name: 'England vs Pakistan · T20I',
    market: 'Total Sixes',
    selection: 'Over 12.5',
    model_name: 'poisson-hier-v4',
    model_edge_pct: 7.1,
    stake_paise: 1_000_000,
    odds_decimal: 2.10,
    status: 'won',
    payout_paise: 2_100_000,
  },
] as const;

/* ----------------------------------------------------------- API ingestion */

const isRecordObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const isBetStatus = (value: unknown): value is BetStatus =>
  typeof value === 'string' && (BET_STATUSES as readonly string[]).includes(value);

/** Validates the wire shape only. Sport is any string at this layer. */
const isBetRecordWire = (value: unknown): value is BetRecordWire => {
  if (!isRecordObject(value)) return false;
  const nullableNumber = (v: unknown): boolean => v === null || typeof v === 'number';
  const nullableString = (v: unknown): boolean => v === null || typeof v === 'string';

  return (
    typeof value.id === 'string' &&
    typeof value.placed_at === 'string' &&
    nullableString(value.settled_at) &&
    typeof value.sport === 'string' &&
    typeof value.event_name === 'string' &&
    typeof value.market === 'string' &&
    typeof value.selection === 'string' &&
    typeof value.model_name === 'string' &&
    typeof value.model_edge_pct === 'number' &&
    typeof value.stake_paise === 'number' &&
    Number.isFinite(value.stake_paise) &&
    typeof value.odds_decimal === 'number' &&
    Number.isFinite(value.odds_decimal) &&
    isBetStatus(value.status) &&
    nullableNumber(value.payout_paise)
  );
};

/** Wire → domain. Normalises sport and hardens numeric fields. */
const toBetRecord = (wire: BetRecordWire): BetRecord => ({
  id: wire.id,
  placed_at: wire.placed_at,
  settled_at: wire.settled_at,
  sport: normaliseSport(wire.sport),
  event_name: wire.event_name,
  market: wire.market,
  selection: wire.selection,
  model_name: wire.model_name,
  model_edge_pct: Number.isFinite(wire.model_edge_pct) ? wire.model_edge_pct : 0,
  stake_paise: Math.round(wire.stake_paise),
  odds_decimal: wire.odds_decimal > 0 ? wire.odds_decimal : 1,
  status: wire.status,
  payout_paise: wire.payout_paise === null ? null : Math.round(wire.payout_paise),
});

const byPlacedAtDesc = (a: BetRecord, b: BetRecord): number =>
  Date.parse(b.placed_at) - Date.parse(a.placed_at);

type LedgerSource = 'api' | 'mock';

interface LedgerQuery {
  readonly bets: readonly BetRecord[];
  readonly loading: boolean;
  readonly source: LedgerSource;
  readonly droppedRows: number;
}

const useLedger = (): LedgerQuery => {
  const [bets, setBets] = useState<readonly BetRecord[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [source, setSource] = useState<LedgerSource>('mock');
  const [droppedRows, setDroppedRows] = useState<number>(0);

  useEffect(() => {
    const controller = new AbortController();

    const load = async (): Promise<void> => {
      try {
        const response = await fetch(`${API_BASE}/history`, {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const payload: unknown = await response.json();
        if (!Array.isArray(payload)) throw new Error('Ledger payload is not an array');

        const wire = payload.filter(isBetRecordWire);
        // An empty ledger is a valid state; only a malformed payload falls back.
        if (payload.length > 0 && wire.length === 0) {
          throw new Error('Ledger payload failed wire validation');
        }

        setBets(wire.map(toBetRecord).sort(byPlacedAtDesc));
        setDroppedRows(payload.length - wire.length);
        setSource('api');
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        // Deterministic fallback keeps the ledger renderable when the API is down.
        setBets([...MOCK_BETS].sort(byPlacedAtDesc));
        setDroppedRows(0);
        setSource('mock');
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };

    void load();
    return () => controller.abort();
  }, []);

  return { bets, loading, source, droppedRows };
};

/* ------------------------------------------------------------ presentation */

const SPRING = { type: 'spring', stiffness: 400, damping: 25 } as const;

type AccentTone = 'positive' | 'neutral' | 'negative' | 'warning';

interface StatusStyle {
  readonly label: string;
  readonly pill: string;
  readonly dot: string;
}

const STATUS_STYLES: Record<BetStatus, StatusStyle> = {
  pending: { label: 'Pending', pill: 'bg-amber-100 text-amber-700', dot: 'bg-amber-500' },
  won: { label: 'Won', pill: 'bg-emerald-100 text-emerald-700', dot: 'bg-emerald-500' },
  half_won: { label: 'Half Won', pill: 'bg-teal-100 text-teal-700', dot: 'bg-teal-500' },
  lost: { label: 'Lost', pill: 'bg-rose-100 text-rose-700', dot: 'bg-rose-500' },
  half_lost: { label: 'Half Lost', pill: 'bg-rose-50 text-rose-600', dot: 'bg-rose-400' },
  void: { label: 'Void', pill: 'bg-slate-100 dark:bg-white/[0.08] text-slate-600 dark:text-[#A6A39E]', dot: 'bg-slate-400' },
};

const StatusBadge = ({ status }: { readonly status: BetStatus }): ReactElement => {
  const style = STATUS_STYLES[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-bold tracking-tight ${style.pill}`}
    >
      <span
        className={[
          'size-1.5 rounded-full',
          style.dot,
          status === 'pending' ? 'animate-pulse' : '',
        ].join(' ')}
        aria-hidden="true"
      />
      {style.label}
    </span>
  );
};

const SPORT_PRESENTATION: Record<Sport, { readonly label: string; readonly chip: string }> = {
  cricket: { label: 'CRK', chip: 'bg-emerald-50 text-emerald-600' },
  football: { label: 'FTB', chip: 'bg-indigo-50 text-indigo-600' },
  tennis: { label: 'TEN', chip: 'bg-amber-50 text-amber-600' },
  basketball: { label: 'BSK', chip: 'bg-orange-50 text-orange-600' },
  esports: { label: 'ESP', chip: 'bg-purple-50 text-purple-600' },
  other: { label: 'OTH', chip: 'bg-slate-100 dark:bg-white/[0.08] text-slate-500' },
};

interface KpiCardProps {
  readonly label: string;
  readonly value: string;
  readonly caption: string;
  readonly icon: string;
  readonly tone: AccentTone;
}

const KPI_ICON_TONE: Record<AccentTone, string> = {
  neutral: 'bg-indigo-50 text-indigo-600',
  positive: 'bg-emerald-50 text-emerald-600',
  negative: 'bg-rose-50 text-rose-600',
  warning: 'bg-amber-50 text-amber-600',
};

const KPI_VALUE_TONE: Record<AccentTone, string> = {
  neutral: 'text-slate-900 dark:text-slate-50',
  positive: 'text-emerald-600',
  negative: 'text-rose-600',
  warning: 'text-amber-600',
};

const KPI_SHADOW: Record<AccentTone, string> = {
  neutral: 'hover:shadow-[0_8px_30px_rgb(99,102,241,0.2)]',
  positive: 'hover:shadow-[0_8px_30px_rgb(16,185,129,0.2)]',
  negative: 'hover:shadow-[0_8px_30px_rgb(244,63,94,0.2)]',
  warning: 'hover:shadow-[0_8px_30px_rgb(245,158,11,0.2)]',
};

const KpiCard = ({ label, value, caption, icon, tone }: KpiCardProps): ReactElement => (
  <motion.div
    whileHover={{ y: -4 }}
    transition={SPRING}
    className={[
      'rounded-3xl bg-white dark:bg-[#161514] dark:border dark:border-white/[0.06] p-6 shadow-[0_4px_24px_rgb(0,0,0,0.04)] transition-shadow duration-300',
      KPI_SHADOW[tone],
    ].join(' ')}
  >
    <div className="flex items-start justify-between gap-3">
      <p className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-slate-400 dark:text-slate-500">
        {label}
      </p>
      <span className={`grid size-11 shrink-0 place-items-center rounded-2xl ${KPI_ICON_TONE[tone]}`}>
        <span className="material-symbols-outlined text-[20px] leading-none" aria-hidden="true">
          {icon}
        </span>
      </span>
    </div>
    <p
      className={`mt-4 text-[22px] font-semibold leading-none tracking-tight tabular-nums ${KPI_VALUE_TONE[tone]}`}
    >
      {value}
    </p>
    <p className="mt-2.5 text-[11px] font-medium text-slate-400 dark:text-slate-500">{caption}</p>
  </motion.div>
);

type StatusFilter = BetStatus | 'all';

const FILTERS: readonly { readonly key: StatusFilter; readonly label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'pending', label: 'Open' },
  { key: 'won', label: 'Won' },
  { key: 'half_won', label: 'Half Won' },
  { key: 'half_lost', label: 'Half Lost' },
  { key: 'lost', label: 'Lost' },
  { key: 'void', label: 'Void' },
] as const;

const TH =
  'px-6 py-4 text-left font-mono text-[9px] font-bold uppercase tracking-[0.18em] text-slate-400 dark:text-slate-500';
const TD = 'px-6 py-4 align-middle';

/* ------------------------------------------------------------------ screen */

const BetHistory = (): ReactElement => {
  const { bets, loading, source, droppedRows } = useLedger();
  const [filter, setFilter] = useState<StatusFilter>('all');

  // KPIs are computed over the full ledger, never the filtered view.
  const kpis = useMemo<LedgerKpis>(() => computeKpis(bets), [bets]);

  const rows = useMemo<readonly BetRecord[]>(
    () => (filter === 'all' ? bets : bets.filter((b) => b.status === filter)),
    [bets, filter],
  );

  const netPnl = paiseToRupees(kpis.netPnlPaise);

  return (
    <section className="flex w-full flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex items-center gap-4">
          <span className="grid size-14 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-emerald-400 to-teal-500 shadow-lg shadow-emerald-500/30">
            <span
              className="material-symbols-outlined text-[26px] leading-none text-white"
              aria-hidden="true"
            >
              receipt_long
            </span>
          </span>
          <div className="min-w-0">
            <h2 className="text-3xl font-black tracking-tighter text-slate-900 dark:text-slate-50 dark:text-slate-50">
              Trade Ledger
            </h2>
            <p className="mt-1 font-mono text-[10px] font-bold uppercase tracking-[0.18em] tabular-nums text-slate-400 dark:text-slate-500">
              {bets.length} wagers · {kpis.openCount} open · {kpis.settledCount} settled
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          <AnimatePresence>
            {droppedRows > 0 && (
              <motion.span
                initial={{ opacity: 0, y: -6, scale: 0.94 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, scale: 0.94 }}
                transition={SPRING}
                className="inline-flex items-center gap-2 rounded-full bg-rose-100 px-4 py-2 text-[11px] font-bold tracking-tight text-rose-700"
              >
                <span
                  className="material-symbols-outlined text-[15px] leading-none"
                  aria-hidden="true"
                >
                  error
                </span>
                <span className="font-mono tabular-nums">{droppedRows}</span> malformed rows
              </motion.span>
            )}
          </AnimatePresence>

          <AnimatePresence mode="wait">
            {!loading && source === 'mock' && (
              <motion.span
                key="offline"
                initial={{ opacity: 0, y: -8, scale: 0.9 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, scale: 0.9 }}
                transition={SPRING}
                className="inline-flex items-center gap-2 rounded-full bg-amber-100 px-4 py-2 text-[11px] font-bold tracking-tight text-amber-700 shadow-[0_8px_30px_rgb(245,158,11,0.2)]"
              >
                <motion.span
                  animate={{ scale: [1, 1.5, 1], opacity: [1, 0.55, 1] }}
                  transition={{ duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
                  className="size-2 rounded-full bg-amber-500"
                  aria-hidden="true"
                />
                Simulated ledger · API offline
              </motion.span>
            )}
            {!loading && source === 'api' && (
              <motion.span
                key="live"
                initial={{ opacity: 0, y: -8, scale: 0.9 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, scale: 0.9 }}
                transition={SPRING}
                className="inline-flex items-center gap-2 rounded-full bg-emerald-100 px-4 py-2 text-[11px] font-bold tracking-tight text-emerald-700 shadow-[0_8px_30px_rgb(16,185,129,0.2)]"
              >
                <motion.span
                  animate={{ scale: [1, 1.5, 1], opacity: [1, 0.55, 1] }}
                  transition={{ duration: 1.4, repeat: Infinity, ease: 'easeInOut' }}
                  className="size-2 rounded-full bg-emerald-500"
                  aria-hidden="true"
                />
                Live ledger
              </motion.span>
            )}
          </AnimatePresence>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          label="Gross Exposure"
          value={formatINR(paiseToRupees(kpis.grossExposurePaise))}
          caption={`${kpis.openCount} open positions`}
          icon="inventory_2"
          tone="warning"
        />
        <KpiCard
          label="Net P&L"
          value={formatSignedINR(netPnl)}
          caption={`ROI ${formatPct(kpis.roi)} on settled turnover`}
          icon={netPnl < 0 ? 'trending_down' : 'trending_up'}
          tone={netPnl > 0 ? 'positive' : netPnl < 0 ? 'negative' : 'neutral'}
        />
        <KpiCard
          label="Strike Rate"
          value={formatPct(kpis.strikeRate)}
          caption="Half outcomes weighted 0.5 · voids excluded"
          icon="target"
          tone="neutral"
        />
        <KpiCard
          label="Total Volume"
          value={formatINR(paiseToRupees(kpis.totalVolumePaise))}
          caption="Lifetime staked"
          icon="database"
          tone="neutral"
        />
      </div>

      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filter by status">
        {FILTERS.map(({ key, label }) => {
          const active = filter === key;
          return (
            <motion.button
              key={key}
              type="button"
              onClick={() => setFilter(key)}
              aria-pressed={active}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.96 }}
              transition={SPRING}
              className={[
                'rounded-xl px-4 py-2.5 text-[12px] font-bold tracking-tight',
                'outline-none focus-visible:ring-2 focus-visible:ring-emerald-400/60',
                active
                  ? 'bg-gradient-to-br from-emerald-400 to-teal-500 text-white shadow-[0_8px_30px_rgb(16,185,129,0.2)]'
                  : 'bg-white dark:bg-[#161514] text-slate-500 shadow-[0_2px_12px_rgb(0,0,0,0.04)] hover:text-slate-900 dark:text-slate-50 dark:text-[#A6A39E] dark:hover:text-[#E8E6E3] dark:hover:bg-white/[0.04]',
              ].join(' ')}
            >
              {label}
            </motion.button>
          );
        })}
      </div>

      <div className="overflow-hidden rounded-3xl bg-white dark:bg-[#161514] dark:border dark:border-white/[0.06] shadow-[0_4px_24px_rgb(0,0,0,0.04)]">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1080px] border-collapse">
            <caption className="sr-only">Bet history ledger</caption>
            <thead className="sticky top-0 z-10 bg-slate-50 dark:bg-white/[0.04]">
              <tr>
                <th scope="col" className={TH}>Date / Time</th>
                <th scope="col" className={TH}>Match / Event</th>
                <th scope="col" className={TH}>Market &amp; Selection</th>
                <th scope="col" className={TH}>Model</th>
                <th scope="col" className={`${TH} text-right`}>Stake</th>
                <th scope="col" className={`${TH} text-right`}>Odds</th>
                <th scope="col" className={`${TH} text-right`}>Return</th>
                <th scope="col" className={`${TH} text-right`}>Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading &&
                Array.from({ length: 6 }, (_, i) => (
                  <tr key={`skeleton-${i}`} className="animate-pulse">
                    <td className={TD} colSpan={8}>
                      <div className="h-5 w-full rounded-full bg-slate-100 dark:bg-white/[0.08]" />
                    </td>
                  </tr>
                ))}

              {!loading && rows.length === 0 && (
                <tr>
                  <td className="px-6 py-20 text-center" colSpan={8}>
                    <span className="mx-auto grid size-14 place-items-center rounded-2xl bg-slate-50 dark:bg-white/[0.04]">
                      <span
                        className="material-symbols-outlined text-[26px] leading-none text-slate-300 dark:text-slate-600 dark:text-[#A6A39E]"
                        aria-hidden="true"
                      >
                        filter_alt_off
                      </span>
                    </span>
                    <p className="mt-4 text-[13px] font-bold tracking-tight text-slate-400 dark:text-slate-500">
                      No wagers match this filter
                    </p>
                  </td>
                </tr>
              )}

              <AnimatePresence initial={false}>
                {!loading &&
                  rows.map((bet) => {
                    const { returnPaise, profitPaise } = settlementOf(bet);
                    const placedAt = new Date(bet.placed_at);
                    const validDate = !Number.isNaN(placedAt.getTime());
                    const isOpen = bet.status === 'pending';
                    const profit = paiseToRupees(profitPaise);
                    const returnTone = isOpen
                      ? 'text-slate-500'
                      : profit > 0
                        ? 'text-emerald-600'
                        : profit < 0
                          ? 'text-rose-600'
                          : 'text-slate-500';
                    const sport = SPORT_PRESENTATION[bet.sport];

                    return (
                      <motion.tr
                        key={bet.id}
                        layout
                        initial={{ opacity: 0, y: 8 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -8 }}
                        transition={SPRING}
                        className="transition-colors duration-200 hover:bg-slate-50 dark:bg-white/[0.04]"
                      >
                        <td className={TD}>
                          <p className="font-mono text-[13px] font-bold leading-tight tracking-tight tabular-nums text-slate-900 dark:text-slate-50">
                            {validDate ? DATE_FMT.format(placedAt) : '—'}
                            <span className="ml-2 font-medium text-slate-400 dark:text-slate-500">
                              {validDate ? TIME_FMT.format(placedAt) : '--:--'}
                            </span>
                          </p>
                          <p className="mt-1 font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-300 dark:text-slate-600 dark:text-[#A6A39E]">
                            {bet.id.slice(0, 14)}
                          </p>
                        </td>

                        <td className={TD}>
                          <div className="flex items-center gap-2.5">
                            <span
                              className={`rounded-lg px-2 py-1 font-mono text-[9px] font-bold tracking-[0.1em] ${sport.chip}`}
                            >
                              {sport.label}
                            </span>
                            <span className="truncate text-[13.5px] font-bold tracking-tight text-slate-900 dark:text-slate-50">
                              {bet.event_name}
                            </span>
                          </div>
                        </td>

                        <td className={TD}>
                          <p className="text-[13px] font-semibold leading-tight text-slate-700 dark:text-[#E8E6E3]">
                            {bet.selection}
                          </p>
                          <p className="mt-1 font-mono text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-400 dark:text-slate-500">
                            {bet.market}
                          </p>
                        </td>

                        <td className={TD}>
                          <p className="font-mono text-[11.5px] font-semibold text-slate-600 dark:text-[#A6A39E]">
                            {bet.model_name}
                          </p>
                          <p className="mt-1 inline-flex rounded-md bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] font-bold tabular-nums text-emerald-600">
                            +{bet.model_edge_pct.toFixed(1)}% edge
                          </p>
                        </td>

                        <td
                          className={`${TD} text-right font-mono text-[14px] font-bold tracking-tight tabular-nums text-slate-900 dark:text-slate-50`}
                        >
                          {formatINR(paiseToRupees(bet.stake_paise))}
                        </td>

                        <td className={`${TD} text-right`}>
                          <span className="inline-flex rounded-lg bg-slate-100 dark:bg-white/[0.08] px-2.5 py-1.5 font-mono text-[13px] font-bold tracking-tight tabular-nums text-slate-700 dark:text-[#E8E6E3]">
                            {formatOdds(bet.odds_decimal)}
                          </span>
                        </td>

                        <td className={`${TD} text-right`}>
                          <p
                            className={`font-mono text-[14px] font-bold tracking-tight tabular-nums ${returnTone}`}
                          >
                            {formatINR(paiseToRupees(returnPaise))}
                          </p>
                          <p className="mt-1 font-mono text-[10px] font-semibold uppercase tracking-[0.12em] tabular-nums text-slate-400 dark:text-slate-500">
                            {isOpen ? 'potential' : formatSignedINR(profit)}
                          </p>
                        </td>

                        <td className={`${TD} text-right`}>
                          <StatusBadge status={bet.status} />
                        </td>
                      </motion.tr>
                    );
                  })}
              </AnimatePresence>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};

export default BetHistory;