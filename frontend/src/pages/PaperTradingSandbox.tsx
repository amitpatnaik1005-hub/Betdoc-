/**
 * Paper Trading Sandbox Dashboard Page (Commander TODAR MAL, Group 80).
 * Real-time Mock Betting Forward-Testing Suite with ₹1,00,000 INR Virtual Capital.
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Every paper slip is executed by ASHVATTHAMA on the target book (Parimatch by default) at the live price it can get,
 * settled by the Group 79 sport adapters when the match's final score is recorded (or by hand here), and taxed under
 * Section 115BBJ. The scoreboard is the proof a strategy needs before real money: ROI, win rate on decided bets,
 * maximum drawdown, profit factor, the daily Sharpe ratio and a one-sided t-test on the per-bet returns.
 */
import { useMemo, useState } from "react";
import { TaxEstimationDrawer } from "../components/currency/TaxEstimationDrawer";
import { CurrencySelector } from "../components/currency/CurrencySelector";
import { useDisplayMoney } from "../components/currency/useDisplayMoney";
import {
  CURRENCY_LABELS,
  PAPER_VENUES,
  SUPPORTED_CURRENCIES,
  cashOutPaperBet,
  convertCurrency,
  liveCheck,
  refreshFxRates,
  settlePaperBet,
  statusTone,
  useFxBoard,
  usePaperBets,
  usePaperPerformance,
  useTaxLedger,
  type Bucket,
  type Conversion,
  type CurrencyCode,
  type LiveCheck,
  type PaperBet,
  type PaperBetStatus,
} from "../lib/paper_trading";
import { formatAgo, formatDateTime, formatOdds, formatPct, humanize } from "../lib/format";
import { ALL_SPORTS } from "../store/useSportStore";
import { useAuthStore } from "../store/useAuthStore";
import { explainPaperError, usePaperTradingStore } from "../store/usePaperTradingStore";
import { toast } from "../store/useToastStore";
import { LineChart } from "../ui/charts";
import { CommanderHero, MOTIFS } from "../ui/hero";
import {
  Async,
  Button,
  ConfirmButton,
  DataTable,
  EmptyState,
  Field,
  KeyValues,
  Meter,
  NumberInput,
  Page,
  Panel,
  Pill,
  Segmented,
  Select,
  Stat,
  StatGrid,
  TextInput,
  num,
  type Column,
} from "../ui/kit";
import { formatCurrency, formatINR, formatLakhCrore, formatSignedCurrency } from "../utils/formatCurrency";

const STATUS_FILTERS: readonly { value: PaperBetStatus | ""; label: string }[] = [
  { value: "", label: "All" },
  { value: "PENDING", label: "Open" },
  { value: "WON", label: "Won" },
  { value: "LOST", label: "Lost" },
  { value: "CASHED_OUT", label: "Cashed out" },
];

const SUMMARY_TONE: Record<string, "good" | "warning" | "info" | "critical"> = {
  "PROFITABLE FORWARD-TEST": "good",
  "PROFITABLE, NOT YET SIGNIFICANT": "info",
  "BUILDING STATISTICAL SAMPLE": "warning",
  "CALIBRATING ADVICE": "critical",
};

const MARKETS = [
  { value: "moneyline", label: "Match winner (moneyline)", selections: ["home", "draw", "away"], line: false },
  { value: "total", label: "Total over / under", selections: ["over", "under"], line: true },
  { value: "asian_handicap", label: "Asian handicap", selections: ["home", "away"], line: true },
  { value: "btts", label: "Both teams to score", selections: ["yes", "no"], line: false },
  { value: "double_chance", label: "Double chance", selections: ["1x", "12", "x2"], line: false },
  { value: "draw_no_bet", label: "Draw no bet", selections: ["home", "away"], line: false },
] as const;

const fmt2 = (n: number | null | undefined): string => (n === null || n === undefined ? "—" : n.toFixed(2));

