/**
 * Global Trading Mode Toggle: Paper Trading (Sandbox) vs Live Real Money (Group 80).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * PAPER is the default. Going LIVE while the forward-test has not yet proven a statistically significant edge is
 * KUMBHA's sandbox firewall: the first click arms the switch and says why, a second click within 4 seconds confirms.
 * LIVE mode never places a bet by itself: ASHVATTHAMA issues the Parimatch ticket and the trader places it by hand.
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { usePaperAccount, usePaperPerformance } from "../../lib/paper_trading";
import { MOCK_AUTH } from "../../store/useAuthStore";
import { usePaperTradingStore } from "../../store/usePaperTradingStore";
import { toast } from "../../store/useToastStore";
import { formatLakhCrore } from "../../utils/formatCurrency";

const pill =
  "inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[11px] font-semibold tracking-wide transition-[background-color,color,transform] duration-300 active:scale-[0.97]";

export const TradingModeToggle = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const isPaperMode = usePaperTradingStore((s) => s.isPaperMode);
  const setPaperMode = usePaperTradingStore((s) => s.setPaperMode);
  const account = usePaperAccount(!MOCK_AUTH);
  const performance = usePaperPerformance(!MOCK_AUTH);
  const [armed, setArmed] = useState(false);
  const liveReady = performance.data?.live_ready ?? false;

  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), 4_000);
    return () => window.clearTimeout(id);
  }, [armed]);

  const goLive = () => {
    if (!isPaperMode) {
      if (location.pathname !== "/live-desk") {
        navigate("/live-desk");
      }
      return;
    }
    if (!liveReady && !armed) {
      setArmed(true);
      toast.warning(
        "KUMBHA sandbox firewall",
        `${performance.data?.status_summary ?? "The forward-test has no verdict yet"}: the paper record has not proven a significant edge. Click again to go live anyway.`,
      );
      return;
    }
    setArmed(false);
    setPaperMode(false);
    navigate("/live-desk");
    toast.info("Live real money", "ASHVATTHAMA will issue Parimatch tickets for you to place by hand. Nothing is placed automatically.");
  };

  const goPaper = () => {
    setPaperMode(true);
    if (location.pathname === "/live-desk") {
      navigate("/sandbox");
    }
  };

  return (
    <div role="radiogroup" aria-label="Trading mode" className="inline-flex items-center gap-0.5 rounded-full bg-stone-100 p-1 dark:bg-stone-800">
      <button
        type="button"
        role="radio"
        aria-checked={isPaperMode}
        onClick={goPaper}
        title="Forward-test with virtual rupees at real-time odds"
        className={`${pill} ${isPaperMode ? "bg-emerald-600 text-white shadow-sm dark:bg-emerald-500/90" : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200"}`}
      >
        <span className={`size-1.5 rounded-full ${isPaperMode ? "animate-pulse bg-emerald-200" : "bg-emerald-500/60"}`} aria-hidden />
        [ 🧪 PAPER TRADING (SANDBOX) ]
        {account.data && (
          <span className="rounded bg-black/10 px-1.5 py-0.5 font-mono text-[10px] dark:bg-white/10">{formatLakhCrore(account.data.virtual_balance)}</span>
        )}
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={!isPaperMode}
        onClick={goLive}
        title={liveReady ? "The paper record is statistically significant" : "The paper record has not proven an edge yet"}
        className={`${pill} ${!isPaperMode ? "bg-rose-600 text-white shadow-sm dark:bg-rose-500/90" : armed ? "animate-breathe bg-amber-100 text-amber-800 dark:bg-amber-400/15 dark:text-amber-200" : "text-stone-500 hover:text-stone-800 dark:text-stone-400 dark:hover:text-stone-200"}`}
      >
        <span className="size-1.5 rounded-full bg-rose-400" aria-hidden />
        {armed ? "Confirm: go live without proof?" : "[ 🔴 LIVE REAL MONEY ]"}
      </button>
    </div>
  );
};
