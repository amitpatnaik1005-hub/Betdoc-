/**
 * One fixture on the match board (Group 103, point b.11 screenshot 1): league and kickoff (or minutes since kickoff
 * while in play), the teams, the market-implied score for three-way football, the best EV on offer, the count of
 * markets priced, and the chosen market column as clickable prices. Group 112: a fixture the live market is not pricing
 * shows its stored prices (marked, aged, not clickable), and the sports-data feeds' context where one paired it
 * (postponed, live or full-time score, kickoff confirmed or moved, table positions). Developed for Amit Ashok Kumar Patnaik.
 */
import type { MarketCard, SelectionCard } from "../../../lib/backtest_and_parlay";
import { formatDateTime } from "../../../lib/format";
import { type BoardFixture, boxLabel, columnMarket, driftFor, type MarketColumn, type MatchBoard } from "../../../lib/match_board";
import { FixtureContextChips, StoredPricesBadge } from "./FixtureContextChips";
import { OddsBox } from "./OddsBox";

interface Props {
  card: BoardFixture;
  column: MarketColumn;
  book: string | null;
  inSlip: (legId: string) => boolean;
  drift: MatchBoard["drift"];
  onPick: (market: MarketCard, s: SelectionCard) => void;
  onOpen: () => void;
}

export const MatchRow = ({ card, column, book, inSlip, drift, onPick, onOpen }: Props) => {
  const market = columnMarket(card, column);
  return (
    <li className="flex flex-col gap-2 rounded-2xl p-3 xl:flex-row xl:items-center xl:justify-between" style={{ background: "color-mix(in srgb, var(--wb-card) 70%, transparent)" }}>
      <button type="button" onClick={onOpen} className="flex min-w-0 flex-1 flex-col gap-1 text-left">
        <span className="flex flex-wrap items-center gap-x-2 text-[11px]" style={{ color: "var(--wb-muted)" }}>
          <span className="truncate">{card.league_name ?? card.sport ?? card.sport_key ?? "—"}</span>
          {card.in_play
            ? <span className="font-bold text-rose-400">LIVE · {card.minutes_since_kickoff}′ since kickoff</span>
            : <span>{card.kickoff ? formatDateTime(card.kickoff) : "kickoff not stated"}</span>}
          {card.priced_from === "stored" && <StoredPricesBadge asOf={card.prices_as_of} ageMinutes={card.prices_age_minutes} />}
          <FixtureContextChips context={card.context} />
          {card.best_ev !== null && card.best_ev > 0 && (
            <span className="rounded-full px-1.5 font-bold" style={{ background: "color-mix(in srgb, #22C55E 20%, transparent)", color: "#22C55E" }}>
              +{(card.best_ev * 100).toFixed(1)}% EV
            </span>
          )}
        </span>
        <span className="truncate text-sm font-semibold">{card.home}</span>
        <span className="truncate text-sm font-semibold">{card.away}</span>
        {card.projection && (
          <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
            BetDoc score <strong style={{ color: "var(--wb-primary)" }}>{card.projection.score}</strong> · {(card.projection.probability * 100).toFixed(1)}% ·
            xG {card.projection.expected_goals[0].toFixed(2)}–{card.projection.expected_goals[1].toFixed(2)}
          </span>
        )}
      </button>
      <div className="flex shrink-0 items-center gap-1.5">
        <button type="button" onClick={onOpen} className="rounded-lg px-2 py-2 text-xs font-bold" style={{ background: "var(--wb-card)" }}
                aria-label={`${card.market_count} markets for ${card.home} v ${card.away}`}>
          +{card.market_count} ›
        </button>
        {market ? (
          market.selections.map((s) => (
            <OddsBox key={s.leg_id} selection={s} label={boxLabel(market, s)} book={book} selected={inSlip(s.leg_id)} stored={card.priced_from === "stored"}
                     drift={driftFor(drift, s, book)} onPick={() => onPick(market, s)} />
          ))
        ) : (
          <span className="px-2 text-[11px]" style={{ color: "var(--wb-muted)" }}>no {column} market</span>
        )}
      </div>
    </li>
  );
};