// ================================================================ manual placement
const ManualTicket = () => {
  const placeMockBet = usePaperTradingStore((s) => s.placeMockBet);
  const isPaperMode = usePaperTradingStore((s) => s.isPaperMode);
  const [sport, setSport] = useState("soccer");
  const [matchId, setMatchId] = useState("");
  const [eventName, setEventName] = useState("");
  const [market, setMarket] = useState<(typeof MARKETS)[number]["value"]>("moneyline");
  const [selection, setSelection] = useState("home");
  const [line, setLine] = useState("2.5");
  const [odds, setOdds] = useState("2.00");
  const [stake, setStake] = useState("1000");
  const [fair, setFair] = useState("");
  const [venue, setVenue] = useState<string>("parimatch");
  const [busy, setBusy] = useState(false);
  const spec = MARKETS.find((m) => m.value === market) ?? MARKETS[0];
  const valid = matchId.trim() !== "" && num(odds) > 1 && num(stake) > 0 && (!spec.line || Number.isFinite(num(line)));

  const submit = async () => {
    setBusy(true);
    const fairP = num(fair);
    await placeMockBet({
      sport_key: sport,
      match_id: matchId.trim(),
      event_name: eventName.trim() || matchId.trim(),
      market_type: market,
      selection,
      odds: num(odds),
      line: spec.line ? num(line) : null,
      stake_amount: num(stake),
      target_bookmaker: venue,
      fair_probability: fairP > 0 && fairP < 1 ? fairP : null,
      source: "MANUAL",
    });
    setBusy(false);
  };

  return (
    <Panel title="Place a paper bet" icon="add_card" className="lg:col-span-12" subtitle="executed by ASHVATTHAMA at the book's live price">
      {!isPaperMode && (
        <p className="mb-4 rounded-2xl bg-rose-50 px-3 py-2 text-[12px] text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
          The desk is in LIVE mode. Paper bets still go to the sandbox only; switch back to PAPER in the header to forward-test from the Oracle.
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Field label="Sport">
          <Select value={sport} onChange={(e) => setSport(e.target.value)}>
            {ALL_SPORTS.map((s) => <option key={s.key} value={s.key}>{s.name}</option>)}
          </Select>
        </Field>
        <Field label="Target book">
          <Select value={venue} onChange={(e) => setVenue(e.target.value)}>
            {PAPER_VENUES.map((v) => <option key={v} value={v}>{humanize(v)}</option>)}
          </Select>
        </Field>
        <Field label="Match id" hint="the fixture id the final score is recorded under"><TextInput value={matchId} onChange={(e) => setMatchId(e.target.value)} placeholder="e.g. 3f9c…" /></Field>
        <Field label="Event"><TextInput value={eventName} onChange={(e) => setEventName(e.target.value)} placeholder="Arsenal vs Chelsea" /></Field>
        <Field label="Market" className="col-span-2 md:col-span-1">
          <Select value={market} onChange={(e) => { const next = MARKETS.find((m) => m.value === e.target.value) ?? MARKETS[0]; setMarket(next.value); setSelection(next.selections[0]); }}>
            {MARKETS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </Select>
        </Field>
        <Field label="Selection">
          <Select value={selection} onChange={(e) => setSelection(e.target.value)}>
            {spec.selections.map((s) => <option key={s} value={s}>{s.toUpperCase()}</option>)}
          </Select>
        </Field>
        {spec.line ? (
          <Field label="Line" hint={market === "asian_handicap" ? "the selected side's handicap" : undefined}><NumberInput step="0.25" value={line} onChange={(e) => setLine(e.target.value)} /></Field>
        ) : <div />}
        <Field label="Requested odds"><NumberInput step="0.01" min="1.01" value={odds} onChange={(e) => setOdds(e.target.value)} /></Field>
        <Field label="Stake ₹"><NumberInput min="1" value={stake} onChange={(e) => setStake(e.target.value)} /></Field>
        <Field label="Model probability" hint="optional: FA-4 Kelly, FA-2 discipline">
          <NumberInput step="0.01" min="0.01" max="0.99" value={fair} onChange={(e) => setFair(e.target.value)} placeholder="0.55" />
        </Field>
      </div>
      <Button variant="primary" icon="experiment" className="mt-4 w-full md:w-auto" busy={busy} disabled={!valid} onClick={() => void submit()}>
        Execute paper bet
      </Button>
    </Panel>
  );
};

// ================================================================ the selected bet's actions
const BetActions = ({ bet, onClose }: { bet: PaperBet; onClose: () => void }) => {
  const [home, setHome] = useState("");
  const [away, setAway] = useState("");
  const [cash, setCash] = useState(() => String(Math.round(bet.stake_amount)));
  const [minute, setMinute] = useState("60");
  const [busy, setBusy] = useState<string | null>(null);
  const [check, setCheck] = useState<LiveCheck | null>(null);
  const single = bet.structure === "SINGLE";

  const act = async (what: string, work: () => Promise<unknown>, ok: string) => {
    setBusy(what);
    try {
      await work();
      toast.success(ok);
      if (what !== "check") onClose();
    } catch (err) {
      toast.error("Refused", explainPaperError(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-5 rounded-3xl bg-stone-50 p-4 dark:bg-white/[0.03]">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-stone-900 dark:text-stone-100">{bet.event_name}</p>
          <p className="text-xs text-stone-500 dark:text-stone-400">
            {single ? `${humanize(bet.market_type)} · ${bet.selection.toUpperCase()}${bet.line !== null ? ` ${bet.line}` : ""}` : `${bet.legs.length}-leg parlay`} · {formatINR(bet.stake_amount)} @ {formatOdds(bet.executed_odds)}
          </p>
        </div>
        <Button size="sm" variant="ghost" icon="close" aria-label="Close" onClick={onClose} />
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold text-stone-600 dark:text-stone-300">Settle from the final score</p>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Home"><NumberInput min="0" value={home} onChange={(e) => setHome(e.target.value)} /></Field>
            <Field label="Away"><NumberInput min="0" value={away} onChange={(e) => setAway(e.target.value)} /></Field>
          </div>
          <Button size="sm" icon="gavel" busy={busy === "settle"} disabled={!single || !(num(home) >= 0) || !(num(away) >= 0)}
            title={single ? undefined : "Parlays settle leg by leg from the recorded scores"}
            onClick={() => void act("settle", () => settlePaperBet(bet.id, { home_score: num(home), away_score: num(away), status: "final" }, "Settled by hand"), "Paper bet settled")}>
            Settle
          </Button>
        </div>
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold text-stone-600 dark:text-stone-300">FA-1 live check</p>
          <div className="grid grid-cols-3 gap-2">
            <Field label="Min"><NumberInput min="0" value={minute} onChange={(e) => setMinute(e.target.value)} /></Field>
            <Field label="H"><NumberInput min="0" value={home} onChange={(e) => setHome(e.target.value)} /></Field>
            <Field label="A"><NumberInput min="0" value={away} onChange={(e) => setAway(e.target.value)} /></Field>
          </div>
          <Button size="sm" icon="monitor_heart" busy={busy === "check"} disabled={!single || bet.sport_key !== "soccer"}
            title={bet.sport_key === "soccer" ? undefined : "Other sports' live states are scored from the Sports Engine's in-play tick"}
            onClick={() => void act("check", async () => setCheck(await liveCheck(bet.id, { live_state: { minute: num(minute), home_goals: num(home) || 0, away_goals: num(away) || 0 } })), "Live check done")}>
            Check
          </Button>
          {check && (
            <div className="flex flex-col gap-1.5">
              <Pill tone={check.alert ? "critical" : "good"} icon={check.alert ? "warning" : "shield"}>{check.alert ? "Cash-out alert" : "Hold"}</Pill>
              <Meter value={check.live_probability} tone={check.alert ? "critical" : "good"} label={`${check.side} live ${formatPct(check.live_probability * 100)}`} />
              <p className="text-[11px] text-stone-500 dark:text-stone-400">{check.message} · fair value {formatINR(Number(check.shield.fair_value_inr))}</p>
            </div>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <p className="text-xs font-semibold text-stone-600 dark:text-stone-300">Virtual cash-out</p>
          <Field label="Offer ₹" hint={`up to ${formatINR(bet.potential_payout)}`}><NumberInput min="0" value={cash} onChange={(e) => setCash(e.target.value)} /></Field>
          <ConfirmButton size="sm" icon="payments" busy={busy === "cash"} disabled={!(num(cash) >= 0) || num(cash) > bet.potential_payout} confirmLabel="Confirm cash-out"
            onConfirm={() => void act("cash", () => cashOutPaperBet(bet.id, num(cash)), "Cashed out")}>
            Cash out
          </ConfirmButton>
        </div>
      </div>
    </div>
  );
};

// ================================================================ breakdowns
const BucketTable = ({ rows, label }: { rows: Record<string, Bucket>; label: string }) => {
  const entries = Object.entries(rows).sort((a, b) => b[1].bets - a[1].bets);
  if (entries.length === 0) return <EmptyState icon="leaderboard" title={`No settled bets by ${label} yet`} />;
  const columns: Column<[string, Bucket]>[] = [
    { key: "k", header: humanize(label), render: ([k]) => <span className="font-medium">{humanize(k)}</span> },
    { key: "n", header: "Bets", align: "right", render: ([, b]) => b.bets },
    { key: "w", header: "Win rate", align: "right", render: ([, b]) => formatPct(b.win_rate_pct) },
    { key: "y", header: "Yield", align: "right", render: ([, b]) => <span className={b.yield_pct >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-rose-600 dark:text-rose-300"}>{formatPct(b.yield_pct, 2)}</span> },
    { key: "p", header: "P&L", align: "right", render: ([, b]) => <span className="font-mono">{formatSignedCurrency(b.pnl)}</span> },
  ];
  return <DataTable dense columns={columns} rows={entries} rowKey={([k]) => k} />;
};

const FxDesk = () => {
  const board = useFxBoard();
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const [amount, setAmount] = useState("100000");
  const [from, setFrom] = useState<CurrencyCode>("INR");
  const [to, setTo] = useState<CurrencyCode>("USD");
  const [result, setResult] = useState<Conversion | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const convert = async () => {
    setBusy("convert");
    try {
      setResult(await convertCurrency(num(amount), from, to));
    } catch (err) {
      toast.error("No conversion", explainPaperError(err));
    } finally {
      setBusy(null);
    }
  };
  const refresh = async () => {
    setBusy("refresh");
    try {
      const fresh = await refreshFxRates();
      toast.success("Rates refreshed", `${Object.keys(fresh.rates).length} currencies priced${Object.keys(fresh.missing).length ? `, ${Object.keys(fresh.missing).join(", ")} unavailable` : ""}.`);
    } catch (err) {
      toast.error("Refresh failed", explainPaperError(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel title="FX board" icon="currency_exchange" className="lg:col-span-5" subtitle="TODAR MAL · INR per unit" updatedAt={board.updatedAt}
      actions={isAdmin ? <Button size="sm" variant="ghost" icon="sync" busy={busy === "refresh"} onClick={() => void refresh()}>Refresh</Button> : undefined}>
      <Async resource={board}>
        {(b) => (
          <div className="flex flex-col gap-4">
            <ul className="grid grid-cols-2 gap-2 xl:grid-cols-3">
              {SUPPORTED_CURRENCIES.map((code) => {
                const r = b.rates[code];
                return (
                  <li key={code} className="rounded-2xl bg-stone-50 px-3 py-2 dark:bg-white/[0.03]" title={r ? `${r.provider} · ${formatDateTime(r.as_of)}` : b.missing[code]}>
                    <p className="text-[11px] font-medium text-stone-500 dark:text-stone-400">{CURRENCY_LABELS[code].symbol} {code}</p>
                    <p className="whitespace-nowrap font-mono text-sm text-stone-900 dark:text-stone-100">{r ? formatINR(r.inr_per_unit) : <span className="text-stone-400">no rate</span>}</p>
                    {r && code !== "INR" && <p className="text-[10px] text-stone-400 dark:text-stone-500">{formatAgo(r.as_of)}</p>}
                  </li>
                );
              })}
            </ul>
            <Field label="Amount"><NumberInput min="0" value={amount} onChange={(e) => setAmount(e.target.value)} /></Field>
            <div className="flex flex-wrap items-center gap-2">
              <CurrencySelector value={from} onSelect={setFrom} label="From currency" />
              <span className="material-symbols-outlined text-[16px] text-stone-400" aria-hidden>arrow_forward</span>
              <CurrencySelector value={to} onSelect={setTo} label="To currency" />
            </div>
            <Button size="sm" icon="swap_horiz" busy={busy === "convert"} disabled={!(num(amount) >= 0)} onClick={() => void convert()}>Convert</Button>
            {result && (
              <p className="text-sm text-stone-700 dark:text-stone-200">
                {formatCurrency(result.amount, result.from_currency)} = <span className="font-semibold">{formatCurrency(result.converted, result.to_currency)}</span>
                <span className="text-xs text-stone-400"> · {result.provider}</span>
              </p>
            )}
          </div>
        )}
      </Async>
    </Panel>
  );
};

// ================================================================ the page
export const PaperTradingSandbox = () => {
  const credit = usePaperTradingStore((s) => s.developerCredit);
  const resetAccount = usePaperTradingStore((s) => s.resetAccount);
  const isPaperMode = usePaperTradingStore((s) => s.isPaperMode);
  const perf = usePaperPerformance();
  const [filter, setFilter] = useState<PaperBetStatus | "">("");
  const bets = usePaperBets(filter);
  const ledger = useTaxLedger();
  const money = useDisplayMoney();
  const [selected, setSelected] = useState<PaperBet | null>(null);
  const [tax, setTax] = useState<{ gross: number; stake: number; title: string } | null>(null);
  const [resetting, setResetting] = useState(false);
  const [breakdown, setBreakdown] = useState<"sport" | "strategy">("sport");

  const p = perf.data;
  const curve = useMemo(() => (p?.equity_curve ?? []).map((pt) => ({ label: formatDateTime(pt.at), value: pt.equity })), [p?.equity_curve]);

  const reset = async () => {
    setResetting(true);
    await resetAccount();
    setSelected(null);
    setResetting(false);
  };

  const columns: Column<PaperBet>[] = [
    { key: "event", header: "Event", render: (b) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate font-medium">{b.event_name}</span>
        <span className="text-[11px] text-stone-400 dark:text-stone-500">{humanize(b.sport_key)} · {b.structure === "PARLAY" ? `${b.legs.length} legs` : `${humanize(b.market_type)} ${b.selection.toUpperCase()}${b.line !== null ? ` ${b.line}` : ""}`}</span>
      </span>
    ) },
    { key: "book", header: "Book", render: (b) => <span className="text-xs">{humanize(b.target_bookmaker)}{b.source === "ASHOKA" ? " · Ashoka" : ""}</span> },
    { key: "odds", header: "Odds", align: "right", render: (b) => (
      <span className="whitespace-nowrap font-mono text-xs" title={b.execution ? `${b.execution.price_source} · slippage ${fmt2(b.execution.slippage_pct)}% · ${Math.round(b.execution.total_latency_ms)} ms` : undefined}>
        {formatOdds(b.executed_odds)}{b.executed_odds !== b.odds && <span className="text-stone-400"> / {formatOdds(b.odds)}</span>}
      </span>
    ) },
    { key: "stake", header: "Stake", align: "right", render: (b) => <span className="whitespace-nowrap font-mono text-xs">{money.format(b.stake_amount)}</span> },
    { key: "status", header: "Status", render: (b) => <Pill tone={statusTone(b.status)}>{humanize(b.status)}</Pill> },
    { key: "pnl", header: "P&L", align: "right", render: (b) => (
      <span className={`whitespace-nowrap font-mono text-xs ${b.pnl > 0 ? "text-emerald-700 dark:text-emerald-300" : b.pnl < 0 ? "text-rose-600 dark:text-rose-300" : ""}`}>
        {b.status === "PENDING" ? "—" : `${b.pnl > 0 ? "+" : ""}${money.format(b.pnl)}`}
      </span>
    ) },
    { key: "act", header: "", align: "right", render: (b) => (
      <span className="inline-flex gap-1.5 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
        {b.status === "PENDING" && <Button size="sm" variant="ghost" icon="tune" onClick={() => setSelected(b)}>Manage</Button>}
        {b.pnl > 0 && (
          <Button size="sm" variant="ghost" icon="receipt_long" title={`TDS ${formatINR(b.tds_amount)}`} onClick={() => setTax({ gross: b.payout ?? b.potential_payout, stake: b.stake_amount, title: `30% TDS · ${b.event_name}` })}>
            30% TDS
          </Button>
        )}
      </span>
    ) },
  ];

  const summaryTone = p ? SUMMARY_TONE[p.status_summary] ?? "info" : "info";

  return (
    <Page>
      <CommanderHero
        commander="TODAR MAL"
        headline={p
          ? `TODAR MAL'S SANDBOX. ${formatLakhCrore(p.virtual_balance)} virtual, ${p.total_bets} paper bet${p.total_bets === 1 ? "" : "s"}, ROI ${formatPct(p.roi_pct, 2)}: ${p.status_summary.toLowerCase()}.`
          : "TODAR MAL'S SANDBOX. ₹1,00,000 of virtual capital, forward-tested at Parimatch's live prices."}
        motif={MOTIFS.vault}
        detail={`Forward-test Ashoka's value bets with dummy money before risking real capital: ASHVATTHAMA executes each slip at the book's live price, the Group 79 adapters settle it from the real result, and every winning bet books its Section 115BBJ TDS. Mode: ${isPaperMode ? "PAPER" : "LIVE"}. Developer Credit: ${credit}.`}
        actions={
          <>
            <Button icon="refresh" busy={perf.loading && perf.data !== undefined} onClick={() => { void perf.refresh(); void bets.refresh(); void ledger.refresh(); }}>Refresh</Button>
            <ConfirmButton icon="restart_alt" busy={resetting} confirmLabel="Reset to ₹1,00,000?" onConfirm={() => void reset()}>Reset to ₹1,00,000</ConfirmButton>
          </>
        }
        stats={p && (
          <div className="flex flex-wrap gap-2">
            <Pill tone={summaryTone} icon="query_stats">{p.status_summary}</Pill>
            <Pill tone={p.live_ready ? "good" : "warning"} icon={p.live_ready ? "verified" : "lock"}>{p.live_ready ? "Live-ready: edge proven" : "KUMBHA firewall: not yet proven"}</Pill>
            {money.converted && <Pill tone="info" icon="currency_exchange">Shown in {money.currency}</Pill>}
          </div>
        )}
      />

      <Panel title="Forward-test scoreboard" icon="scoreboard" className="lg:col-span-12" subtitle={p ? `epoch ${p.epoch} · ${p.significance.verdict}` : undefined} updatedAt={perf.updatedAt}>
        <Async resource={perf} skeletonRows={2}>
          {(s) => (
            <div className="flex flex-col gap-8">
              <StatGrid cols={4}>
                <Stat label="Virtual balance" icon="account_balance_wallet" value={money.format(s.virtual_balance, true)} hint={`equity ${money.format(s.equity, true)}`} />
                <Stat label="Net virtual P&L" icon="trending_up" tone={s.total_pnl > 0 ? "positive" : s.total_pnl < 0 ? "negative" : "neutral"}
                  value={`${s.total_pnl > 0 ? "+" : ""}${money.format(s.total_pnl, true)}`} hint={`after TDS ${money.format(s.net_pnl_after_tds, true)}`} />
                <Stat label="ROI" icon="percent" tone={s.roi_pct >= 0 ? "positive" : "negative"} value={formatPct(s.roi_pct, 2)} hint={`yield ${formatPct(s.yield_pct, 1)}`} />
                <Stat label="Win rate (decided)" icon="emoji_events" value={formatPct(s.win_rate_pct)} hint={`${s.won_bets}W / ${s.lost_bets}L · ${s.push_bets} push`} />
                <Stat label="Max drawdown" icon="south_east" tone={s.max_drawdown_pct > 20 ? "negative" : s.max_drawdown_pct > 10 ? "caution" : "neutral"} value={formatPct(s.max_drawdown_pct, 2)} hint="on equity" />
                <Stat label="Sharpe ratio" icon="ssid_chart" value={fmt2(s.sharpe_ratio)} hint="rf 6.5% · ×√365" />
                <Stat label="Profit factor" icon="balance" value={fmt2(s.profit_factor)} hint={s.brier_score !== null ? `Brier ${s.brier_score.toFixed(3)}` : "wins / losses"} />
                <Stat label="Paper bets" icon="receipt_long" value={s.total_bets} hint={`${s.pending_bets} open · ${money.format(s.open_exposure, true)}`} />
              </StatGrid>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Meter value={Math.min(1, s.significance.sample / 30)} tone={s.significance.proven ? "good" : "warning"} label={`Sample ${s.significance.sample} / 30 decided bets`} />
                <KeyValues items={[
                  { label: "Mean return per bet", value: formatPct(s.significance.mean_return * 100, 2) },
                  { label: "t-statistic / p-value", value: `${fmt2(s.significance.t_stat)} / ${s.significance.p_value === null ? "—" : s.significance.p_value.toFixed(4)}` },
                ]} />
              </div>
            </div>
          )}
        </Async>
      </Panel>

      <Panel title="Equity curve" icon="show_chart" className="lg:col-span-7" subtitle="balance + open stakes after every movement">
        {curve.length < 2 ? (
          <EmptyState icon="show_chart" title="The curve starts with your first settled bet" detail="Place a paper bet from the Oracle or the ticket form." />
        ) : (
          <LineChart caption="Paper equity over time" labels={curve.map((c) => c.label)} series={[{ name: "Equity", color: "var(--viz-series-1)", values: curve.map((c) => c.value) }]} formatValue={(v) => formatLakhCrore(v)} />
        )}
      </Panel>

      <FxDesk />

      <ManualTicket />

      <Panel title="Paper bets" icon="list_alt" className="lg:col-span-12" updatedAt={bets.updatedAt}
        actions={<Segmented size="sm" label="Status" value={filter} onChange={(v) => { setFilter(v); setSelected(null); }} options={STATUS_FILTERS} />}>
        <Async resource={bets} isEmpty={(r) => r.length === 0}
          empty={<EmptyState icon="experiment" title="No paper bets yet" detail='Open the Oracle (Ashoka) and press "Mock bet" on a slip, or use the ticket form above.' />}>
          {(rows) => <DataTable columns={columns} rows={rows} rowKey={(b) => b.id} onRowClick={(b) => b.status === "PENDING" && setSelected(b)} rowTitle="Manage this paper bet" />}
        </Async>
        {selected && selected.status === "PENDING" && <BetActions key={selected.id} bet={selected} onClose={() => setSelected(null)} />}
      </Panel>

      <Panel title="Breakdown" icon="leaderboard" className="lg:col-span-12"
        actions={<Segmented size="sm" label="Breakdown" value={breakdown} onChange={setBreakdown} options={[{ value: "sport", label: "By sport" }, { value: "strategy", label: "By strategy" }]} />}>
        <Async resource={perf}>{(s) => <BucketTable rows={breakdown === "sport" ? s.by_sport : s.by_strategy} label={breakdown} />}</Async>
      </Panel>

      <Panel title="Section 115BBJ ledger" icon="account_balance" className="lg:col-span-12" subtitle="30% TDS on each winning bet's net winnings, by Indian financial year" updatedAt={ledger.updatedAt}>
        <Async resource={ledger} isEmpty={(l) => l.entries.length === 0} empty={<EmptyState icon="account_balance" title="No taxable winnings yet" detail="Every winning paper bet books its TDS here." />}>
          {(l) => (
            <div className="flex flex-col gap-6">
              <div className="flex flex-wrap gap-3">
                {Object.entries(l.by_financial_year).map(([fy, t]) => (
                  <div key={fy} className="rounded-2xl bg-stone-50 px-4 py-3 dark:bg-white/[0.03]">
                    <p className="text-[11px] font-semibold text-stone-500 dark:text-stone-400">FY {fy} · {t.entries} entr{t.entries === 1 ? "y" : "ies"}</p>
                    <p className="font-mono text-sm text-stone-900 dark:text-stone-100">TDS {formatINR(t.tds)}</p>
                    <p className="text-[11px] text-stone-400 dark:text-stone-500">on {formatINR(t.net_winnings)} net winnings</p>
                  </div>
                ))}
              </div>
              <DataTable dense rows={l.entries.slice(0, 50)} rowKey={(e) => e.id} columns={[
                { key: "at", header: "Booked", render: (e) => <span className="text-xs">{formatDateTime(e.created_at)}</span> },
                { key: "mode", header: "Mode", render: (e) => <Pill tone={e.mode === "PAPER" ? "info" : "accent"}>{e.mode}</Pill> },
                { key: "gross", header: "Payout", align: "right", render: (e) => <span className="font-mono text-xs">{formatINR(e.gross_payout)}</span> },
                { key: "net", header: "Net winnings", align: "right", render: (e) => <span className="font-mono text-xs">{formatINR(e.net_winnings)}</span> },
                { key: "tds", header: "TDS", align: "right", render: (e) => <span className="font-mono text-xs text-rose-600 dark:text-rose-300">{formatINR(e.tds_amount)}</span> },
                { key: "home", header: "Take-home", align: "right", render: (e) => <span className="font-mono text-xs">{formatINR(e.net_post_tax_payout)}</span> },
              ]} />
            </div>
          )}
        </Async>
      </Panel>

      <TaxEstimationDrawer isOpen={tax !== null} grossWin={tax?.gross ?? 0} stake={tax?.stake ?? 0} title={tax?.title} onClose={() => setTax(null)} />
    </Page>
  );
};
