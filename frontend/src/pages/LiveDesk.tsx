/**
 * Live Real-Money Trading Desk (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Primary execution router for Parimatch (INR) & 1xBet.
 * Commander ASHVATTHAMA (#17 - Execution Router) & Commander TODAR MAL (#8 - Cash Vault).
 * Commander SHIVAJI (#5 - 15% Daily Drawdown Circuit Breaker & 3-Loss Cool-down Shield).
 */

import { useMemo, useState } from "react";
import { ParimatchExecutionModal } from "../components/real_money/ParimatchExecutionModal";
import { RealActiveTickets } from "../components/real_money/RealActiveTickets";
import { RealBankrollCard } from "../components/real_money/RealBankrollCard";
import { RealHistoryTable } from "../components/real_money/RealHistoryTable";
import { TaxCertificateModal } from "../components/real_money/TaxCertificateModal";
import { useRealMoneyStore } from "../store/useRealMoneyStore";
import { formatINR } from "../utils/formatCurrency";

interface ParlayLeg {
  id: string;
  fixture: string;
  market: string;
  selection: string;
  odds: number;
  prob: number;
}

const DEFAULT_PARLAY_LEGS: ParlayLeg[] = [
  {
    id: "leg-1",
    fixture: "Arsenal v Leeds United",
    market: "Match Odds",
    selection: "Arsenal (Home)",
    odds: 1.44,
    prob: 0.707,
  },
  {
    id: "leg-2",
    fixture: "Chelsea v Bournemouth",
    market: "Match Odds",
    selection: "Chelsea (Home)",
    odds: 1.79,
    prob: 0.585,
  },
  {
    id: "leg-3",
    fixture: "Aston Villa v Brentford",
    market: "Match Odds",
    selection: "Aston Villa (Home)",
    odds: 2.78,
    prob: 0.38,
  },
];

