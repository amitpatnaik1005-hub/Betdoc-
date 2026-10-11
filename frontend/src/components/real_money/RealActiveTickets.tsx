/**
 * Active Real-Money Bet Tickets Monitor (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Displays pending-placement slips and locked open wagers on Parimatch / 1xBet.
 */

import { useActiveRealTickets, type RealBetTicket } from "../../lib/real_money";
import { useRealMoneyStore } from "../../store/useRealMoneyStore";
import { formatINR } from "../../utils/formatCurrency";

export const RealActiveTickets = () => {
  const activeResource = useActiveRealTickets(true);
  const tickets = activeResource.data ?? [];
  const openExecutionModal = useRealMoneyStore((s) => s.openExecutionModal);

  if (activeResource.loading && tickets.length === 0) {
    return (
      <div className="flex h-36 items-center justify-center rounded-2xl bg-stone-50 p-6 text-xs text-stone-400 dark:bg-stone-800/40">
        <span>Loading active execution tickets...</span>
      </div>
    );
  }

  if (tickets.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-3xl border border-dashed border-stone-200 p-8 text-center dark:border-stone-800">
        <span className="text-3xl">🎫</span>
        <h4 className="mt-2 text-sm font-bold text-stone-800 dark:text-stone-200">No Active Real Tickets</h4>
        <p className="mt-1 max-w-sm text-xs text-stone-500 dark:text-stone-400">
          When ASHVATTHAMA generates value execution slips or you issue a ticket, they will appear here with live
          monitoring and Parimatch deep links.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between px-1">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-bold text-stone-900 dark:text-stone-100">Active Real Wagers & Tickets</h3>
          <span className="rounded-full bg-orange-500/10 px-2 py-0.5 font-mono text-[11px] font-bold text-orange-600 dark:text-orange-400">
            {tickets.length}
          </span>
        </div>
        <span className="text-[11px] text-stone-400">Locked on Parimatch/1xBet</span>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {tickets.map((t: RealBetTicket) => {
          const isPlaced = t.status === "PLACED";
          return (
            <div
              key={t.id}
              onClick={() => openExecutionModal(t)}
              className="group flex cursor-pointer flex-col justify-between gap-3 rounded-2xl border border-stone-200 bg-white p-4 shadow-xs transition hover:border-orange-500/50 hover:shadow-md dark:border-stone-800 dark:bg-stone-900/80"
            >
              <div>
                {/* Header row: sport, bookmaker, status badge */}
                <div className="flex items-center justify-between text-[11px]">
                  <span className="font-semibold text-stone-500 uppercase dark:text-stone-400">
                    {t.sport} · {t.league}
                  </span>
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono text-[10px] font-bold text-stone-600 uppercase dark:text-stone-300">
                      {t.bookmaker}
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${
                        isPlaced
                          ? "bg-amber-500/15 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300"
                          : "bg-blue-500/15 text-blue-700 dark:bg-blue-500/20 dark:text-blue-300"
                      }`}
                    >
                      {t.status}
                    </span>
                  </div>
                </div>

                {/* Match title */}
                <h4 className="mt-1 text-sm font-bold text-stone-900 group-hover:text-orange-600 dark:text-stone-100 dark:group-hover:text-orange-400">
                  {t.event_name}
                </h4>

                {/* Market & Selection */}
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="rounded bg-stone-100 px-2 py-0.5 text-stone-700 dark:bg-stone-800 dark:text-stone-300">
                    {t.market_type}
                  </span>
                  <span className="font-semibold text-stone-800 dark:text-stone-200">
                    Pick: <span className="text-orange-600 dark:text-orange-400">{t.selection}</span>
                  </span>
                </div>
              </div>

              {/* Bottom stats row: odds, stake, action */}
              <div className="flex items-center justify-between border-t border-stone-100 pt-3 dark:border-stone-800">
                <div className="flex items-center gap-3">
                  <div>
                    <span className="block text-[10px] text-stone-400">Odds</span>
                    <span className="font-mono text-sm font-bold text-stone-900 dark:text-stone-100">
                      {t.placed_odds ? t.placed_odds.toFixed(2) : t.target_odds.toFixed(2)}
                    </span>
                  </div>
                  <div>
                    <span className="block text-[10px] text-stone-400">Stake</span>
                    <span className="font-mono text-sm font-bold text-stone-900 dark:text-stone-100">
                      {formatINR(t.placed_stake > 0 ? t.placed_stake : t.recommended_stake)}
                    </span>
                  </div>
                  <div>
                    <span className="block text-[10px] text-stone-400">Floor</span>
                    <span className="font-mono text-xs font-semibold text-amber-600 dark:text-amber-400">
                      {t.minimum_profitable_odds.toFixed(2)}
                    </span>
                  </div>
                </div>

                <button
                  type="button"
                  className="rounded-xl bg-stone-100 px-3 py-1.5 text-xs font-bold text-stone-700 transition group-hover:bg-orange-600 group-hover:text-white dark:bg-stone-800 dark:text-stone-300"
                >
                  {isPlaced ? "Inspect / Settle" : "Execute ➔"}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
