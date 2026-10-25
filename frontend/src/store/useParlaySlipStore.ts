/**
 * One parlay slip for The Oracle (Group 103): the match board and the manual parlay workbench (Group 77) build the
 * same slip in the same bookmaker skin, so a leg picked on the board is the leg the workbench inspects against the 15
 * pillars and records. Developed for Amit Ashok Kumar Patnaik.
 *
 * Every leg is a selection the feeds quote (its `leg_id` is Ashoka's `fixture|market|selection`); nothing is placed
 * from here. Kept for the browser session, so moving between tabs and pages keeps the slip.
 *
 * Group 131: one pick per market of a fixture. A second selection of the same fixture's market (Home after Away, Over
 * 2.5 after Under 2.5) replaces the first; clicking a picked selection again takes it off the slip.
 */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { type Board, BOOKMAKER_THEMES, type FixtureCard, type SelectionCard, type Skin } from "../lib/backtest_and_parlay";
import { pick, type PickResult as Rules } from "../lib/slip_rules";
import { toast } from "./useToastStore";

export interface SlipLeg {
  leg_id: string;
  fixture: string;
  label: string;
  market: string;
  prices: Record<string, number>;
  fair_probability: number;
}

const RETAIL = ["parimatch", "1xbet", "stake"];

/** The leg's price at a skin's book; BetDoc (no book): the best retail price. */
export const priceAt = (leg: Pick<SlipLeg, "prices">, book: string | null): number | null => {
  if (book) return leg.prices[book] ?? null;
  const values = Object.entries(leg.prices).filter(([b]) => RETAIL.includes(b)).map(([, v]) => v);
  return values.length ? Math.max(...values) : null;
};

/** The book that pays the shown price: the skin's own, or (BetDoc best price) whichever retail book pays most. */
export const payingBook = (leg: Pick<SlipLeg, "prices">, book: string | null): string | null => {
  if (book) return leg.prices[book] !== undefined ? book : null;
  const best = Object.entries(leg.prices).filter(([b]) => RETAIL.includes(b)).sort((a, b) => b[1] - a[1])[0];
  return best ? best[0] : null;
};

export type PickResult = Rules<SlipLeg>;

/** Says what a pick did when it was not a plain add (a replaced selection is easy to miss). */
export const announcePick = (result: PickResult, leg: SlipLeg): void => {
  if (result.action === "replaced" && result.replaced) toast.info("Pick replaced", `${result.replaced.label} is off the slip: one pick per market, now ${leg.label}`);
  else if (result.action === "removed") toast.info("Taken off the slip", leg.label);
};

export const toSlipLeg = (card: Pick<FixtureCard, "home" | "away">, market: string, s: SelectionCard): SlipLeg => ({
  leg_id: s.leg_id, fixture: `${card.home} v ${card.away}`, label: s.label, market, prices: s.prices, fair_probability: s.fair_probability,
});

interface ParlaySlipState {
  legs: SlipLeg[];
  skin: Skin;
  /** Add a leg (a selection already on the slip stays); another selection of its market is replaced. */
  add: (leg: SlipLeg) => PickResult;
  addMany: (legs: SlipLeg[]) => void;
  /** As add, but a picked selection clicked again comes off the slip. */
  toggle: (leg: SlipLeg) => PickResult;
  remove: (legId: string) => void;
  /** Swap one leg for another in place (the auto-fix keeps the slip's order). */
  replace: (legId: string, leg: SlipLeg) => void;
  clear: () => void;
  setSkin: (skin: Skin) => void;
  /** Group 130: every slip leg the board prices again takes the board's latest prices (a slip never calculates on stale ones) */
  syncWithBoard: (board: Board) => void;
}

const isSkin = (v: unknown): v is Skin => typeof v === "string" && v in BOOKMAKER_THEMES;

export const useParlaySlipStore = create<ParlaySlipState>()(
  persist(
    (set, get) => ({
      legs: [],
      skin: "betdoc",
      add: (leg) => {
        const next = pick(get().legs, leg, false);
        if (next.result.action !== "kept") set({ legs: next.legs });
        return next.result;
      },
      addMany: (legs) => set((s) => ({ legs: legs.reduce((acc, l) => pick(acc, l, false).legs, s.legs) })),
      toggle: (leg) => {
        const next = pick(get().legs, leg, true);
        set({ legs: next.legs });
        return next.result;
      },
      remove: (legId) => set((s) => ({ legs: s.legs.filter((l) => l.leg_id !== legId) })),
      replace: (legId, leg) => set((s) => ({ legs: s.legs.map((l) => (l.leg_id === legId ? leg : l)).filter((l, i, all) => all.findIndex((x) => x.leg_id === l.leg_id) === i) })),
      clear: () => set({ legs: [] }),
      setSkin: (skin) => set({ skin: isSkin(skin) ? skin : "betdoc" }),
      syncWithBoard: (board) => set((s) => {
        if (!s.legs.length || !board.fixtures.length) return s;
        const latest = new Map(board.fixtures.flatMap((f) => f.markets.flatMap((m) => m.selections.map((sel) => [sel.leg_id, sel] as const))));
        let changed = false;
        const legs = s.legs.map((leg) => {
          const sel = latest.get(leg.leg_id);
          if (!sel || (sel.fair_probability === leg.fair_probability && JSON.stringify(sel.prices) === JSON.stringify(leg.prices))) return leg;
          changed = true;
          return { ...leg, prices: sel.prices, fair_probability: sel.fair_probability };
        });
        return changed ? { legs } : s;
      }),
    }),
    {
      name: "betdoc:parlay-slip",
      storage: createJSONStorage(() => sessionStorage),
      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<ParlaySlipState>;
        const legs = Array.isArray(p.legs) ? p.legs.filter((l) => l && typeof l.leg_id === "string" && typeof l.label === "string" && l.prices) : [];
        return { ...current, legs, skin: isSkin(p.skin) ? p.skin : current.skin };
      },
    },
  ),
);

/** The slip's signature: an inspection is only valid for the exact legs, structure and skin it was run on. */
export const slipSignature = (legs: SlipLeg[], skin: Skin, kind: string | null): string => `${skin}|${kind ?? ""}|${legs.map((l) => l.leg_id).join(",")}`;
