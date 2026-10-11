/**
 * The strategic pivot of Group 80: cross-bookmaker arbitrage is on hold.
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * Every arbitrage surface shows this banner instead of a live scanner while the fleet's compute goes to Ashoka's
 * highest-conviction value bets, Parimatch execution and the Paper Trading Sandbox's forward-test.
 */
import { Link } from "react-router-dom";
import { Pill } from "../../ui/kit";

export const ARBITRAGE_COMING_SOON = "Arbitrage Matrix: Coming Soon / Advanced Multi-Bookmaker Engine Under Optimization";

export const ArbitrageComingSoon = ({ detail, compact = false }: { detail?: string; compact?: boolean }) => (
  <div
    role="status"
    className={`flex flex-col items-center gap-3 rounded-3xl border border-dashed border-amber-500/40 bg-amber-500/[0.06] text-center ${compact ? "p-6" : "p-8"}`}
  >
    <span className="material-symbols-outlined text-[28px] text-amber-600 dark:text-amber-300" aria-hidden>hourglass_top</span>
    <Pill tone="warning" icon="construction">Coming Soon · Under Optimization</Pill>
    <h3 className={`max-w-xl font-display font-bold text-amber-800 dark:text-amber-200 ${compact ? "text-sm" : "text-base"}`}>{ARBITRAGE_COMING_SOON}</h3>
    <p className="max-w-md text-xs leading-relaxed text-stone-500 dark:text-stone-400">
      {detail ??
        "Cross-bookmaker arbitrage computations are paused to focus full algorithmic power on Ashoka's value picks, Parimatch live execution and Paper Trading forward-testing."}
    </p>
    <Link
      to="/sandbox"
      className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-3 py-1 text-[11px] font-semibold text-emerald-700 transition-colors hover:bg-emerald-500/15 dark:text-emerald-300"
    >
      <span className="material-symbols-outlined text-[14px]" aria-hidden>experiment</span>
      Paper Trading active (₹1,00,000 virtual capital)
    </Link>
  </div>
);
