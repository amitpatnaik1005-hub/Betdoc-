/**
 * Real Bankroll & Shivaji Risk Shield Card (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Displays TODAR MAL's live INR cash ledger, locked exposure, Section 115BBJ 30% TDS,
 * and SHIVAJI's daily stop-loss (15%) and 3-loss cool-down (30m) telemetry.
 */

import { useState } from "react";
import { useRealBankroll } from "../../lib/real_money";
import { useRealMoneyStore } from "../../store/useRealMoneyStore";
import { formatINR, formatLakhCrore } from "../../utils/formatCurrency";

export const RealBankrollCard = () => {
  const bankrollResource = useRealBankroll(true);
  const data = bankrollResource.data;

  const openDepositModal = useRealMoneyStore((s) => s.openDepositModal);
  const openWithdrawModal = useRealMoneyStore((s) => s.openWithdrawModal);
  const openTaxCertModal = useRealMoneyStore((s) => s.openTaxCertModal);

  const [depositAmount, setDepositAmount] = useState<string>("5000");
  const [depositRef, setDepositRef] = useState<string>("UPI/IMPS");
  const [withdrawAmount, setWithdrawAmount] = useState<string>("2000");

  const depositAction = useRealMoneyStore((s) => s.deposit);
  const withdrawAction = useRealMoneyStore((s) => s.withdraw);
  const isLoading = useRealMoneyStore((s) => s.isLoading);

  const isDepositOpen = useRealMoneyStore((s) => s.isDepositModalOpen);
  const closeDeposit = useRealMoneyStore((s) => s.closeDepositModal);
  const isWithdrawOpen = useRealMoneyStore((s) => s.isWithdrawModalOpen);
  const closeWithdraw = useRealMoneyStore((s) => s.closeWithdrawModal);

  if (!data) {
    return (
      <div className="flex h-48 w-full animate-pulse items-center justify-center rounded-2xl bg-stone-900/50 p-6 text-stone-400">
        <span>Loading TODAR MAL's live cash vault...</span>
      </div>
    );
  }

  const drawdownPct = Math.min(100, Math.max(0, data.daily_drawdown_pct));
  const maxDrawdownPct = data.daily_drawdown_limit_pct;
  const isHalted = data.stop_loss_triggered;
  const inCooldown = data.is_in_cooldown;

  return (
    <div className="flex flex-col gap-6 rounded-3xl border border-stone-200 bg-white p-6 shadow-sm dark:border-stone-800 dark:bg-stone-900/90 sm:p-8">
      {/* Top Banner / Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-stone-100 pb-5 dark:border-stone-800/80">
        <div className="flex items-center gap-3">
          <div className="flex size-11 items-center justify-center rounded-2xl bg-amber-500/10 text-amber-600 dark:bg-amber-400/15 dark:text-amber-300">
            <span className="font-mono text-xl font-bold">₹</span>
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-bold tracking-tight text-stone-900 dark:text-stone-100">
                TODAR MAL'S REAL CASH VAULT
              </h2>
              <span className="rounded-md bg-rose-500/10 px-2 py-0.5 text-[10px] font-bold tracking-wider text-rose-600 uppercase dark:bg-rose-500/20 dark:text-rose-400">
                Live Money
              </span>
            </div>
            <p className="text-xs text-stone-500 dark:text-stone-400">
              Primary INR Account | Exact Double-Entry Ledger | Section 115BBJ Compliant
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={openDepositModal}
            className="inline-flex items-center gap-1.5 rounded-xl bg-emerald-600 px-4 py-2 text-xs font-semibold text-white shadow-sm transition hover:bg-emerald-500 active:scale-95"
          >
            <span>+ Deposit</span>
          </button>
          <button
            type="button"
            onClick={openWithdrawModal}
            className="inline-flex items-center gap-1.5 rounded-xl border border-stone-200 bg-white px-4 py-2 text-xs font-semibold text-stone-700 shadow-sm transition hover:bg-stone-50 active:scale-95 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200 dark:hover:bg-stone-700"
          >
            <span>Withdraw</span>
          </button>
          <button
            type="button"
            onClick={openTaxCertModal}
            className="inline-flex items-center gap-1.5 rounded-xl border border-amber-300/40 bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-800 shadow-sm transition hover:bg-amber-100 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300 dark:hover:bg-amber-500/20"
            title="Download / View Section 115BBJ 30% TDS Certificate"
          >
            <span>📜 Tax Cert (30% TDS)</span>
          </button>
        </div>
      </div>

      {/* SHIVAJI Circuit Breaker Alert Banner (if triggered or in cooldown) */}
      {isHalted && (
        <div className="flex items-center gap-3 rounded-2xl border border-rose-300/60 bg-rose-50 p-4 text-rose-800 dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-200">
          <span className="text-2xl">🛡️</span>
          <div>
            <h4 className="text-sm font-bold">SHIVAJI 15% Daily Stop-Loss Latch Active</h4>
            <p className="text-xs opacity-90">
              Real-money execution halted until 00:00 IST to protect your capital. Daily loss reached{" "}
              {formatINR(data.daily_realized_loss)} ({data.daily_drawdown_pct.toFixed(1)}%).
            </p>
          </div>
        </div>
      )}

      {inCooldown && !data.stop_loss_triggered && (
        <div className="flex items-center gap-3 rounded-2xl border border-amber-300/60 bg-amber-50 p-4 text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
          <span className="text-2xl">⏳</span>
          <div>
            <h4 className="text-sm font-bold">SHIVAJI 30-Minute Cool-Down Active</h4>
            <p className="text-xs opacity-90">
              3 consecutive losses triggered a safety pause. Cool-down remaining:{" "}
              <span className="font-mono font-bold">
                {Math.ceil(data.cooldown_seconds_remaining / 60)} minutes
              </span>
              .
            </p>
          </div>
        </div>
      )}

      {/* Vault Balance Cards Grid */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {/* Liquid Cash */}
        <div className="flex flex-col gap-1 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/60">
          <span className="text-[11px] font-medium tracking-wider text-stone-500 uppercase dark:text-stone-400">
            Liquid Cash
          </span>
          <span className="font-mono text-2xl font-bold tracking-tight text-emerald-600 dark:text-emerald-400">
            {formatINR(data.cash_balance)}
          </span>
          <span className="text-[11px] text-stone-400">Ready for assisted execution</span>
        </div>

        {/* Locked Exposure */}
        <div className="flex flex-col gap-1 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/60">
          <span className="text-[11px] font-medium tracking-wider text-stone-500 uppercase dark:text-stone-400">
            Locked Exposure
          </span>
          <span className="font-mono text-2xl font-bold tracking-tight text-amber-600 dark:text-amber-400">
            {formatINR(data.locked_exposure)}
          </span>
          <span className="text-[11px] text-stone-400">Active open wagers on book</span>
        </div>

        {/* Total Bankroll Equity */}
        <div className="flex flex-col gap-1 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/60">
          <span className="text-[11px] font-medium tracking-wider text-stone-500 uppercase dark:text-stone-400">
            Total Bankroll
          </span>
          <span className="font-mono text-2xl font-bold tracking-tight text-stone-900 dark:text-stone-100">
            {formatINR(data.total_bankroll)}
          </span>
          <span className="text-[11px] text-stone-400">HWM: {formatLakhCrore(data.high_water_mark)}</span>
        </div>

        {/* Realized P&L */}
        <div className="flex flex-col gap-1 rounded-2xl bg-stone-50 p-4 dark:bg-stone-800/60">
          <span className="text-[11px] font-medium tracking-wider text-stone-500 uppercase dark:text-stone-400">
            Realized Net P&L
          </span>
          <span
            className={`font-mono text-2xl font-bold tracking-tight ${
              data.realized_pnl >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"
            }`}
          >
            {data.realized_pnl >= 0 ? "+" : ""}
            {formatINR(data.realized_pnl)}
          </span>
          <span className="text-[11px] text-stone-400">Gross: {formatINR(data.gross_winnings)}</span>
        </div>
      </div>

      {/* Shivaji Risk Telemetry & Tax Bar */}
      <div className="grid grid-cols-1 gap-4 rounded-2xl border border-stone-200/70 bg-stone-50/70 p-4 dark:border-stone-800 dark:bg-stone-800/40 md:grid-cols-3">
        {/* 15% Daily Drawdown Progress */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between text-xs">
            <span className="font-semibold text-stone-700 dark:text-stone-300">
              SHIVAJI Daily Stop-Loss (15%)
            </span>
            <span className="font-mono font-bold text-stone-900 dark:text-stone-100">
              {drawdownPct.toFixed(1)}% / {maxDrawdownPct}%
            </span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-stone-200 dark:bg-stone-700">
            <div
              className={`h-full transition-all duration-500 ${
                drawdownPct > 12 ? "bg-rose-500" : drawdownPct > 8 ? "bg-amber-500" : "bg-emerald-500"
              }`}
              style={{ width: `${Math.min(100, (drawdownPct / maxDrawdownPct) * 100)}%` }}
            />
          </div>
          <span className="text-[10px] text-stone-500 dark:text-stone-400">
            Loss today: {formatINR(data.daily_realized_loss)} (Base: {formatINR(data.daily_starting_balance)})
          </span>
        </div>

        {/* Consecutive Losses Gauge */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between text-xs">
            <span className="font-semibold text-stone-700 dark:text-stone-300">
              Loss Streak (3-Limit Pause)
            </span>
            <span className="font-mono font-bold text-stone-900 dark:text-stone-100">
              {data.consecutive_losses} / {data.loss_streak_limit}
            </span>
          </div>
          <div className="flex gap-1.5">
            {[0, 1, 2].map((idx) => (
              <div
                key={idx}
                className={`h-2 flex-1 rounded-full ${
                  idx < data.consecutive_losses
                    ? "bg-rose-500 dark:bg-rose-400"
                    : "bg-stone-200 dark:bg-stone-700"
                }`}
              />
            ))}
          </div>
          <span className="text-[10px] text-stone-500 dark:text-stone-400">
            Max single stake: {formatINR(data.max_single_stake)} (5% cap)
          </span>
        </div>

        {/* Section 115BBJ 30% TDS Total */}
        <div className="flex flex-col justify-between gap-1">
          <div className="flex items-center justify-between text-xs">
            <span className="font-semibold text-stone-700 dark:text-stone-300">
              Sec 115BBJ TDS Withheld
            </span>
            <span className="rounded bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-bold text-rose-600 dark:bg-rose-500/20 dark:text-rose-400">
              Flat 30%
            </span>
          </div>
          <span className="font-mono text-base font-bold text-stone-900 dark:text-stone-100">
            {formatINR(data.total_tds_deducted)}
          </span>
          <span className="text-[10px] text-stone-500 dark:text-stone-400">
            Deducted at settlement from net winnings
          </span>
        </div>
      </div>

      {/* Deposit Modal */}
      {isDepositOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-md rounded-3xl border border-stone-200 bg-white p-6 shadow-2xl dark:border-stone-800 dark:bg-stone-900">
            <h3 className="text-base font-bold text-stone-900 dark:text-stone-100">Record Real Cash Deposit</h3>
            <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
              Credits your TODAR MAL liquid vault in INR for assisted execution on Parimatch.
            </p>
            <div className="mt-4 flex flex-col gap-3">
              <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                Amount (₹ INR)
                <input
                  type="number"
                  min="1"
                  step="100"
                  value={depositAmount}
                  onChange={(e) => setDepositAmount(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2.5 font-mono text-sm dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
                />
              </label>
              <div className="flex gap-2">
                {[1000, 5000, 10000, 25000].map((amt) => (
                  <button
                    key={amt}
                    type="button"
                    onClick={() => setDepositAmount(String(amt))}
                    className="flex-1 rounded-lg border border-stone-200 py-1 font-mono text-xs dark:border-stone-700 dark:text-stone-300"
                  >
                    +{amt}
                  </button>
                ))}
              </div>
              <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                Reference / Channel
                <input
                  type="text"
                  value={depositRef}
                  onChange={(e) => setDepositRef(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2.5 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
                />
              </label>
            </div>
            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                onClick={closeDeposit}
                className="rounded-xl px-4 py-2 text-xs font-semibold text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-800"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={isLoading || parseFloat(depositAmount) <= 0}
                onClick={() => depositAction(parseFloat(depositAmount), depositRef)}
                className="rounded-xl bg-emerald-600 px-5 py-2 text-xs font-bold text-white shadow-sm hover:bg-emerald-500 disabled:opacity-50"
              >
                {isLoading ? "Recording..." : "Confirm Deposit"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Withdraw Modal */}
      {isWithdrawOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs">
          <div className="w-full max-w-md rounded-3xl border border-stone-200 bg-white p-6 shadow-2xl dark:border-stone-800 dark:bg-stone-900">
            <h3 className="text-base font-bold text-stone-900 dark:text-stone-100">Record Real Cash Withdrawal</h3>
            <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
              Withdraws unencumbered liquid cash. Available: {formatINR(data.cash_balance)}.
            </p>
            <div className="mt-4 flex flex-col gap-3">
              <label className="text-xs font-semibold text-stone-700 dark:text-stone-300">
                Amount (₹ INR)
                <input
                  type="number"
                  min="1"
                  max={data.cash_balance}
                  step="100"
                  value={withdrawAmount}
                  onChange={(e) => setWithdrawAmount(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-stone-200 bg-stone-50 p-2.5 font-mono text-sm dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
                />
              </label>
            </div>
            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                onClick={closeWithdraw}
                className="rounded-xl px-4 py-2 text-xs font-semibold text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-800"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={isLoading || parseFloat(withdrawAmount) <= 0 || parseFloat(withdrawAmount) > data.cash_balance}
                onClick={() => withdrawAction(parseFloat(withdrawAmount))}
                className="rounded-xl bg-rose-600 px-5 py-2 text-xs font-bold text-white shadow-sm hover:bg-rose-500 disabled:opacity-50"
              >
                {isLoading ? "Recording..." : "Confirm Withdrawal"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
