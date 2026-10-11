/**
 * Real-Money Settled Bets & Double-Entry Cash Ledger (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Full audit trail for settlements, Section 115BBJ 30% TDS, and cash movements.
 */

import { useState } from "react";
import {
  useRealCashTransactions,
  useRealTicketHistory,
  type RealBetTicket,
  type RealCashTransaction,
} from "../../lib/real_money";
import { formatINR } from "../../utils/formatCurrency";

export const RealHistoryTable = () => {
  const [tab, setTab] = useState<"settled" | "ledger">("settled");
  const historyResource = useRealTicketHistory(true, 50);
  const ledgerResource = useRealCashTransactions(true, 50);

  const settledTickets = (historyResource.data ?? []).filter(
    (t: RealBetTicket) => t.status !== "PENDING_PLACEMENT" && t.status !== "PLACED"
  );
  const transactions = ledgerResource.data ?? [];

  return (
    <div className="flex flex-col gap-4 rounded-3xl border border-stone-200 bg-white p-6 shadow-sm dark:border-stone-800 dark:bg-stone-900/90 sm:p-8">
      {/* Header & Tabs */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-stone-100 pb-4 dark:border-stone-800">
        <div>
          <h3 className="text-base font-bold text-stone-900 dark:text-stone-100">Audit Trail & History</h3>
          <p className="text-xs text-stone-500 dark:text-stone-400">
            Immutable settlement ledger and double-entry cash movements in INR.
          </p>
        </div>
        <div className="flex rounded-xl bg-stone-100 p-1 dark:bg-stone-800">
          <button
            type="button"
            onClick={() => setTab("settled")}
            className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
              tab === "settled"
                ? "bg-white text-stone-900 shadow-xs dark:bg-stone-700 dark:text-stone-100"
                : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200"
            }`}
          >
            Settled Wagers ({settledTickets.length})
          </button>
          <button
            type="button"
            onClick={() => setTab("ledger")}
            className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
              tab === "ledger"
                ? "bg-white text-stone-900 shadow-xs dark:bg-stone-700 dark:text-stone-100"
                : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200"
            }`}
          >
            Cash Ledger ({transactions.length})
          </button>
        </div>
      </div>

      {/* Tab 1: Settled Tickets */}
      {tab === "settled" && (
        <div className="overflow-x-auto">
          {settledTickets.length === 0 ? (
            <div className="py-12 text-center text-xs text-stone-400">
              No settled real-money tickets yet.
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-stone-100 text-stone-400 dark:border-stone-800">
                  <th className="py-2.5 pr-3 font-semibold">Ref / Date</th>
                  <th className="py-2.5 px-3 font-semibold">Event & Market</th>
                  <th className="py-2.5 px-3 font-semibold">Book</th>
                  <th className="py-2.5 px-3 font-semibold">Odds</th>
                  <th className="py-2.5 px-3 font-semibold">Stake</th>
                  <th className="py-2.5 px-3 font-semibold">Gross Return</th>
                  <th className="py-2.5 px-3 font-semibold">30% TDS</th>
                  <th className="py-2.5 px-3 font-semibold">Net P&L</th>
                  <th className="py-2.5 pl-3 font-semibold">Outcome</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-100 dark:divide-stone-800/60 font-mono">
                {settledTickets.map((t) => {
                  const isWon = t.status === "WON" || t.status === "HALF_WON";
                  const isLost = t.status === "LOST" || t.status === "HALF_LOST";
                  return (
                    <tr key={t.id} className="hover:bg-stone-50/60 dark:hover:bg-stone-800/40">
                      <td className="py-3 pr-3 text-stone-500">
                        <span className="font-bold text-stone-800 dark:text-stone-200">{t.ticket_reference}</span>
                        <span className="block text-[10px] text-stone-400">
                          {t.settled_at ? new Date(t.settled_at).toLocaleDateString() : "N/A"}
                        </span>
                      </td>
                      <td className="py-3 px-3 font-sans">
                        <span className="font-medium text-stone-900 dark:text-stone-100">{t.event_name}</span>
                        <span className="block text-[11px] text-stone-500">
                          {t.market_type} · <span className="font-semibold text-orange-600 dark:text-orange-400">{t.selection}</span>
                        </span>
                      </td>
                      <td className="py-3 px-3 uppercase text-[11px] text-stone-500">{t.bookmaker}</td>
                      <td className="py-3 px-3 font-bold">{t.placed_odds?.toFixed(2) ?? t.target_odds.toFixed(2)}</td>
                      <td className="py-3 px-3">{formatINR(t.placed_stake)}</td>
                      <td className="py-3 px-3 text-stone-700 dark:text-stone-300">{formatINR(t.gross_return)}</td>
                      <td className="py-3 px-3 text-rose-600 dark:text-rose-400">
                        {t.tds_deducted > 0 ? `-${formatINR(t.tds_deducted)}` : "₹0.00"}
                      </td>
                      <td className={`py-3 px-3 font-bold ${isWon ? "text-emerald-600 dark:text-emerald-400" : isLost ? "text-rose-600 dark:text-rose-400" : "text-stone-500"}`}>
                        {t.net_profit >= 0 ? "+" : ""}{formatINR(t.net_profit)}
                      </td>
                      <td className="py-3 pl-3">
                        <span
                          className={`rounded-md px-2 py-0.5 text-[10px] font-bold ${
                            isWon
                              ? "bg-emerald-500/15 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-300"
                              : isLost
                              ? "bg-rose-500/15 text-rose-700 dark:bg-rose-500/20 dark:text-rose-300"
                              : "bg-stone-500/15 text-stone-700 dark:bg-stone-500/20 dark:text-stone-300"
                          }`}
                        >
                          {t.status}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Tab 2: Double-Entry Cash Ledger */}
      {tab === "ledger" && (
        <div className="overflow-x-auto">
          {transactions.length === 0 ? (
            <div className="py-12 text-center text-xs text-stone-400">
              No cash transactions recorded yet.
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-stone-100 text-stone-400 dark:border-stone-800">
                  <th className="py-2.5 pr-3 font-semibold">Time</th>
                  <th className="py-2.5 px-3 font-semibold">Type</th>
                  <th className="py-2.5 px-3 font-semibold">Amount</th>
                  <th className="py-2.5 px-3 font-semibold">Balance Before</th>
                  <th className="py-2.5 px-3 font-semibold">Balance After</th>
                  <th className="py-2.5 pl-3 font-semibold">Description</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-100 dark:divide-stone-800/60 font-mono">
                {transactions.map((tx: RealCashTransaction) => {
                  const isCredit =
                    tx.transaction_type === "DEPOSIT" ||
                    tx.transaction_type === "BET_PAYOUT_CREDIT" ||
                    tx.transaction_type === "BET_REFUND";
                  return (
                    <tr key={tx.id} className="hover:bg-stone-50/60 dark:hover:bg-stone-800/40">
                      <td className="py-3 pr-3 text-stone-400">
                        {new Date(tx.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                      </td>
                      <td className="py-3 px-3">
                        <span className="rounded-md bg-stone-100 px-2 py-0.5 text-[10px] font-bold text-stone-700 dark:bg-stone-800 dark:text-stone-300">
                          {tx.transaction_type}
                        </span>
                      </td>
                      <td className={`py-3 px-3 font-bold ${isCredit ? "text-emerald-600 dark:text-emerald-400" : "text-stone-700 dark:text-stone-300"}`}>
                        {isCredit ? "+" : ""}{formatINR(tx.amount)}
                      </td>
                      <td className="py-3 px-3 text-stone-500">{formatINR(tx.balance_before)}</td>
                      <td className="py-3 px-3 font-bold text-stone-900 dark:text-stone-100">{formatINR(tx.balance_after)}</td>
                      <td className="py-3 pl-3 font-sans text-stone-600 dark:text-stone-400 max-w-xs truncate">
                        {tx.description}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
};
