/**
 * The universal betslip of the manual bet builder (Group 130): one slip the operator can fill at Parimatch or at 1xBet,
 * with the return and profit at each. Developed for Amit Ashok Kumar Patnaik.
 *
 * The odds feeds quote 1xBet; they do not carry Parimatch. A Parimatch figure here is therefore an ESTIMATE by the
 * operator's own rule (98.5% of 1xBet's price, the "19-20" difference; 95% of the fair price when 1xBet is not quoted
 * either), and every estimate says so: `source` is never "quoted" for it. It is a planning figure for the slip only: the
 * fortress, the EV figures, the Chef, the synthesizer and recorded tickets use quoted prices, and the odds Parimatch
 * actually gives are what gets recorded. Not used by the slip scanner, which reads the bookmaker's own slip.
 */
export type DeskBook = "1xbet" | "parimatch";
export type PriceSource = "quoted" | "from_1xbet" | "from_parimatch" | "from_consensus";

export interface BookPrice {
  price: number;
  source: PriceSource;
}

interface PricedLeg {
  prices: Record<string, number>;
  fair_probability: number;
}

/** The operator's rules (Group 130 brief): Parimatch ~1.5% under 1xBet; retail returns 95% / 96% of the fair price. */
export const PARIMATCH_PER_1XBET = 0.985;
export const ONEXBET_PER_PARIMATCH = 1.015;
export const CONSENSUS_RETURN: Record<DeskBook, number> = { "1xbet": 0.96, parimatch: 0.95 };
const MIN_ODDS = 1.01;

const round2 = (x: number): number => Math.round(x * 100) / 100;
const quoted = (leg: PricedLeg, book: string): number | null => {
  const p = leg.prices[book];
  return typeof p === "number" && Number.isFinite(p) && p > 1 ? p : null;
};

/** A leg's price at one desk book: quoted when the feeds quote it, else an estimate that names its rule; null without either. */
export const bookPrice = (leg: PricedLeg, book: DeskBook): BookPrice | null => {
  const own = quoted(leg, book);
  if (own !== null) return { price: own, source: "quoted" };
  const other = quoted(leg, book === "parimatch" ? "1xbet" : "parimatch");
  if (other !== null) {
    const factor = book === "parimatch" ? PARIMATCH_PER_1XBET : ONEXBET_PER_PARIMATCH;
    return { price: Math.max(MIN_ODDS, round2(other * factor)), source: book === "parimatch" ? "from_1xbet" : "from_parimatch" };
  }
  if (leg.fair_probability > 0 && leg.fair_probability < 1) {
    return { price: Math.max(MIN_ODDS, round2(CONSENSUS_RETURN[book] / leg.fair_probability)), source: "from_consensus" };
  }
  return null;
};

export const SOURCE_NOTE: Record<Exclude<PriceSource, "quoted">, string> = {
  from_1xbet: "estimated at 98.5% of 1xBet's quoted price (the operator's rule)",
  from_parimatch: "estimated at 101.5% of Parimatch's quoted price (the operator's rule)",
  from_consensus: "estimated from the books' fair price less a retail margin (neither desk book quotes it)",
};

export interface BookTotals {
  book: DeskBook;
  odds: number | null; // null: a leg has no price there at all
  payout: number | null;
  profit: number | null;
  estimatedLegs: number; // legs whose price here is an estimate
}

export interface UniversalSlip {
  books: Record<DeskBook, BookTotals>;
  gapPct: number | null; // |payout at 1xBet - payout at Parimatch| / payout at 1xBet
}

const totals = (legs: PricedLeg[], book: DeskBook, stake: number): BookTotals => {
  const prices = legs.map((leg) => bookPrice(leg, book));
  if (!legs.length || prices.some((p) => p === null)) return { book, odds: null, payout: null, profit: null, estimatedLegs: 0 };
  const odds = prices.reduce((acc, p) => acc * (p as BookPrice).price, 1);
  const payout = stake > 0 ? round2(stake * odds) : null;
  return { book, odds: round2(odds), payout, profit: payout === null ? null : round2(payout - stake), estimatedLegs: prices.filter((p) => p?.source !== "quoted").length };
};

/** Both desk books' combined odds, return and profit for one stake; recomputed on every keystroke. */
export const universalSlip = (legs: PricedLeg[], stake: number): UniversalSlip => {
  const books = { "1xbet": totals(legs, "1xbet", stake), parimatch: totals(legs, "parimatch", stake) };
  const a = books["1xbet"].payout;
  const b = books.parimatch.payout;
  return { books, gapPct: a && b ? round2((Math.abs(a - b) / a) * 100) : null };
};