export const LiveDesk = () => {
  const activeTicket = useRealMoneyStore((s) => s.activeTicket);
  const isExecutionOpen = useRealMoneyStore((s) => s.isExecutionModalOpen);
  const closeExecution = useRealMoneyStore((s) => s.closeExecutionModal);
  const createAndOpenTicket = useRealMoneyStore((s) => s.createAndOpenTicket);
  const isLoading = useRealMoneyStore((s) => s.isLoading);

  // Desk Mode: Parlay (Accumulator) vs Single
  const [deskMode, setDeskMode] = useState<"parlay" | "single">("parlay");

  // Bookmaker
  const [bookmaker, setBookmaker] = useState("parimatch");

  // Parlay Builder State
  const [parlayLegs, setParlayLegs] = useState<ParlayLeg[]>(DEFAULT_PARLAY_LEGS);

  // Single Ticket Builder State
  const [sport, setSport] = useState("soccer");
  const [league, setLeague] = useState("Premier League");
  const [eventName, setEventName] = useState("Arsenal vs Chelsea");
  const [marketType, setMarketType] = useState("Match Odds");
  const [selection, setSelection] = useState("Arsenal");
  const [targetOdds, setTargetOdds] = useState("2.10");
  const [trueProb, setTrueProb] = useState("0.52");

  // Telemetry for Parlay
  const parlayCombinedOdds = useMemo(() => {
    return parlayLegs.reduce((acc, l) => acc * (l.odds > 1 ? l.odds : 1), 1);
  }, [parlayLegs]);

  const parlayJointProb = useMemo(() => {
    return parlayLegs.reduce((acc, l) => acc * (l.prob > 0 ? l.prob : 0.5), 1);
  }, [parlayLegs]);

  const parlayEV = useMemo(() => {
    return parlayJointProb * parlayCombinedOdds - 1;
  }, [parlayJointProb, parlayCombinedOdds]);

  const handleAddLeg = () => {
    const newId = `leg-${Date.now()}`;
    setParlayLegs((prev) => [
      ...prev,
      {
        id: newId,
        fixture: "Manchester United v Tottenham",
        market: "Match Odds",
        selection: "Man Utd (Home)",
        odds: 1.76,
        prob: 0.59,
      },
    ]);
  };

  const handleRemoveLeg = (id: string) => {
    if (parlayLegs.length <= 1) return;
    setParlayLegs((prev) => prev.filter((l) => l.id !== id));
  };

  const handleUpdateLeg = (id: string, field: keyof ParlayLeg, value: string | number) => {
    setParlayLegs((prev) =>
      prev.map((l) => (l.id === id ? { ...l, [field]: value } : l))
    );
  };

  const handleAutoBuildParlay = () => {
    setParlayLegs(DEFAULT_PARLAY_LEGS);
  };

  // Submit Parlay Ticket
  const handleCreateParlayTicket = async (e: React.FormEvent) => {
    e.preventDefault();
    if (parlayLegs.length < 2) return;
    if (parlayCombinedOdds <= 1.0) return;

    const shortSummary = parlayLegs
      .map((l) => `${l.fixture.split(" v ")[0]} (${l.odds.toFixed(2)})`)
      .join(" + ");

    const legsPayload = parlayLegs.map((l) => ({
      fixture: l.fixture,
      market: l.market,
      selection: l.selection,
      odds: l.odds,
      prob: l.prob,
    }));

    await createAndOpenTicket({
      sport: "soccer",
      league: `Multi-Match Parlay (${parlayLegs.length} Legs)`,
      match_id: `parlay_${Date.now()}`,
      event_name: shortSummary,
      market_type: `PARLAY (${parlayLegs.length} LEGS)`,
      selection: parlayLegs.map((l) => l.selection).join(" · "),
      target_odds: parseFloat(parlayCombinedOdds.toFixed(3)),
      true_probability: Math.min(0.99, Math.max(0.01, parseFloat(parlayJointProb.toFixed(4)))),
      model_edge_pct: parseFloat((parlayEV * 100).toFixed(2)),
      bookmaker,
      notes: JSON.stringify({ legs: legsPayload }),
    });
  };

  // Submit Single Ticket
  const handleCreateSingleTicket = async (e: React.FormEvent) => {
    e.preventDefault();
    const odds = parseFloat(targetOdds);
    const prob = parseFloat(trueProb);
    if (!odds || odds <= 1.0) return;
    if (!prob || prob <= 0 || prob >= 1) return;

    await createAndOpenTicket({
      sport,
      league,
      match_id: `custom_${Date.now()}`,
      event_name: eventName,
      market_type: marketType,
      selection,
      target_odds: odds,
      true_probability: prob,
      bookmaker,
    });
  };

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-8 py-6">
      {/* Page Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="flex size-3 rounded-full bg-rose-500 animate-pulse" />
            <h1 className="text-2xl font-black tracking-tight text-stone-900 uppercase dark:text-stone-100 sm:text-3xl">
              Live Real-Money Desk
            </h1>
            <span className="rounded-lg bg-orange-500/10 px-2.5 py-0.5 font-mono text-xs font-bold text-orange-600 dark:bg-orange-500/20 dark:text-orange-400">
              ASHVATTHAMA #17
            </span>
          </div>
          <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
            Assisted Live Execution for Parimatch (INR) & 1xBet | Exact ₹ Cash Vault | Shivaji Shield Active
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span className="rounded-xl border border-stone-200 bg-stone-50 px-3 py-1.5 font-mono text-xs text-stone-600 dark:border-stone-800 dark:bg-stone-900 dark:text-stone-400">
            Primary Book: <span className="font-bold text-amber-500">Parimatch (INR)</span>
          </span>
          <span className="rounded-xl border border-emerald-300/40 bg-emerald-50 px-3 py-1.5 font-mono text-xs text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300">
            Sec 115BBJ: <span className="font-bold">30% Flat TDS</span>
          </span>
        </div>
      </div>

      {/* Main Bankroll & Shivaji Shield Card */}
      <RealBankrollCard />

      {/* Grid: Parlay / Single Builder + Active Tickets */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Slip Builder Terminal */}
        <div className="lg:col-span-6 flex flex-col justify-between rounded-3xl border border-stone-200 bg-white p-6 shadow-sm dark:border-stone-800 dark:bg-stone-900/90">
          <div>
            {/* Header & Tabs */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-stone-100 pb-4 dark:border-stone-800">
              <div className="flex items-center gap-2">
                <span className="text-xl">🎯</span>
                <div>
                  <h3 className="text-sm font-bold text-stone-900 dark:text-stone-100">
                    Assisted Parimatch Slip Builder
                  </h3>
                  <p className="text-[11px] text-stone-400">
                    KUMBHA fractional Kelly stake · ASHVATTHAMA odds floor · Exact clipboard copy
                  </p>
                </div>
              </div>

              {/* Mode Switcher */}
              <div className="flex rounded-xl bg-stone-100 p-1 dark:bg-stone-800">
                <button
                  type="button"
                  onClick={() => setDeskMode("parlay")}
                  className={`rounded-lg px-3 py-1 text-xs font-bold transition ${
                    deskMode === "parlay"
                      ? "bg-orange-600 text-white shadow-sm"
                      : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200"
                  }`}
                >
                  🎯 Multi-Leg Parlay
                </button>
                <button
                  type="button"
                  onClick={() => setDeskMode("single")}
                  className={`rounded-lg px-3 py-1 text-xs font-bold transition ${
                    deskMode === "single"
                      ? "bg-orange-600 text-white shadow-sm"
                      : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200"
                  }`}
                >
                  ⚡ Single Bet
                </button>
              </div>
            </div>

            {/* Bookmaker Selector Bar */}
            <div className="mt-3 flex items-center justify-between rounded-xl bg-stone-50 p-2.5 dark:bg-stone-800/40">
              <span className="text-xs font-semibold text-stone-600 dark:text-stone-300">
                Target Bookmaker:
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setBookmaker("parimatch")}
                  className={`rounded-lg px-3 py-1 text-xs font-bold transition ${
                    bookmaker === "parimatch"
                      ? "bg-amber-500 text-stone-900 shadow-sm"
                      : "bg-stone-200/60 text-stone-600 dark:bg-stone-700 dark:text-stone-300"
                  }`}
                >
                  Parimatch (INR)
                </button>
                <button
                  type="button"
                  onClick={() => setBookmaker("onexbet")}
                  className={`rounded-lg px-3 py-1 text-xs font-bold transition ${
                    bookmaker === "onexbet"
                      ? "bg-sky-600 text-white shadow-sm"
                      : "bg-stone-200/60 text-stone-600 dark:bg-stone-700 dark:text-stone-300"
                  }`}
                >
                  1xBet
                </button>
              </div>
            </div>

            {/* PARLAY BUILDER MODE */}
            {deskMode === "parlay" ? (
              <form onSubmit={handleCreateParlayTicket} className="mt-4 space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-stone-700 dark:text-stone-300">
                    Parlay Legs ({parlayLegs.length})
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={handleAutoBuildParlay}
                      className="rounded-lg bg-emerald-500/10 px-2.5 py-1 text-[11px] font-bold text-emerald-600 transition hover:bg-emerald-500/20 dark:bg-emerald-500/20 dark:text-emerald-400"
                      title="Auto-load top value legs from live odds"
                    >
                      🤖 Auto-Build Value Parlay
                    </button>
                    <button
                      type="button"
                      onClick={handleAddLeg}
                      className="rounded-lg bg-stone-100 px-2.5 py-1 text-[11px] font-bold text-stone-700 transition hover:bg-stone-200 dark:bg-stone-800 dark:text-stone-200 dark:hover:bg-stone-700"
                    >
                      + Add Leg
                    </button>
                  </div>
                </div>

                {/* Legs List */}
                <div className="space-y-2.5 max-h-[340px] overflow-y-auto pr-1">
                  {parlayLegs.map((leg, index) => (
                    <div
                      key={leg.id}
                      className="flex flex-col gap-2 rounded-2xl border border-stone-200 bg-stone-50/80 p-3 dark:border-stone-800 dark:bg-stone-800/50"
                    >
                      <div className="flex items-center justify-between">
                        <span className="flex size-5 items-center justify-center rounded-full bg-orange-600 text-[10px] font-bold text-white">
                          {index + 1}
                        </span>
                        <input
                          type="text"
                          value={leg.fixture}
                          onChange={(e) => handleUpdateLeg(leg.id, "fixture", e.target.value)}
                          placeholder="Fixture (e.g. Arsenal v Leeds)"
                          className="mx-2 flex-1 rounded-lg border border-stone-200 bg-white px-2 py-1 text-xs font-semibold text-stone-800 dark:border-stone-700 dark:bg-stone-900 dark:text-stone-100"
                        />
                        {parlayLegs.length > 2 && (
                          <button
                            type="button"
                            onClick={() => handleRemoveLeg(leg.id)}
                            className="rounded-lg p-1 text-stone-400 hover:text-rose-500"
                            title="Remove leg"
                          >
                            ✕
                          </button>
                        )}
                      </div>

                      <div className="grid grid-cols-3 gap-2">
                        <div>
                          <label className="text-[10px] font-medium text-stone-400">Selection</label>
                          <input
                            type="text"
                            value={leg.selection}
                            onChange={(e) => handleUpdateLeg(leg.id, "selection", e.target.value)}
                            className="w-full rounded-lg border border-stone-200 bg-white px-2 py-1 text-xs dark:border-stone-700 dark:bg-stone-900 dark:text-stone-200"
                          />
                        </div>
                        <div>
                          <label className="text-[10px] font-medium text-stone-400">Odds</label>
                          <input
                            type="number"
                            step="0.01"
                            min="1.01"
                            value={leg.odds}
                            onChange={(e) =>
                              handleUpdateLeg(leg.id, "odds", parseFloat(e.target.value) || 1.01)
                            }
                            className="w-full rounded-lg border border-stone-200 bg-white px-2 py-1 font-mono text-xs dark:border-stone-700 dark:bg-stone-900 dark:text-stone-200"
                          />
                        </div>
                        <div>
                          <label className="text-[10px] font-medium text-stone-400">True Prob (0-1)</label>
                          <input
                            type="number"
                            step="0.01"
                            min="0.01"
                            max="0.99"
                            value={leg.prob}
                            onChange={(e) =>
                              handleUpdateLeg(leg.id, "prob", parseFloat(e.target.value) || 0.5)
                            }
                            className="w-full rounded-lg border border-stone-200 bg-white px-2 py-1 font-mono text-xs dark:border-stone-700 dark:bg-stone-900 dark:text-stone-200"
                          />
                        </div>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Combined Parlay HUD */}
                <div className="rounded-2xl border border-orange-200 bg-orange-50/60 p-4 dark:border-orange-900/50 dark:bg-orange-950/20">
                  <div className="flex items-center justify-between border-b border-orange-200/50 pb-2 dark:border-orange-900/40">
                    <span className="text-xs font-bold text-orange-900 dark:text-orange-200">
                      Combined Parlay Telemetry
                    </span>
                    <span
                      className={`font-mono text-xs font-bold ${
                        parlayEV > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600"
                      }`}
                    >
                      {parlayEV > 0 ? "+" : ""}
                      {(parlayEV * 100).toFixed(1)}% EV
                    </span>
                  </div>

                  <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
                    <div>
                      <span className="text-stone-500 dark:text-stone-400">Total Parlay Odds:</span>
                      <p className="font-mono text-lg font-black text-stone-900 dark:text-stone-100">
                        {parlayCombinedOdds.toFixed(3)}
                      </p>
                    </div>
                    <div>
                      <span className="text-stone-500 dark:text-stone-400">Joint True Probability:</span>
                      <p className="font-mono text-lg font-black text-stone-900 dark:text-stone-100">
                        {(parlayJointProb * 100).toFixed(1)}%
                      </p>
                    </div>
                  </div>

                  <div className="mt-2 text-[11px] text-stone-500 dark:text-stone-400">
                    KUMBHA sizes Kelly stake dynamically against your real vault balance.
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={isLoading || parlayLegs.length < 2}
                  className="w-full rounded-2xl bg-orange-600 py-3 text-xs font-black tracking-wider text-white uppercase shadow-md transition hover:bg-orange-500 active:scale-98 disabled:opacity-50"
                >
                  {isLoading
                    ? "Assembling Parlay Ticket..."
                    : `🎯 Build Assisted Parimatch Parlay Slip (${parlayCombinedOdds.toFixed(2)} Odds)`}
                </button>
              </form>
            ) : (
              /* SINGLE BET BUILDER MODE */
              <form onSubmit={handleCreateSingleTicket} className="mt-4 space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                    Sport
                    <select
                      value={sport}
                      onChange={(e) => setSport(e.target.value)}
                      className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                    >
                      <option value="soccer">Soccer</option>
                      <option value="cricket">Cricket (IPL)</option>
                      <option value="basketball">Basketball (NBA)</option>
                      <option value="tennis">Tennis</option>
                      <option value="ice_hockey">Ice Hockey (NHL)</option>
                      <option value="american_football">American Football (NFL)</option>
                      <option value="combat">Combat (MMA/UFC)</option>
                    </select>
                  </label>
                  <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                    League / Event
                    <input
                      type="text"
                      value={league}
                      onChange={(e) => setLeague(e.target.value)}
                      className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                    />
                  </label>
                </div>

                <div>
                  <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                    Match Name
                    <input
                      type="text"
                      value={eventName}
                      onChange={(e) => setEventName(e.target.value)}
                      className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                    />
                  </label>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                    Market
                    <input
                      type="text"
                      value={marketType}
                      onChange={(e) => setMarketType(e.target.value)}
                      className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                    />
                  </label>
                  <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                    Selection
                    <input
                      type="text"
                      value={selection}
                      onChange={(e) => setSelection(e.target.value)}
                      className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                    />
                  </label>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                    Target Odds
                    <input
                      type="number"
                      step="0.01"
                      min="1.01"
                      value={targetOdds}
                      onChange={(e) => setTargetOdds(e.target.value)}
                      className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 font-mono text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                    />
                  </label>
                  <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                    Model Probability (0-1)
                    <input
                      type="number"
                      step="0.01"
                      min="0.01"
                      max="0.99"
                      value={trueProb}
                      onChange={(e) => setTrueProb(e.target.value)}
                      className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 font-mono text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                    />
                  </label>
                </div>

                <button
                  type="submit"
                  disabled={isLoading}
                  className="mt-2 w-full rounded-xl bg-orange-600 py-2.5 text-xs font-bold text-white shadow-sm transition hover:bg-orange-500 active:scale-95 disabled:opacity-50"
                >
                  {isLoading ? "Generating Slip..." : "🎯 Generate Single Slip"}
                </button>
              </form>
            )}
          </div>

          <div className="mt-4 border-t border-stone-100 pt-3 text-[11px] text-stone-400 dark:border-stone-800">
            <span>Nothing is automated: You place the slip on Parimatch and confirm it here.</span>
          </div>
        </div>

        {/* Active Tickets List */}
        <div className="lg:col-span-6">
          <RealActiveTickets />
        </div>
      </div>

      {/* History & Cash Ledger Table */}
      <RealHistoryTable />

      {/* Modals */}
      <ParimatchExecutionModal
        ticket={activeTicket}
        isOpen={isExecutionOpen}
        onClose={closeExecution}
      />
      <TaxCertificateModal />
    </div>
  );
};
