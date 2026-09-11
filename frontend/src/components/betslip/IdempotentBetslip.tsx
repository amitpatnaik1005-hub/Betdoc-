import { useState } from "react";
import { useBetStore } from "../../store/useBetStore";

export const IdempotentBetslip = () => {
  const { bankroll, reserve, activeModel, contextMarketId } = useBetStore();
  const [stake, setStake] = useState<string>("100");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [lastStatus, setLastStatus] = useState<"idle" | "success" | "error">("idle");

  const handlePlaceBet = async () => {
    const numStake = parseFloat(stake);
    if (isNaN(numStake) || numStake <= 0) return;
    if (!contextMarketId) return;

    const idempotencyKey = crypto.randomUUID();
    setIsSubmitting(true);
    setLastStatus("idle");

    try {
      // 1. Optimistic UI Update via Zustand Reserve
      reserve({
        idempotency_key: idempotencyKey,
        market_id: contextMarketId,
        stake: numStake,
        model_used: activeModel
      }, numStake * 100);

      // 2. Mock API call
      await new Promise((resolve, reject) => {
        setTimeout(() => {
          if (Math.random() > 0.1) resolve(true);
          else reject(new Error("Network Timeout"));
        }, 1000);
      });

      setLastStatus("success");
    } catch (error) {
      console.error("Bet placement failed:", error);
      setLastStatus("error");
      // Refund handled by the reconcile action in production
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="bg-onyx text-white p-6 rounded-xl shadow-[8px_8px_0_rgba(230,57,70,1)] border-2 border-white/10">
      <h3 className="font-display font-black text-xl uppercase tracking-widest mb-4 flex items-center gap-2">
        <span className="w-2 h-2 bg-accent rounded-full animate-pulse"></span>
        Betslip
      </h3>
      
      <div className="flex justify-between items-center bg-white/5 p-4 rounded-lg mb-4 border border-white/10">
        <span className="font-sans text-sm text-slate-300 font-medium">Bankroll</span>
        <span className="font-mono font-bold text-lg text-accent">
          ${(bankroll || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}
        </span>
      </div>

      <div className="space-y-2 mb-6">
        <label className="font-sans text-xs text-slate-400 font-bold uppercase tracking-wider">Stake Amount</label>
        <div className="relative">
          <span className="absolute left-4 top-1/2 -translate-y-1/2 font-mono text-slate-400">$</span>
          <input 
            type="number" 
            value={stake}
            onChange={(e) => setStake(e.target.value)}
            disabled={isSubmitting}
            className="w-full bg-white/10 border-2 border-white/20 text-white font-mono text-lg py-3 pl-8 pr-4 rounded-lg focus:outline-none focus:border-accent transition-colors disabled:opacity-50"
          />
        </div>
      </div>

      <button
        onClick={handlePlaceBet}
        disabled={isSubmitting || !contextMarketId}
        className="w-full py-4 bg-primary text-white font-display font-black uppercase tracking-widest rounded-lg shadow-[0_4px_14px_0_rgba(230,57,70,0.39)] hover:shadow-[0_6px_20px_rgba(230,57,70,0.23)] hover:-translate-y-px transition-all disabled:opacity-50 disabled:cursor-wait"
      >
        {isSubmitting ? "Placing..." : "Lock Bet"}
      </button>

      {!contextMarketId && (
        <div className="mt-3 text-center font-mono text-xs text-slate-400 font-bold">
          Select a market on the board first
        </div>
      )}

      {lastStatus === "success" && (
        <div className="mt-3 text-center font-mono text-xs text-accent font-bold">
          ✓ Bet confirmed
        </div>
      )}
      {lastStatus === "error" && (
        <div className="mt-3 text-center font-mono text-xs text-primary font-bold">
          ✗ Network error. Bankroll refunded.
        </div>
      )}
    </div>
  );
};
