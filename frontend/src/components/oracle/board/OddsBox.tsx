/**
 * One clickable price on the match board (Group 103): the skin's price (BetDoc: the best retail price), held in the
 * slip when highlighted in the skin's colour, and a green / red arrow when this browser saw it move since the board's
 * previous poll. Developed for Amit Ashok Kumar Patnaik.
 */
import type { SelectionCard } from "../../../lib/backtest_and_parlay";
import { priceFor } from "../../../lib/match_board";

const DRIFT_UP = "#22C55E";
const DRIFT_DOWN = "#EF4444";

interface Props {
  selection: SelectionCard;
  label: string;
  book: string | null;
  selected: boolean;
  drift?: 1 | -1;
  onPick: () => void;
  wide?: boolean;
  /** Group 112: a stored price (as bought, not live): shown, never added to the slip. */
  stored?: boolean;
}

export const OddsBox = ({ selection, label, book, selected, drift, onPick, wide = false, stored = false }: Props) => {
  const price = priceFor(selection, book, stored);
  return (
    <button
      type="button"
      onClick={onPick}
      disabled={price === undefined || stored}
      aria-pressed={selected}
      aria-label={`${selection.label} ${price ?? "not quoted"}${stored ? ", stored price, not offered" : ""}${selected ? ", in the slip" : ""}${drift ? (drift > 0 ? ", price rose" : ", price fell") : ""}`}
      title={`${selection.label} · fair ${(selection.fair_probability * 100).toFixed(1)}%${stored ? " when the price was bought · stored price: re-check when the feed prices it again" : ""}${selection.ev !== null ? ` · EV at the best price ${selection.ev >= 0 ? "+" : ""}${(selection.ev * 100).toFixed(1)}%` : ""}`}
      className={`flex ${wide ? "flex-row justify-between px-3 py-2" : "min-w-[64px] flex-col items-center px-2 py-1.5"} rounded-xl text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--wb-primary)] ${stored ? "cursor-not-allowed opacity-60" : "disabled:opacity-35"}`}
      style={selected
        ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)", fontWeight: 800 }
        : { background: "var(--wb-card)", border: `1px solid ${drift ? (drift > 0 ? DRIFT_UP : DRIFT_DOWN) : "transparent"}` }}
    >
      <span className={wide ? "font-medium" : "text-[10px] opacity-75"}>{label}</span>
      <span className="inline-flex items-center gap-0.5 font-mono font-bold tabular-nums">
        {drift && <span aria-hidden="true" style={{ color: selected ? "inherit" : drift > 0 ? DRIFT_UP : DRIFT_DOWN }}>{drift > 0 ? "▲" : "▼"}</span>}
        {price !== undefined ? price.toFixed(2) : "—"}
      </span>
    </button>
  );
};
