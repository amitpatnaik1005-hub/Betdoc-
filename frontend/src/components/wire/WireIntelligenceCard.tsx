/**
 * One row of VIDUR's intelligence board (Group 108, point b.14b), four columns:
 * 1. the item: sport, category and impact, the headline and the source's own first two sentences;
 * 2. the source: its name, tier and credibility, and how long ago it was published or measured;
 * 3. the match and market impact: only what was measured (the consensus since the story, the market following it,
 *    the lineup model, the friction factor, the steam move) and the fixture's best leg above fair now;
 * 4. the action: open the fixture in the Oracle, audit the slip through the neural chain, or run the chain.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { type ReactNode, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ago, type BoardItem, CATEGORY_LABEL, pts, runChain, TIER_LABEL } from "../../lib/neural_bus";
import { useNeuralBusStore } from "../../store/useNeuralBusStore";
import { useParlaySlipStore } from "../../store/useParlaySlipStore";
import { toast } from "../../store/useToastStore";
import { Button, Pill, type Tone } from "../../ui/kit";

const SPORT_TONE: Record<string, string> = {
  CRICKET: "bg-amber-100 text-amber-900 dark:bg-amber-950/60 dark:text-amber-300",
  FOOTBALL: "bg-sky-100 text-sky-900 dark:bg-sky-950/60 dark:text-sky-300",
  TENNIS: "bg-lime-100 text-lime-900 dark:bg-lime-950/60 dark:text-lime-300",
  MMA: "bg-rose-100 text-rose-900 dark:bg-rose-950/60 dark:text-rose-300",
};
const IMPACT_TONE: Record<string, Tone> = { CRITICAL: "critical", HIGH: "warning", MEDIUM: "info", LOW: "neutral" };
const pct = (p: number | null | undefined) => (p === null || p === undefined ? "—" : `${(100 * p).toFixed(1)}%`);
const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");
const tone = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? "text-stone-600 dark:text-stone-300" : v > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");

const Row = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="flex items-baseline justify-between gap-2 text-[11px]">
    <span className="shrink-0 text-stone-500 dark:text-stone-400">{label}</span>
    <span className="min-w-0 text-right font-mono text-stone-800 dark:text-stone-100">{children}</span>
  </div>
);

const Impact = ({ item }: { item: BoardItem }) => {
  const m = item.market;
  const f = item.fixture;
  if (!f) {
    return <p className="text-[11px] leading-snug text-stone-500 dark:text-stone-400">Names no fixture on the live board, so no market impact is claimed.</p>;
  }
  const side = (s: string) => (s === "HOME" ? f.home : s === "AWAY" ? f.away : s === "DRAW" ? "Draw" : s);
  const measured = m.move || m.catalyst || m.lineup || m.weather || m.steam;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate text-xs font-bold text-stone-900 dark:text-stone-100">{f.home} v {f.away}</span>
        <span className="shrink-0 text-[10px] text-stone-500 dark:text-stone-400">{f.in_play ? "in play" : `kicks off ${new Date(f.kickoff).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}`}</span>
      </div>
      {m.move && (
        <Row label="Market since the story">
          {side(m.move.selection)} {pct(m.move.before)} → {pct(m.move.now)} <span className={tone(m.move.delta_pts)}>({pts(m.move.delta_pts)})</span>
        </Row>
      )}
      {m.catalyst && (
        <Row label="Market followed it">
          {side(m.catalyst.selection)} <span className={tone(m.catalyst.delta_pts)}>{pts(m.catalyst.delta_pts)}</span> in {Math.round(m.catalyst.latency_seconds / 60)} min
        </Row>
      )}
      {m.lineup && (
        <Row label="Lineup model">
          {Object.entries(m.lineup.delta_pts).filter(([, v]) => v !== 0).map(([k, v]) => <span key={k} className={tone(v)}>{side(k)} {pts(v)} </span>)}
          {Object.values(m.lineup.delta_pts).every((v) => v === 0) && "no rated absence moves it"}
          {m.lineup.unrated > 0 && <span className="text-stone-500"> · {m.lineup.unrated} unrated</span>}
        </Row>
      )}
      {m.weather && (
        <Row label="Friction factor Π">
          {m.weather.friction_factor?.toFixed(2) ?? "—"}{m.weather.dew_expected ? " · dew expected" : ""}
        </Row>
      )}
      {m.steam && (
        <Row label={`Steam · ${m.steam.market}`}>
          {side(m.steam.selection)} {m.steam.opening_odds.toFixed(2)} → {m.steam.current_odds.toFixed(2)} <span className={tone(m.steam.delta_pts)}>({pts(m.steam.delta_pts)})</span>
        </Row>
      )}
      {!measured && item.kind === "NEWS" && (
        <p className="text-[11px] text-stone-500 dark:text-stone-400">No market move measured: the Wire records a baseline for CRITICAL and HIGH stories only.</p>
      )}
      <div className="mt-1 rounded-lg bg-white/70 px-2 py-1.5 dark:bg-white/[0.04]">
        {m.edge ? (
          <>
            <div className="flex items-baseline justify-between gap-2 text-[11px]">
              <span className="min-w-0 truncate font-semibold text-stone-800 dark:text-stone-100">{m.edge.label} @ {m.edge.price.toFixed(2)}</span>
              <span className="shrink-0 font-mono font-bold text-emerald-600 dark:text-emerald-400">EV {(100 * m.edge.ev).toFixed(1)}%</span>
            </div>
            <div className="text-[10px] text-stone-500 dark:text-stone-400">
              best of {m.legs_above_fair} leg{m.legs_above_fair === 1 ? "" : "s"} above fair · {m.edge.book_label} · fair {pct(m.edge.fair_probability)}
              {m.edge_shift_pts !== null && <span className={tone(m.edge_shift_pts)}> · {pts(m.edge_shift_pts)} since the last chain</span>}
            </div>
          </>
        ) : (
          <p className="text-[11px] text-stone-500 dark:text-stone-400">
            {m.legs_valued ? `No leg priced above fair now (${m.legs_valued} valued).` : "No leg freshly quoted at a retail book with a fair price."}
          </p>
        )}
      </div>
    </div>
  );
};

export const WireIntelligenceCard = ({ item }: { item: BoardItem }) => {
  const navigate = useNavigate();
  const legs = useParlaySlipStore((s) => s.legs);
  const skin = useParlaySlipStore((s) => s.skin);
  const recordChain = useNeuralBusStore((s) => s.recordChain);
  const setDrawerOpen = useNeuralBusStore((s) => s.setDrawerOpen);
  const [busy, setBusy] = useState<"chain" | "audit" | null>(null);
  const tier = TIER_LABEL[item.source.tier];
  const category = CATEGORY_LABEL[item.category];
  const onFixture = item.fixture ? legs.filter((l) => l.leg_id.split("|", 1)[0] === item.fixture?.fixture_id).length : 0;

  const run = async (audit: boolean) => {
    setBusy(audit ? "audit" : "chain");
    try {
      const chain = await runChain({ item_id: item.id, ...(audit ? { leg_ids: legs.map((l) => l.leg_id), skin } : {}) });
      recordChain(chain);
      setDrawerOpen(true);
      const consultant = chain.steps.find((s) => s.module === "CONSULTANT");
      toast.success(audit ? "Slip audited through the chain" : "Chain complete", audit && consultant ? consultant.summary : chain.outcome);
    } catch (err) {
      toast.error("The chain did not run", refusal(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className="grid grid-cols-1 gap-3 rounded-2xl bg-white p-4 shadow-soft transition-shadow duration-300 hover:shadow-soft-lg md:grid-cols-12 md:items-start dark:bg-stone-900 dark:shadow-none dark:ring-1 dark:ring-white/[0.06]">
      {/* 1. the item */}
      <div className="min-w-0 md:col-span-4">
        <div className="flex flex-wrap items-center gap-1.5">
          {item.sport && <span className={`rounded px-1.5 py-0.5 font-mono text-[9px] font-black uppercase ${SPORT_TONE[item.sport] ?? ""}`}>{item.sport}</span>}
          <Pill icon={category.icon}>{category.label}</Pill>
          {item.impact_level && <Pill tone={IMPACT_TONE[item.impact_level]}>{item.impact_level}</Pill>}
        </div>
        <h3 className="mt-1.5 text-sm font-bold leading-snug text-stone-900 dark:text-stone-100">{item.headline}</h3>
        {item.summary && <p className="mt-1 text-xs leading-relaxed text-stone-600 dark:text-stone-300">{item.summary}</p>}
      </div>

      {/* 2. source and freshness */}
      <div className="flex min-w-0 flex-col gap-1 text-xs md:col-span-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-stone-400 md:hidden">Source</span>
        {item.source.url ? (
          <a href={item.source.url} target="_blank" rel="noopener noreferrer" className="truncate font-semibold text-stone-800 hover:underline dark:text-stone-100">
            {item.source.name}
            <span className="material-symbols-outlined ml-0.5 align-[-2px] text-[12px] text-stone-400" aria-hidden="true">open_in_new</span>
          </a>
        ) : (
          <span className="truncate font-semibold text-stone-800 dark:text-stone-100">{item.source.name}</span>
        )}
        <span title={tier.hint}><Pill tone={tier.tone} icon={item.source.tier === "TIER_1_OFFICIAL" ? "verified" : undefined}>{tier.label}</Pill></span>
        {item.source.credibility !== null && <span className="text-[11px] text-stone-500 dark:text-stone-400">credibility {Math.round(100 * item.source.credibility)}%</span>}
        <span className="flex items-center gap-1 font-mono text-[11px] text-stone-500 dark:text-stone-400" title={new Date(item.published_at).toLocaleString()}>
          <span className="material-symbols-outlined text-[13px]" aria-hidden="true">schedule</span>{ago(item.published_at)}
        </span>
        <span className="text-[10px] text-stone-400">{item.source.basis}</span>
      </div>

      {/* 3. match and market impact */}
      <div className="min-w-0 rounded-xl bg-stone-50 p-3 md:col-span-4 dark:bg-white/[0.03]">
        <Impact item={item} />
      </div>

      {/* 4. the action */}
      <div className="flex flex-col gap-1.5 md:col-span-2">
        <Button size="sm" variant="primary" icon="bolt" disabled={!item.fixture} onClick={() => item.fixture && navigate(item.fixture.oracle_link)} className="w-full justify-center">
          Open in Oracle
        </Button>
        <Button size="sm" icon="fact_check" busy={busy === "audit"} disabled={!item.fixture || legs.length === 0 || busy !== null} onClick={() => void run(true)} className="w-full justify-center">
          Audit my slip
        </Button>
        <Button size="sm" variant="ghost" icon="account_tree" busy={busy === "chain"} disabled={busy !== null} onClick={() => void run(false)} className="w-full justify-center">
          Run the chain
        </Button>
        <span className="text-center text-[10px] text-stone-400">
          {legs.length === 0 ? "Your slip is empty" : onFixture ? `${onFixture} of your ${legs.length} legs on this match` : `None of your ${legs.length} legs on this match`}
        </span>
      </div>
    </li>
  );
};
