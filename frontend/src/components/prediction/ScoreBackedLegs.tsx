/**
 * Score-backed legs on the parlay workbench (Group 102). Developed for Amit Ashok Kumar Patnaik.
 *
 * The fixture's own board is the input: its de-vigged Match Odds (and any x.5 goal lines) fit Dixon-Coles rates, the
 * scoreline heatmap follows, and every leg the board offers for the fixture is priced off the same matrix, so totals,
 * handicaps and BTTS agree with the 1X2 by construction. Choose a scoreline: the legs that win (or half win) at it are
 * marked and sorted first, each one click from the slip. Nothing is added that the board does not quote.
 */
import { useState } from "react";
import { formatRatioPct } from "../../lib/format";
import type { FixtureCard, SelectionCard } from "../../lib/backtest_and_parlay";
import { boardTargets, type MarketInput, type PricedSelection, predictFootballFromMarket, settleAt } from "../../lib/prediction";
import { useResource } from "../../lib/resource";
import { Async, Button, EmptyState } from "../../ui/kit";
import { FootballScoreHeatmap } from "./FootballScoreHeatmap";

/** EV of one unit at `odds` under the leg's settlement distribution (half results and pushes counted). */
const evAt = (p: PricedSelection, odds: number): number => p.win * odds + p.half_win * (odds + 1) / 2 + p.push + p.half_loss / 2 - 1;

interface Row {
  market: string;
  selection: SelectionCard;
  model: PricedSelection;
  price: number | null;
}

export const ScoreBackedLegs = ({ card, book, onAdd, onClose }: { card: FixtureCard; book: string | null; onAdd: (market: string, s: SelectionCard) => void; onClose: () => void }) => {
  const targets = boardTargets(card);
  const prediction = useResource(targets ? `prediction:board:${card.fixture_id}:${JSON.stringify(targets)}` : null, () => predictFootballFromMarket(targets as MarketInput));
  const [picked, setPicked] = useState<{ score: string; home: number; away: number } | null>(null);

  if (!targets) {
    return <EmptyState icon="scoreboard" title="No complete Match Odds on the board" detail="Scorelines are fitted to the fixture's de-vigged 1X2; this card does not quote all three sides yet." action={<Button size="sm" onClick={onClose}>Close</Button>} />;
  }
  return (
    <Async resource={prediction} skeletonRows={4}>
      {(pr) => {
        const priced = new Map(pr.markets.map((m) => [`${m.market}|${m.selection}`, m]));
        const rows: Row[] = card.markets.flatMap((m) =>
          m.selections.flatMap((s) => {
            const model = priced.get(`${m.market}|${s.selection}`);
            const price = book ? s.prices[book] ?? null : s.best?.odds ?? null;
            return model ? [{ market: m.market, selection: s, model, price }] : [];
          }),
        );
        const backs = (r: Row) => (picked ? settleAt(r.model, picked.home, picked.away) : null);
        rows.sort((a, b) => {
          const wa = backs(a) === "WON" || backs(a) === "HALF_WON" ? 1 : 0;
          const wb = backs(b) === "WON" || backs(b) === "HALF_WON" ? 1 : 0;
          return wb - wa || b.model.win_probability - a.model.win_probability;
        });
        return (
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className="text-sm font-semibold">{card.home} v {card.away} · score prediction</p>
                <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
                  BetDoc scoreline model fitted to this board's de-vigged prices · expected goals {pr.expected_goals[0].toFixed(2)} – {pr.expected_goals[1].toFixed(2)}
                </p>
              </div>
              <Button size="sm" variant="ghost" icon="close" onClick={onClose}>Close</Button>
            </div>
            <FootballScoreHeatmap heatmap={pr.heatmap} top={pr.top_scorelines} mass={pr.heatmap_mass} homeLabel={card.home} awayLabel={card.away}
                                  selected={picked?.score ?? null} onSelect={(score, home, away) => setPicked((cur) => (cur?.score === score ? null : { score, home, away }))} />
            <ul className="flex flex-col gap-1.5" aria-label="Board legs priced off the scorelines">
              {rows.map((r) => {
                const result = backs(r);
                const backed = result === "WON" || result === "HALF_WON";
                const ev = r.price !== null ? evAt(r.model, r.price) : null;
                return (
                  <li key={r.selection.leg_id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl px-3 py-2 text-xs"
                      style={{ background: "var(--wb-card)", outline: backed ? "2px solid var(--wb-primary)" : undefined }}>
                    <span className="min-w-0">
                      <span className="font-semibold">{r.selection.label}</span>
                      <span style={{ color: "var(--wb-muted)" }}> · {r.market}</span>
                      {picked && result && <span className="ml-1 font-bold" style={{ color: backed ? "var(--wb-primary)" : "var(--wb-muted)" }}>· {result.replace("_", " ").toLowerCase()} at {picked.score}</span>}
                    </span>
                    <span className="flex items-center gap-3 tabular-nums">
                      <span title="model chance of a win (half wins count)">{formatRatioPct(r.model.win_probability)}</span>
                      <span title="model fair price" style={{ color: "var(--wb-muted)" }}>fair {r.model.fair_odds?.toFixed(2) ?? "—"}</span>
                      <span className="font-mono font-bold" style={{ color: "var(--wb-primary)" }}>{r.price ?? "—"}</span>
                      <span title="expected value at that price" style={{ color: ev !== null && ev > 0 ? "var(--wb-primary)" : "var(--wb-muted)" }}>
                        {ev === null ? "EV —" : `EV ${ev >= 0 ? "+" : ""}${(ev * 100).toFixed(1)}%`}
                      </span>
                      <Button size="sm" icon="add" disabled={r.price === null} onClick={() => onAdd(r.market, r.selection)}>Add</Button>
                    </span>
                  </li>
                );
              })}
            </ul>
            {rows.length === 0 && <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>The board quotes no market the scorelines price for this fixture.</p>}
          </div>
        );
      }}
    </Async>
  );
};
