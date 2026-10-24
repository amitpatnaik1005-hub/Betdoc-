/**
 * Parimatch is not in the odds feeds BetDoc is connected to (The Odds API and OddsPapi do not carry it): its prices show as
 * "—". BetDoc never copies another book's price or derives one and calls it Parimatch's, because a slip staked on a price
 * Parimatch never offered is vetted on numbers that are not real (Group 129). The note says so once per board. A feed that
 * carries Parimatch can push its prices through `POST /parimatch/odds` or its webhook. Developed for Amit Ashok Kumar Patnaik.
 */
export const ParimatchFeedNote = () => (
  <p role="note" className="rounded-xl px-3 py-2 text-[11px] leading-relaxed" style={{ background: "color-mix(in srgb, var(--wb-primary) 10%, transparent)", color: "var(--wb-muted)" }}>
    <strong style={{ color: "var(--wb-primary)" }}>Parimatch is not in the connected odds feeds</strong> (The Odds API and OddsPapi do not carry it), so its prices
    show as —. BetDoc does not copy 1xBet's price or invent one for Parimatch. Use 1xBet or the best retail price here, or connect a feed that carries
    Parimatch.
  </p>
);
