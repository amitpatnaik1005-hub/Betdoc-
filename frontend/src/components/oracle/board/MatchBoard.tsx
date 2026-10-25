/**
 * The Oracle's match board (Group 103, points b.4 and b.11): BetDoc's own bookmaker-style board over the market it
 * already prices, in the skin of the book you bet from (Parimatch, 1xBet, Stake, or BetDoc's best price).
 * Developed for Amit Ashok Kumar Patnaik.
 *
 * Toolbar (sports, Cricket and Football first; time window; market column; skin) · tournament accordion · fixture
 * rows with clickable prices and drift · a fixture's page with its score prediction, category tabs and market
 * accordions · Ashoka's top vetted parlays · the betslip rail. Everything priced comes from the authorised feeds and
 * BetDoc's own models; nothing is placed from here.
 */
import { type CSSProperties, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { BOOKMAKER_THEMES, type MarketCard, type SelectionCard } from "../../../lib/backtest_and_parlay";
import { type BoardFixture, type BoardWindow, type MarketColumn, useMatchBoard } from "../../../lib/match_board";
import type { Slip } from "../../../lib/oracle";
import { announcePick, toSlipLeg, useParlaySlipStore } from "../../../store/useParlaySlipStore";
import { Async, EmptyState, Panel } from "../../../ui/kit";
import { MatchBoardToolbar } from "./MatchBoardToolbar";
import { MatchDetail } from "./MatchDetail";
import { MatchRow } from "./MatchRow";
import { SlipRail } from "./SlipRail";
import { TopParlays } from "./TopParlays";
import { TournamentFilter } from "./TournamentFilter";

const TOP_PARLAYS = 2;

export const MatchBoard = ({ slips, onRecord }: { slips: Slip[]; onRecord: () => void }) => {
  const [sport, setSport] = useState<string | null>(null);
  const [window, setWindow] = useState<BoardWindow>("all");
  const [column, setColumn] = useState<MarketColumn>("winner");
  const [leagues, setLeagues] = useState<Set<string>>(new Set());
  // Group 108: "Open in Oracle" on VIDUR's board lands on the fixture (?fixture=<id>); leaving it clears the link
  const [params, setParams] = useSearchParams();
  const [picked, setPicked] = useState<string | null>(null);
  const openId = params.get("fixture") ?? picked;  // a followed link wins, even with the board already open
  const setOpenId = (id: string | null) => {
    setPicked(id);
    if (params.has("fixture")) setParams((prev) => { const p = new URLSearchParams(prev); p.delete("fixture"); return p; }, { replace: true });
  };
  const skin = useParlaySlipStore((s) => s.skin);
  const setSkin = useParlaySlipStore((s) => s.setSkin);
  const legs = useParlaySlipStore((s) => s.legs);
  const toggle = useParlaySlipStore((s) => s.toggle);
  const addMany = useParlaySlipStore((s) => s.addMany);
  const board = useMatchBoard(sport, window);
  const theme = BOOKMAKER_THEMES[skin];
  const held = new Set(legs.map((l) => l.leg_id));
  const inSlip = (legId: string) => held.has(legId);

  const pick = (card: BoardFixture) => (market: MarketCard, s: SelectionCard) => {
    if (card.priced_from !== "stored") { const leg = toSlipLeg(card, market.market, s); announcePick(toggle(leg), leg); }  // Group 112: a stored price is never slipped
  };
  const addParlay = (slip: Slip) =>
    addMany(slip.legs.map((l) => ({ leg_id: l.leg_id, fixture: l.fixture, label: l.label, market: l.market, prices: l.prices, fair_probability: l.fair_probability })));
  const style = { "--wb-primary": theme.primary, "--wb-on-primary": theme.onPrimary, "--wb-card": theme.card, "--wb-muted": theme.muted,
                  background: theme.surface, color: theme.text, fontFamily: theme.font } as CSSProperties;

  return (
    <Panel title="Match board" icon="stadium" className="lg:col-span-12" updatedAt={board.updatedAt}
           subtitle="Every fixture the feeds price, by sport, country and kickoff · prices at the book you bet from">
      <div className="flex flex-col gap-3 rounded-3xl p-3 transition-colors duration-300 sm:p-4" style={style}>
        <Async resource={board} skeletonRows={6}>
          {(b) => {
            const fixtures = leagues.size ? b.fixtures.filter((f) => leagues.has(f.league_name ?? "Unnamed league")) : b.fixtures;
            const open = openId ? b.fixtures.find((f) => f.fixture_id === openId) ?? null : null;
            const fixtureParlays = open ? slips.filter((s) => s.legs.some((l) => l.fixture_id === open.fixture_id)) : [];
            return (
              <>
                <MatchBoardToolbar sports={b.sports} total={b.sports.reduce((a, s) => a + s.count, 0)} sport={sport} onSport={(s) => { setSport(s); setLeagues(new Set()); setOpenId(null); }}
                                   window={window} onWindow={(w) => { setWindow(w); setOpenId(null); }} column={column} onColumn={setColumn} skin={skin} onSkin={setSkin} />
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1fr)_19rem] 2xl:grid-cols-[13rem_minmax(0,1fr)_20rem]">
                  <div className="lg:col-start-1 lg:row-start-1 2xl:col-start-1">
                  <TournamentFilter countries={b.countries} selected={leagues} onClear={() => setLeagues(new Set())}
                                    onToggle={(league) => setLeagues((prev) => { const next = new Set(prev); if (next.has(league)) next.delete(league); else next.add(league); return next; })} />
                  </div>
                  <div className="flex min-w-0 flex-col gap-3 lg:col-start-1 lg:row-start-2 2xl:col-start-2 2xl:row-start-1">
                    {openId && open ? (
                      <MatchDetail card={open} book={theme.book} inSlip={inSlip} drift={b.drift} onPick={pick(open)} onBack={() => setOpenId(null)}
                                   parlays={fixtureParlays} onAddParlay={addParlay} />
                    ) : (
                      <>
                        {openId && !open && (
                          <p className="rounded-xl p-3 text-xs" style={{ background: "var(--wb-card)" }}>That fixture has left the board (no longer quoted in this window).</p>
                        )}
                        <h3 className="text-[11px] font-bold uppercase tracking-wider" style={{ color: "var(--wb-muted)" }}>
                          {window === "live" ? "Live" : "Live and upcoming"} · {fixtures.length} of {b.total} fixture{b.total === 1 ? "" : "s"}
                        </h3>
                        {fixtures.length === 0 ? (
                          <EmptyState icon="stadium" title={window === "live" ? "Nothing in play right now" : "No fixture priced in this window"}
                                      detail="Fixtures appear as the authorised feeds price them (and stay for three hours after kickoff)." />
                        ) : (
                          <ul className="flex flex-col gap-2">
                            {fixtures.map((card) => (
                              <MatchRow key={card.fixture_id} card={card} column={column} book={theme.book} inSlip={inSlip} drift={b.drift}
                                        onPick={pick(card)} onOpen={() => setOpenId(card.fixture_id)} />
                            ))}
                          </ul>
                        )}
                        {!b.steam_readable && <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>Sharp steam is unreadable right now.</p>}
                        {/* Group 111: the fixtures first (the Chef's Table's parlay of the day sits above the board), then Ashoka's top parlays */}
                        {slips.length > 0 && <TopParlays slips={slips.slice(0, TOP_PARLAYS)} book={theme.book} inSlip={inSlip} onAdd={addParlay} />}
                      </>
                    )}
                  </div>
                  <div className="lg:col-start-2 lg:row-span-2 lg:row-start-1 2xl:col-start-3 2xl:row-span-1">
                    <SlipRail fixtures={b.fixtures} onRecord={onRecord} />
                  </div>
                </div>
              </>
            );
          }}
        </Async>
      </div>
    </Panel>
  );
};
