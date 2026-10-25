/**
 * Inside the match (Group 131): every market of one fixture as KPI cards, opened from its card on the manual bet
 * builder. Developed for Amit Ashok Kumar Patnaik.
 *
 * Two kinds of market, never mixed up:
 * - Quoted: the board's own markets, priced by the feeds (1xBet's real price; Parimatch's marked "≈ est." where the
 *   feeds do not carry it, by the universal slip's rule).
 * - BetDoc model: for three-way football, the totals, both-teams-to-score, double chance, draw no bet and handicap lines
 *   the feeds do not quote, read off the scoreline matrix fitted to this fixture's de-vigged 1X2. Their fair price is
 *   the model's; the 1xBet and Parimatch figures are estimates. They can go on the universal slip to fill at a book;
 *   the 15-pillar inspection needs a quoted price and says so.
 *
 * One pick per market: a second selection replaces the first, a picked one clicked again comes off the slip.
 */
import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import type { FixtureCard } from "../../lib/backtest_and_parlay";
import { formatDateTime } from "../../lib/format";
import { boardTargets, type MarketInput, type PricedSelection, predictFootballFromMarket } from "../../lib/prediction";
import { useResource } from "../../lib/resource";
import { bookPrice, type DeskBook, SOURCE_NOTE } from "../../lib/universal_slip";
import { priceAt, type SlipLeg, toSlipLeg } from "../../store/useParlaySlipStore";

type Category = "all" | "winner" | "double" | "goals" | "btts" | "handicap";
const CATEGORIES: { id: Category; label: string }[] = [
  { id: "all", label: "All markets" },
  { id: "winner", label: "Match winner" },
  { id: "double", label: "Double chance · DNB" },
  { id: "goals", label: "Totals" },
  { id: "btts", label: "Both teams to score" },
  { id: "handicap", label: "Handicaps" },
];
const categoryOf = (market: string): Category =>
  market === "Match Odds" || market === "h2h" ? "winner"
    : market === "Double Chance" || market === "Draw No Bet" ? "double"
      : market.startsWith("Totals") ? "goals"
        : market === "BTTS" ? "btts"
          : market.includes("Handicap") || market.startsWith("Spread") ? "handicap" : "all";

// The model lines worth a card: whole-ball totals 0.5-4.5 and the four common handicaps (the API prices every quarter line)
const MODEL_LINES = new Set(["Totals 0.5", "Totals 1.5", "Totals 2.5", "Totals 3.5", "Totals 4.5",
  "Asian Handicap -1.5", "Asian Handicap -0.5", "Asian Handicap +0.5", "Asian Handicap +1.5", "BTTS", "Double Chance", "Draw No Bet"]);

/** The slip's label for a model selection (mirrors the backend's market_selection_label). */
const modelLabel = (p: PricedSelection, home: string, away: string): string => {
  const team = p.selection === "HOME" ? home : away;
  const line = p.selection === "HOME" ? (p.line ?? 0) : -(p.line ?? 0); // the handicap from the picked side
  switch (p.kind) {
    case "TOTALS": return `Totals: ${p.selection === "OVER" ? "Over" : "Under"} ${p.line}`;
    case "BTTS": return `Both Teams to Score: ${p.selection}`;
    case "ASIAN_HANDICAP": return `Asian Handicap: ${team} (${line >= 0 ? "+" : ""}${line})`;
    case "DOUBLE_CHANCE": return `Double Chance: ${{ "1X": `${home} or Draw`, "12": `${home} or ${away}`, X2: `Draw or ${away}` }[p.selection] ?? p.selection}`;
    case "DRAW_NO_BET": return `Draw No Bet: ${team}`;
    default: return `${p.market}: ${p.selection}`;
  }
};
const shortName = (p: PricedSelection): string =>
  p.kind === "DOUBLE_CHANCE" ? p.selection : p.kind === "TOTALS" ? `${p.selection === "OVER" ? "Over" : "Under"} ${p.line}` : p.selection;

interface Tile {
  leg: SlipLeg;
  name: string;
  price: number | null;
  estimate: string | null; // the rule behind an estimated price
  fair: number;
  ev: number | null;
}

interface Group {
  market: string;
  source: "quoted" | "model";
  margin: number | null;
  tiles: Tile[];
}

