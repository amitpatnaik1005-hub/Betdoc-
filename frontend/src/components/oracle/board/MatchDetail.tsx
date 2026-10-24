/**
 * One fixture's page on the match board (Group 103, point b.11 screenshots 2-4): the match header, BetDoc's score
 * prediction and verdict from the market's own numbers, the category tabs, and every market the feeds price for it as
 * a collapsible accordion of clickable prices. Categories no feed prices say so; nothing is filled in.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { type MarketCard, missingParimatch, type SelectionCard } from "../../../lib/backtest_and_parlay";
import { formatDateTime } from "../../../lib/format";
import { type BoardFixture, CATEGORIES, type Category, driftFor, type MatchBoard, marketsIn, priceFor } from "../../../lib/match_board";
import type { Slip } from "../../../lib/oracle";
import { boardTargets } from "../../../lib/prediction";
import { ParimatchPriceEntry } from "../../parlay/ParimatchPriceEntry";
import { ScoreBackedLegs } from "../../prediction/ScoreBackedLegs";
import { FixtureContextChips, StoredPricesBadge } from "./FixtureContextChips";
import { OddsBox } from "./OddsBox";
import { TopParlays } from "./TopParlays";

interface Props {
  card: BoardFixture;
  book: string | null;
  inSlip: (legId: string) => boolean;
  drift: MatchBoard["drift"];
  onPick: (market: MarketCard, s: SelectionCard) => void;
  onBack: () => void;
  parlays: Slip[];
  onAddParlay: (slip: Slip) => void;
}

const pct = (p: number) => `${(p * 100).toFixed(1)}%`;

const Verdict = ({ card, book }: { card: BoardFixture; book: string | null }) => {
  const best = card.markets.flatMap((m) => m.selections.map((s) => ({ m, s }))).filter((x) => x.s.ev !== null).sort((a, b) => (b.s.ev ?? 0) - (a.s.ev ?? 0))[0];
  const likeliest = card.markets.find((m) => m.kind === "MATCH_ODDS")?.selections.slice().sort((a, b) => b.fair_probability - a.fair_probability)[0];
  return (
    <ul className="flex flex-col gap-1 text-xs">
      {card.projection && (
        <li>Projected score <strong style={{ color: "var(--wb-primary)" }}>{card.projection.score}</strong> ({pct(card.projection.probability)}, the single likeliest of every scoreline) · expected goals {card.projection.expected_goals[0].toFixed(2)} – {card.projection.expected_goals[1].toFixed(2)}</li>
      )}
      {likeliest && (
        <li>
          Likeliest result: <strong>{likeliest.label}</strong> at {pct(likeliest.fair_probability)}
          {card.priced_from === "stored" ? " (the de-vigged consensus when the stored prices were bought)" : " (the de-vigged consensus)"}
        </li>
      )}
      {best && best.s.ev !== null && (
        <li>
          {best.s.ev > 0 ? "Best value" : "No selection beats its fair price; closest"}: <strong>{best.s.label}</strong> ({best.m.market}) at {priceFor(best.s, book)?.toFixed(2) ?? "—"}
          {" "}· EV {best.s.ev >= 0 ? "+" : ""}{(best.s.ev * 100).toFixed(1)}% at the best price{best.s.best ? ` (${best.s.best.book})` : ""}
        </li>
      )}
    </ul>
  );
};

export const MatchDetail = ({ card, book, inSlip, drift, onPick, onBack, parlays, onAddParlay }: Props) => {
  const [category, setCategory] = useState<Category>("all");
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [scores, setScores] = useState(false);
  const football = boardTargets(card) !== null;
  const markets = marketsIn(card, category);
  const note = CATEGORIES.find((c) => c.id === category)?.note;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <button type="button" onClick={onBack} className="inline-flex items-center gap-1 text-xs font-semibold" style={{ color: "var(--wb-muted)" }}>
          <span className="material-symbols-outlined text-[18px]" aria-hidden="true">arrow_back</span>Match board
        </button>
        <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>{[card.country, card.league_name].filter(Boolean).join(" · ")}</span>
      </div>

      <header className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 rounded-2xl p-4 text-center" style={{ background: "var(--wb-card)" }}>
        <p className="truncate text-base font-bold">{card.home}</p>
        <div className="flex flex-col items-center">
          <span className="font-mono text-lg font-black" style={{ color: "var(--wb-primary)" }}>
            {card.context?.score ? `${card.context.score.home}–${card.context.score.away}` : card.in_play ? `${card.minutes_since_kickoff}′` : "VS"}
          </span>
          <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
            {card.context?.score ? `${card.context.status === "FINISHED" ? "full time" : `${card.minutes_since_kickoff ?? "—"}′ since kickoff`} · ${card.context.provider}`
              : card.in_play ? "since kickoff · no results feed pairs this match" : card.kickoff ? formatDateTime(card.kickoff) : "kickoff not stated"}
          </span>
          {(card.context || card.priced_from === "stored") && (
            <span className="mt-1 flex flex-wrap justify-center gap-1 text-[10px]">
              {card.priced_from === "stored" && <StoredPricesBadge asOf={card.prices_as_of} ageMinutes={card.prices_age_minutes} />}
              <FixtureContextChips context={card.context} />
            </span>
          )}
        </div>
        <p className="truncate text-base font-bold">{card.away}</p>
      </header>

      <section aria-label="BetDoc score prediction and verdict" className="flex flex-col gap-2 rounded-2xl p-3" style={{ border: "1px solid color-mix(in srgb, var(--wb-primary) 45%, transparent)" }}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-xs font-bold uppercase tracking-wider" style={{ color: "var(--wb-primary)" }}>BetDoc score prediction &amp; verdict</h3>
          {football && (
            <button type="button" aria-expanded={scores} onClick={() => setScores((v) => !v)} className="text-[11px] font-semibold underline">
              {scores ? "Hide the scoreline heatmap" : "Open the scoreline heatmap"}
            </button>
          )}
        </div>
        <Verdict card={card} book={book} />
        {!football && (
          <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
            The board fits scorelines to three-way football markets. Project {card.sport ?? "this sport"} from team ratings in <Link to="/lab" className="underline">The Lab</Link>.
          </p>
        )}
        {football && scores && (
          <ScoreBackedLegs card={card} book={book} onClose={() => setScores(false)} onAdd={(market, s) => {
            const m = card.markets.find((x) => x.market === market);
            if (m && !inSlip(s.leg_id)) onPick(m, s);
          }} />
        )}
      </section>

      <div role="tablist" aria-label="Market categories" className="flex gap-1 overflow-x-auto pb-1">
        {CATEGORIES.map((c) => {
          const count = c.id === "parlays" ? parlays.length : marketsIn(card, c.id).length;
          const active = category === c.id;
          return (
            <button key={c.id} type="button" role="tab" aria-selected={active} onClick={() => setCategory(c.id)}
                    className="shrink-0 rounded-lg px-3 py-1.5 text-xs font-semibold transition"
                    style={active ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)" } : { background: "var(--wb-card)", opacity: count ? 1 : 0.55 }}>
              {c.label} <span className="tabular-nums opacity-70">{count}</span>
            </button>
          );
        })}
      </div>
      {note && <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>{note}</p>}

      {category === "parlays" ? (
        parlays.length
          ? <TopParlays slips={parlays} book={book} onAdd={onAddParlay} inSlip={inSlip} title="Vetted parlays with a leg on this match" />
          : <p className="rounded-xl p-3 text-xs" style={{ background: "var(--wb-card)", color: "var(--wb-muted)" }}>No vetted parlay holds a leg on this match right now.</p>
      ) : markets.length === 0 ? (
        <p className="rounded-xl p-3 text-xs" style={{ background: "var(--wb-card)", color: "var(--wb-muted)" }}>
          No authorised feed prices {CATEGORIES.find((c) => c.id === category)?.label.toLowerCase()} for this match, so BetDoc shows none.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {markets.map((m) => {
            const open = !closed.has(m.market);
            const margin = book ? m.margins.by_book[book] : m.margins.best_price;
            return (
              <li key={m.market} className="rounded-2xl p-3" style={{ background: "color-mix(in srgb, var(--wb-card) 70%, transparent)" }}>
                <button type="button" aria-expanded={open} className="flex w-full items-center justify-between text-xs font-bold"
                        onClick={() => setClosed((prev) => { const next = new Set(prev); if (open) next.add(m.market); else next.delete(m.market); return next; })}>
                  <span>{m.market}</span>
                  <span className="flex items-center gap-2 font-normal" style={{ color: "var(--wb-muted)" }}>
                    margin {margin === undefined || margin === null ? "—" : pct(margin)}
                    <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{open ? "expand_less" : "expand_more"}</span>
                  </span>
                </button>
                {open && (
                  <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2 2xl:grid-cols-3">
                    {m.selections.map((s) => (
                      <OddsBox key={s.leg_id} wide selection={s} label={`${s.label} · ${pct(s.fair_probability)}`} book={book} selected={inSlip(s.leg_id)}
                               stored={card.priced_from === "stored"} drift={driftFor(drift, s, book)} onPick={() => onPick(m, s)} />
                    ))}
                  </div>
                )}
                {open && book === "parimatch" && card.priced_from !== "stored" && missingParimatch(m) && (
                  <div className="mt-2 flex flex-col"><ParimatchPriceEntry card={card} market={m} /></div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
};
