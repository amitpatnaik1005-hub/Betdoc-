import { useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { AnimatedGlyph, SURFACE } from '../../ui/brand';

import { useBetStore, useOddsMarket } from "../../store/useBetStore";


const formatINR = (amount: number) => {
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' }).format(amount);
};
const paiseToRupees = (paise: number) => paise / 100;
const glide = { type: 'spring', bounce: 0, duration: 0.4 } as const;


const TICKET_CARD = `relative overflow-hidden rounded-2xl p-5 ${SURFACE.card}`;

const HAIRLINE_PILL =
  'inline-flex items-center rounded-full px-2.5 py-1 text-[10.5px] font-medium tabular-nums ring-1 ring-inset ring-slate-200/80 dark:ring-white/10';

const edgeClass = (edge: number) =>
  edge >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400';

const MESSAGE_STYLE: Record<string, { box: string; dot: string; icon: string }> = {
  locked: {
    box: 'ring-emerald-200/80 bg-emerald-50/40 text-emerald-700 dark:ring-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300',
    dot: 'bg-emerald-500',
    icon: 'verified',
  },
  rejected: {
    box: 'ring-rose-200/80 bg-rose-50/40 text-rose-700 dark:ring-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300',
    dot: 'bg-rose-500',
    icon: 'block',
  },
  failed: {
    box: 'ring-amber-200/80 bg-amber-50/40 text-amber-700 dark:ring-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300',
    dot: 'bg-amber-500',
    icon: 'warning',
  },
} as const;

const BetslipHeader = ({
  armed,
  locking,
}: {
  readonly armed: boolean;
  readonly locking: boolean;
}) => (
  <div className="flex items-center justify-between pb-1 pl-2 pr-1">
    <div className="flex items-center gap-3">
      <AnimatedGlyph
        icon="receipt_long"
        motionPreset="pulse"
        className="text-[18px] text-indigo-500 dark:text-indigo-400"
      />
      <div>
        <h2 className="text-[13px] font-bold uppercase tracking-widest text-slate-900 dark:text-slate-50">
          Bet Slip
        </h2>
        <p className={SURFACE.eyebrow}>Paper ledger &middot; idempotent execution</p>
      </div>
    </div>
    <div
      className={`flex size-8 items-center justify-center rounded-full ring-1 ring-inset transition-colors ${armed ? (locking ? 'bg-indigo-50 ring-indigo-200 dark:bg-indigo-500/10 dark:ring-indigo-500/20' : 'bg-indigo-500 ring-indigo-500 dark:ring-indigo-400') : 'bg-transparent ring-slate-200 dark:ring-white/10'}`}
    >
      <AnimatedGlyph
        icon={armed ? (locking ? 'hourglass_empty' : 'lock_open') : 'lock'}
        motionPreset={locking ? 'pulse' : 'none'}
        className={`text-[16px] ${armed ? (locking ? 'text-indigo-600 dark:text-indigo-400' : 'text-white dark:text-slate-950') : 'text-slate-400 dark:text-slate-500'}`}
        filled={armed && !locking}
      />
    </div>
  </div>
);

export const IdempotentBetslip = () => {
  const { bankroll, reserve, activeModel, contextMarketId } = useBetStore();
  const [stake] = useState("1000");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [lastStatus, setLastStatus] = useState<"idle" | "success" | "error">("idle");

  
  const selection = useOddsMarket(contextMarketId ?? '');
  const phase = isSubmitting ? 'locking' : 'idle';
  const canSubmit = !!selection && !isSubmitting;
  const reduceMotion = useReducedMotion();
  
  const wallet = { balance_paise: (bankroll ?? 0) * 100, exposure_paise: 3700000 };
  const message = lastStatus === 'error' ? { type: 'rejected', text: 'Bet failed' } : null;

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

const handleExecute = handlePlaceBet;
  return (
    <div className="flex h-full flex-col gap-3 p-5 pb-6">
      <BetslipHeader armed={Boolean(selection) && canSubmit} locking={phase === 'locking'} />

      <div className="flex-1 overflow-y-auto scrollbar-hide">
        <AnimatePresence mode="wait">
          {!selection ? (
            <motion.div
              key="empty"
              initial={reduceMotion ? undefined : { opacity: 0, scale: 0.96 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={reduceMotion ? undefined : { opacity: 0, scale: 0.96 }}
              transition={glide}
              className={`flex h-[180px] flex-col items-center justify-center text-center ${TICKET_CARD}`}
            >
              <AnimatedGlyph
                icon="touch_app"
                motionPreset="breathe"
                className="mb-3 text-[32px] text-slate-300 dark:text-slate-600"
              />
              <p className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                Select a market on the board
              </p>
              <p className={`mt-1.5 max-w-[200px] ${SURFACE.eyebrow}`}>
                Quotes expire after 30 seconds of staleness.
              </p>
            </motion.div>
          ) : (
            <motion.div
              key="ticket"
              initial={reduceMotion ? undefined : { opacity: 0, y: 12, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={reduceMotion ? undefined : { opacity: 0, scale: 0.96 }}
              transition={glide}
              className={TICKET_CARD}
            >
              <div className="mb-4 flex items-start justify-between gap-4">
                <div>
                  <div className="flex items-center gap-2">
                    <span className={HAIRLINE_PILL}>{selection.market_type}</span>
                    <span
                      className={`inline-flex items-center rounded-full bg-slate-100 px-2.5 py-1 text-[10.5px] font-bold tabular-nums tracking-wide text-slate-600 dark:bg-white/10 dark:text-slate-300`}
                    >
                      {selection.suspended
                        ? 'SUSP'
                        : selection.sportsbook_odds > 0
                          ? `+${selection.sportsbook_odds}`
                          : selection.sportsbook_odds}
                    </span>
                  </div>
                  <h3 className="mt-3 text-[15px] font-semibold leading-tight text-slate-900 dark:text-slate-50">
                    {selection.team_away} @ {selection.team_home}
                  </h3>
                </div>
              </div>

              <div className="flex items-baseline justify-between border-t border-slate-200/80 pt-3 dark:border-white/10">
                <span className={SURFACE.eyebrow}>Est. Edge</span>
                <span className={`text-[13px] font-bold tabular-nums ${edgeClass(selection.edge_percentage)}`}>
                  {selection.edge_percentage > 0 && '+'}
                  {(selection.edge_percentage * 100).toFixed(1)}%
                </span>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <div className="flex flex-col gap-3">
        <AnimatePresence mode="popLayout">
          {message && (
            <motion.div
              initial={reduceMotion ? undefined : { opacity: 0, y: 10, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={reduceMotion ? undefined : { opacity: 0, scale: 0.96 }}
              transition={glide}
              className={`flex items-center gap-3 rounded-xl p-3 ring-1 ring-inset ${MESSAGE_STYLE[message.type].box}`}
            >
              <span
                className="material-symbols-outlined shrink-0 text-[18px]"
                style={{ fontVariationSettings: "'FILL' 1" }}
                aria-hidden="true"
              >
                {MESSAGE_STYLE[message.type].icon}
              </span>
              <p className="text-[13px] font-medium leading-snug">{message.text}</p>
            </motion.div>
          )}
        </AnimatePresence>

        <motion.button
          type="button"
          disabled={!canSubmit}
          onClick={handleExecute}
          whileTap={canSubmit ? { scale: 0.98 } : undefined}
          transition={glide}
          className={[
            'relative w-full overflow-hidden rounded-xl px-4 py-3.5 outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2',
            'transition-all duration-300',
            canSubmit
              ? 'bg-indigo-600 text-white shadow-[0_4px_14px_rgba(79,70,229,0.35)] hover:bg-indigo-500 dark:bg-indigo-500 dark:hover:bg-indigo-400'
              : 'cursor-not-allowed bg-slate-100 text-slate-400 dark:bg-white/[0.04] dark:text-slate-500',
          ].join(' ')}
        >
          {phase === 'locking' && (
            <motion.div
              layoutId="betslip-progress"
              initial={{ x: '-100%' }}
              animate={{ x: '0%' }}
              transition={{ duration: 1.5, ease: 'linear' }}
              className="absolute inset-0 bg-indigo-500 dark:bg-indigo-400"
            />
          )}
          <span className="relative z-10 flex items-center justify-center gap-2 text-[13px] font-bold uppercase tracking-widest">
            {phase === 'locking' ? 'Locking...' : 'Lock Bet'}
            <AnimatedGlyph
              icon={phase === 'locking' ? 'sync' : 'arrow_forward'}
              motionPreset={phase === 'locking' ? 'sway' : 'none'}
              className="text-[16px]"
            />
          </span>
        </motion.button>
      </div>

      <div className="mt-1 flex items-center justify-between rounded-xl px-4 py-3 ring-1 ring-inset ring-slate-200/80 dark:ring-white/[0.08]">
        <div>
          <span className="flex items-center gap-1.5">
            <AnimatedGlyph
              icon="account_balance_wallet"
              motionPreset="breathe"
              className="text-[14px] text-indigo-500 dark:text-indigo-400"
              filled
            />
            <p className={SURFACE.eyebrow}>Wallet</p>
          </span>
          <p className={`text-[15px] font-semibold tracking-tight tabular-nums ${SURFACE.primary}`}>
            {wallet?.balance_paise === undefined
              ? 'Unavailable'
              : formatINR(paiseToRupees(wallet.balance_paise))}
          </p>
        </div>
        {wallet?.exposure_paise !== undefined && (
          <div className="mt-1.5 flex items-baseline justify-between gap-3">
            <p className={SURFACE.eyebrow}>Exposed</p>
            <p className="text-[12px] font-medium tabular-nums text-amber-600 dark:text-amber-400">
              {formatINR(paiseToRupees(wallet.exposure_paise))}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}