const deskPrice = (leg: SlipLeg, book: string | null): { price: number | null; estimate: string | null } => {
  if (book === "1xbet" || book === "parimatch") {
    const p = bookPrice(leg, book as DeskBook);
    return { price: p?.price ?? null, estimate: p && p.source !== "quoted" ? SOURCE_NOTE[p.source] : null };
  }
  return { price: priceAt(leg, book), estimate: null };
};

const ModelMarkets = ({ card, targets, children }: { card: FixtureCard; targets: MarketInput; children: (model: PricedSelection[] | null, loading: boolean) => ReactNode }) => {
  const prediction = useResource(`prediction:board:${card.fixture_id}:${JSON.stringify(targets)}`, () => predictFootballFromMarket(targets));
  return <>{children(prediction.data?.markets ?? null, prediction.loading && prediction.data === undefined)}</>;
};

export const MatchMarketModal = ({ card, book, legIds, onPick, onClose }: {
  card: FixtureCard;
  book: string | null;
  legIds: string[];
  onPick: (leg: SlipLeg) => void;
  onClose: () => void;
}) => {
  const [category, setCategory] = useState<Category>("all");
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const targets = useMemo(() => boardTargets(card), [card]);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const quoted: Group[] = card.markets.map((m) => ({
    market: m.market,
    source: "quoted",
    margin: (book ? m.margins.by_book[book] : m.margins.best_price) ?? null,
    tiles: m.selections.map((s) => {
      const leg = toSlipLeg(card, m.market, s);
      return { leg, name: s.selection, ...deskPrice(leg, book), fair: s.fair_probability, ev: s.ev };
    }),
  }));

  const modelGroups = (model: PricedSelection[] | null): Group[] => {
    if (!model) return [];
    const onBoard = new Set(card.markets.map((m) => m.market));
    const byMarket = new Map<string, PricedSelection[]>();
    for (const p of model) {
      if (onBoard.has(p.market) || !MODEL_LINES.has(p.market) || p.fair_odds === null) continue;
      byMarket.set(p.market, [...(byMarket.get(p.market) ?? []), p]);
    }
    return [...byMarket.entries()].map(([market, sels]) => ({
      market,
      source: "model",
      margin: null,
      tiles: sels.map((p) => {
        // the fair price is the model's (a push-able line's effective win chance is 1 / fair odds)
        const leg: SlipLeg = { leg_id: `${card.fixture_id}|${p.market}|${p.selection}`, fixture: `${card.home} v ${card.away}`, label: modelLabel(p, card.home, card.away),
                               market: p.market, prices: {}, fair_probability: 1 / (p.fair_odds as number) };
        return { leg, name: shortName(p), ...deskPrice(leg, book), fair: leg.fair_probability, ev: null };
      }),
    }));
  };

  const render = (groups: Group[], modelLoading: boolean) => {
    const shown = groups.filter((g) => category === "all" || categoryOf(g.market) === category);
    return (
      <>
        <div className="scrollbar-hide flex shrink-0 gap-1.5 overflow-x-auto px-5 py-3" role="tablist" aria-label="Market category">
          {CATEGORIES.map((c) => {
            const n = groups.filter((g) => c.id === "all" || categoryOf(g.market) === c.id).length;
            return (
              <button key={c.id} type="button" role="tab" aria-selected={category === c.id} onClick={() => setCategory(c.id)} disabled={n === 0}
                      className="shrink-0 rounded-full px-3.5 py-1.5 text-xs font-semibold transition disabled:opacity-35"
                      style={category === c.id ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)" } : { background: "var(--wb-card)", color: "inherit" }}>
                {c.label} · {n}
              </button>
            );
          })}
        </div>
        <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-5 pb-5">
          {shown.length === 0 && <p className="py-8 text-center text-sm" style={{ color: "var(--wb-muted)" }}>{modelLoading ? "Pricing the model markets…" : "No market in this category."}</p>}
          {shown.map((g) => (
            <section key={g.market} className="rounded-2xl p-3.5" style={{ background: "var(--wb-card)" }} aria-label={g.market}>
              <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
                <span className="text-sm font-bold">{g.market}</span>
                {g.source === "model" ? (
                  <span className="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide" style={{ background: "rgba(250,204,21,0.14)", color: "#FACC15" }}
                        title="No feed quotes this market: the fair price is BetDoc's scoreline model, the book figures are estimates">BetDoc model · not quoted</span>
                ) : (
                  <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>{g.margin !== null ? `book margin ${(g.margin * 100).toFixed(1)}%` : "margin —"}</span>
                )}
              </div>
              <div className={`grid gap-2 ${g.tiles.length === 2 ? "grid-cols-2" : "grid-cols-3"}`}>
                {g.tiles.map((t) => {
                  const picked = legIds.includes(t.leg.leg_id);
                  return (
                    <button key={t.leg.leg_id} type="button" onClick={() => onPick(t.leg)} disabled={t.price === null} aria-pressed={picked}
                            title={`${t.leg.label}${t.estimate ? ` · price ${t.estimate}` : ""}`}
                            className="flex min-w-0 flex-col items-center gap-0.5 rounded-xl px-2 py-2.5 transition disabled:opacity-40"
                            style={picked ? { background: "color-mix(in srgb, var(--wb-primary) 22%, transparent)", outline: "2px solid var(--wb-primary)" }
                                          : { border: "1px solid color-mix(in srgb, var(--wb-primary) 30%, transparent)" }}>
                      <span className="flex max-w-full items-center gap-1 truncate text-xs font-semibold">
                        {picked && <span className="material-symbols-outlined text-[14px]" aria-hidden="true" style={{ color: "var(--wb-primary)" }}>check_circle</span>}
                        {t.name}
                      </span>
                      <span className="font-mono text-lg font-bold" style={{ color: "var(--wb-primary)" }}>
                        {t.price === null ? "—" : `${t.estimate ? "≈" : ""}${t.price.toFixed(2)}`}
                        {t.estimate && <span className="ml-0.5 align-top text-[8px] font-bold uppercase opacity-80">est.</span>}
                      </span>
                      <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
                        fair {(t.fair * 100).toFixed(0)}%
                        {t.ev !== null && <span className="ml-1 font-semibold" style={{ color: t.ev >= 0 ? "#22C55E" : "#FB7185" }}>EV {t.ev >= 0 ? "+" : ""}{(t.ev * 100).toFixed(1)}%</span>}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          ))}
          {groups.some((g) => g.source === "model") && (
            <p className="text-[11px] leading-relaxed" style={{ color: "var(--wb-muted)" }}>
              <strong>BetDoc model</strong> markets are priced off the scorelines this fixture's 1X2 implies; no feed quotes them, so their book figures are
              estimates. Fill them at the book from the universal slip; the 15-pillar inspection rates quoted prices only.
            </p>
          )}
        </div>
      </>
    );
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 backdrop-blur-sm sm:items-center sm:p-4" role="presentation"
         onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-modal="true" aria-labelledby={titleId}
           className="flex max-h-[92vh] w-full max-w-3xl flex-col overflow-hidden rounded-t-3xl shadow-2xl sm:rounded-3xl"
           style={{ background: "var(--wb-surface, #0B0F17)", border: "1px solid rgba(255,255,255,0.08)" }}>
        <header className="flex items-start justify-between gap-3 border-b px-5 pb-4 pt-5" style={{ borderColor: "rgba(255,255,255,0.08)" }}>
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--wb-primary)" }}>
              {card.league ?? card.sport ?? card.sport_key ?? "Fixture"}{card.in_play ? ` · LIVE${card.minutes_since_kickoff !== null ? ` ${card.minutes_since_kickoff}′` : ""}` : ""}
            </p>
            <h2 id={titleId} className="mt-0.5 truncate text-xl font-bold tracking-tight">{card.home} v {card.away}</h2>
            <p className="text-xs" style={{ color: "var(--wb-muted)" }}>
              {card.kickoff ? formatDateTime(card.kickoff) : "kickoff unknown"}
              {card.projection && <> · likeliest score <strong className="font-mono" style={{ color: "var(--wb-primary)" }}>{card.projection.score}</strong></>}
            </p>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close the match"
                  className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition hover:opacity-80" style={{ background: "var(--wb-card)" }}>
            <span className="material-symbols-outlined text-xl" aria-hidden="true">close</span>
          </button>
        </header>
        {targets ? (
          <ModelMarkets card={card} targets={targets}>
            {(model, loading) => render([...quoted, ...modelGroups(model)], loading)}
          </ModelMarkets>
        ) : render(quoted, false)}
      </div>
    </div>
  );
};
