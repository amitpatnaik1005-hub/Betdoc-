/**
 * Live Real-Money Trading Desk (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Primary execution router for Parimatch (INR) & 1xBet.
 * Commander ASHVATTHAMA (#17 - Execution Router) & Commander TODAR MAL (#8 - Cash Vault).
 * Commander SHIVAJI (#5 - 15% Daily Drawdown Circuit Breaker & 3-Loss Cool-down Shield).
 */

import { useState } from "react";
import { ParimatchExecutionModal } from "../components/real_money/ParimatchExecutionModal";
import { RealActiveTickets } from "../components/real_money/RealActiveTickets";
import { RealBankrollCard } from "../components/real_money/RealBankrollCard";
import { RealHistoryTable } from "../components/real_money/RealHistoryTable";
import { TaxCertificateModal } from "../components/real_money/TaxCertificateModal";
import { useRealMoneyStore } from "../store/useRealMoneyStore";

export const LiveDesk = () => {
  const activeTicket = useRealMoneyStore((s) => s.activeTicket);
  const isExecutionOpen = useRealMoneyStore((s) => s.isExecutionModalOpen);
  const closeExecution = useRealMoneyStore((s) => s.closeExecutionModal);
  const createAndOpenTicket = useRealMoneyStore((s) => s.createAndOpenTicket);
  const isLoading = useRealMoneyStore((s) => s.isLoading);

  // Quick ticket builder form state
  const [sport, setSport] = useState("soccer");
  const [league, setLeague] = useState("Premier League");
  const [eventName, setEventName] = useState("Arsenal vs Chelsea");
  const [marketType, setMarketType] = useState("Match Odds");
  const [selection, setSelection] = useState("Arsenal");
  const [targetOdds, setTargetOdds] = useState("2.10");
  const [trueProb, setTrueProb] = useState("0.52");
  const [bookmaker, setBookmaker] = useState("parimatch");

  const handleCreateQuickTicket = async (e: React.FormEvent) => {
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

      {/* Grid: Quick Slip Builder + Active Tickets */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Quick Ticket Builder */}
        <div className="lg:col-span-5 flex flex-col justify-between rounded-3xl border border-stone-200 bg-white p-6 shadow-sm dark:border-stone-800 dark:bg-stone-900/90">
          <div>
            <div className="flex items-center gap-2 border-b border-stone-100 pb-3 dark:border-stone-800">
              <span className="text-lg">⚡</span>
              <div>
                <h3 className="text-sm font-bold text-stone-900 dark:text-stone-100">
                  Build Assisted Parimatch Slip
                </h3>
                <p className="text-[11px] text-stone-400">
                  KUMBHA sizes stake via fractional Kelly; ASHVATTHAMA sets the minimum odds floor.
                </p>
              </div>
            </div>

            <form onSubmit={handleCreateQuickTicket} className="mt-4 space-y-3">
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
                  Bookmaker
                  <select
                    value={bookmaker}
                    onChange={(e) => setBookmaker(e.target.value)}
                    className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
                  >
                    <option value="parimatch">Parimatch (Primary INR)</option>
                    <option value="onexbet">1xBet (Secondary)</option>
                  </select>
                </label>
              </div>

              <div>
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
                {isLoading ? "Generating Slip..." : "🎯 Generate Execution Slip"}
              </button>
            </form>
          </div>

          <div className="mt-4 border-t border-stone-100 pt-3 text-[11px] text-stone-400 dark:border-stone-800">
            <span>Nothing is automated: You place the slip on Parimatch and confirm it here.</span>
          </div>
        </div>

        {/* Active Tickets List */}
        <div className="lg:col-span-7">
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
