import { useState, useRef, useEffect, useId, type ReactElement } from 'react';
import { useBetStore, useOddsMarket } from '../../store/useBetStore';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { BRAND, SURFACE } from '../../ui/brand';

const SCOUT_FEED_CARD = `w-full rounded-2xl px-4 py-3 text-slate-700 dark:text-slate-200 ${SURFACE.card}`;
const SCOUT_USER_CARD = 'max-w-[80%] rounded-2xl bg-slate-900 px-4 py-2.5 text-white dark:bg-white/[0.1] dark:ring-1 dark:ring-inset dark:ring-white/10';
const SCOUT_PROMPT = [
  'rounded-lg px-3 py-1.5 text-[11.5px] font-medium tracking-tight ring-1 ring-inset outline-none transition-colors',
  BRAND.ring,
].join(' ');

const OracleMark = ({ thinking, size = 22 }: { readonly thinking: boolean; readonly size?: number }): ReactElement => {
  const reduceMotion = useReducedMotion();
  const gradientId = useId();

  const eyeAnimate = reduceMotion
    ? undefined
    : thinking
      ? { x: [-1.2, 1.2, -1.2], scaleY: 1 }
      : { scaleY: [1, 1, 0.1, 1, 1], x: 0 };
  const eyeTransition = thinking
    ? { duration: 0.9, repeat: Infinity, ease: 'easeInOut' as any }
    : { duration: 3.4, times: [0, 0.9, 0.93, 0.97, 1], repeat: Infinity, ease: 'easeInOut' as any };

  return (
    <motion.svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      aria-hidden="true"
      animate={reduceMotion ? undefined : thinking ? { rotate: [0, -4, 4, 0] } : { y: [0, -1, 0] }}
      transition={(thinking ? { duration: 1.4, repeat: Infinity, ease: 'easeInOut' } : { duration: 2.8, repeat: Infinity, ease: 'easeInOut' }) as any}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor={BRAND.azure} />
          <stop offset="50%" stopColor={BRAND.dominant} />
          <stop offset="100%" stopColor={BRAND.violet} />
        </linearGradient>
      </defs>
      <path d="M12 6V3.5" stroke={`url(#${gradientId})`} strokeWidth="1.75" strokeLinecap="round" />
      <motion.circle
        cx="12" cy="3" r="1.3" fill={BRAND.violet}
        animate={reduceMotion ? undefined : { scale: [1, 1.5, 1], opacity: [0.7, 1, 0.7] }}
        transition={{ duration: thinking ? 0.6 : 1.8, repeat: Infinity, ease: 'easeInOut' as any }}
        style={{ originX: '12px', originY: '3px' }}
      />
      <rect x="4.5" y="6.5" width="15" height="12" rx="4" stroke={`url(#${gradientId})`} strokeWidth="1.75" />
      <motion.rect x="8" y="10.5" width="2.4" height="3" rx="1.2" fill={BRAND.dominant} animate={eyeAnimate} transition={eyeTransition} style={{ originX: '9.2px', originY: '12px' }} />
      <motion.rect x="13.6" y="10.5" width="2.4" height="3" rx="1.2" fill={BRAND.dominant} animate={eyeAnimate} transition={eyeTransition} style={{ originX: '14.8px', originY: '12px' }} />
      <motion.path
        d={thinking ? 'M9 15.5c1 -1 2 1 3 0s2 -1 3 0' : 'M9.5 15.5h5'}
        stroke={`url(#${gradientId})`} strokeWidth="1.5" strokeLinecap="round"
        animate={reduceMotion || !thinking ? undefined : { pathLength: [0, 1], opacity: [0.5, 1] }}
        transition={{ duration: 0.8, repeat: Infinity, repeatType: 'reverse', ease: 'easeInOut' as any }}
      />
    </motion.svg>
  );
};

export const ScoutDrawer = () => {
  const { contextMarketId, oracleHistory, appendMessage, setOracleLoading } = useBetStore();
  const selection = useOddsMarket(contextMarketId ?? "");
  const [draft, setDraft] = useState("");
  const [thinking, setThinking] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  
  const contextLabel = selection 
    ? `${selection.team_away} @ ${selection.team_home} ${selection.market_type}` 
    : "General Intel";
    
  const SPRING = { type: 'spring', bounce: 0, duration: 0.4 } as const;
  const PROMPTS = ['Implied probability?', 'Pricing edge?', 'Key factors?'];
  const messages = oracleHistory;
  const error = "";

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [oracleHistory, thinking]);

  const send = async (msgText: string) => {
    if (!msgText.trim() || thinking) return;
    const submitted = msgText.trim();
    setDraft("");
    setThinking(true);
    setOracleLoading(true);

    const userMessageId = crypto.randomUUID();
    const scoutMessageId = crypto.randomUUID();

    appendMessage({
      id: userMessageId,
      role: 'user',
      content: submitted,
      context_market_id: contextMarketId ?? undefined
    });

    try {
      const response = await fetch('/api/v1/oracle/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: submitted, context_market_id: contextMarketId })
      });

      if (!response.ok) throw new Error("API failed");

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      
      let aiText = "";
      appendMessage({
        id: scoutMessageId,
        role: 'scout',
        content: '',
        context_market_id: contextMarketId ?? undefined
      });

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          aiText += decoder.decode(value, { stream: true });
        }
      }
      
      useBetStore.setState((state) => ({
        oracleHistory: state.oracleHistory.filter(m => m.id !== scoutMessageId)
      }));

      appendMessage({
        id: scoutMessageId,
        role: 'scout',
        content: aiText || "Error communicating with Oracle.",
        context_market_id: contextMarketId ?? undefined
      });

    } catch (err) {
      appendMessage({
        id: scoutMessageId,
        role: 'scout',
        content: "Error communicating with Oracle.",
        context_market_id: contextMarketId ?? undefined
      });
    } finally {
      setThinking(false);
      setOracleLoading(false);
    }
  };

  return (
    <div className={`flex h-full min-h-0 flex-col ${SURFACE.canvas}`}>
      <header className={`shrink-0 border-b bg-white px-5 py-4 dark:bg-[#11161D] ${SURFACE.divider}`}>
        <div className="flex items-center gap-3">
          <motion.span
            whileHover={{ scale: 1.06, rotate: -4 }}
            whileTap={{ scale: 0.94 }}
            className={`grid size-9 shrink-0 cursor-default place-items-center rounded-xl ${SURFACE.card} ${thinking ? BRAND.glow : ''} transition-shadow duration-500`}
          >
            <OracleMark thinking={thinking} />
          </motion.span>
          <div className="min-w-0">
            <h2 className={`text-[15px] font-semibold leading-none tracking-tight ${SURFACE.primary}`}>Scout Oracle</h2>
            <p className={`mt-1 truncate text-[11px] font-medium ${SURFACE.secondary}`}>{contextLabel}</p>
          </div>
          <AnimatePresence>
            {thinking && (
              <motion.span
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -6 }}
                className={`ml-auto inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10.5px] font-medium ring-1 ring-inset ring-indigo-500/30 text-indigo-700 dark:text-indigo-300`}
              >
                <span className="size-1.5 animate-pulse rounded-full bg-indigo-500" aria-hidden="true" />
                Reasoning
              </motion.span>
            )}
          </AnimatePresence>
        </div>
      </header>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
        {messages.length === 0 && !thinking && (
          <div className="px-4 py-10 text-center">
            <span className={`mx-auto grid size-11 place-items-center rounded-xl ${SURFACE.card}`}>
              <OracleMark thinking={false} size={24} />
            </span>
            <p className={`mt-4 text-[13px] font-semibold tracking-tight ${SURFACE.secondary}`}>Deterministic feed explainer</p>
            <p className={`mt-1 text-[11.5px] leading-relaxed ${SURFACE.muted}`}>
              Select a market, then ask about its probability, implied price, or edge.
            </p>
          </div>
        )}

        <AnimatePresence initial={false}>
          {messages.map((message) => (
            <motion.div
              key={message.id}
              layout
              initial={{ opacity: 0, y: 10, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, scale: 0.97 }}
              transition={SPRING}
              className={message.role === 'user' ? 'flex justify-end' : 'flex justify-start'}
            >
              <div className={['relative text-[12.5px] leading-relaxed', message.role === 'user' ? SCOUT_USER_CARD : SCOUT_FEED_CARD].join(' ')}>
                {message.role !== 'user' && (
                  <>
                    <span aria-hidden="true" className={`pointer-events-none absolute inset-y-3 left-0 w-[2px] rounded-full ${BRAND.gradient}`} />
                    <p className={`mb-1.5 pl-2 ${SURFACE.eyebrow}`}>Scout</p>
                  </>
                )}
                <span className={message.role !== 'user' ? 'block pl-2' : undefined}>{message.content}</span>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>

        {thinking && (
          <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={SPRING} className="flex justify-start">
            <div className={`flex items-center gap-1.5 ${SCOUT_FEED_CARD} py-4`}>
              {[BRAND.azure, BRAND.dominant, BRAND.violet].map((color, index) => (
                <motion.span
                  key={color}
                  animate={{ y: [0, -5, 0], opacity: [0.4, 1, 0.4] }}
                  transition={{ duration: 0.85, repeat: Infinity, delay: index * 0.14, ease: 'easeInOut' }}
                  className="size-1.5 rounded-full"
                  style={{ backgroundColor: color }}
                />
              ))}
            </div>
          </motion.div>
        )}

        {error.length > 0 && (
          <motion.p
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={SPRING}
            className="rounded-xl bg-rose-50/40 px-4 py-3 text-[12px] font-medium leading-relaxed text-rose-700 ring-1 ring-inset ring-rose-200/80 dark:bg-rose-500/10 dark:text-rose-300 dark:ring-rose-500/30"
          >
            {error}
          </motion.p>
        )}
      </div>

      <div className={`shrink-0 border-t bg-white px-5 pb-4 pt-3 dark:bg-[#11161D] ${SURFACE.divider}`}>
        <div className="flex flex-wrap gap-2 pb-3">
          {PROMPTS.map((prompt) => (
            <motion.button
              key={prompt}
              type="button"
              onClick={() => void send(prompt)}
              disabled={thinking}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.96 }}
              transition={SPRING}
              className={[
                SCOUT_PROMPT,
                thinking
                  ? 'cursor-not-allowed bg-white text-slate-300 ring-slate-100 dark:bg-transparent dark:text-slate-600 dark:ring-white/[0.06]'
                  : 'bg-white text-slate-600 ring-slate-200/80 hover:bg-indigo-50/60 hover:text-indigo-700 hover:ring-indigo-500/40 dark:bg-white/[0.04] dark:text-slate-300 dark:ring-white/10 dark:hover:bg-indigo-500/10 dark:hover:text-indigo-300 dark:hover:ring-indigo-400/40',
              ].join(' ')}
            >
              {prompt}
            </motion.button>
          ))}
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send(draft);
          }}
          className="flex items-center gap-2 rounded-2xl bg-white p-1.5 pl-4 ring-1 ring-inset ring-slate-900/[0.08] transition-shadow focus-within:ring-indigo-500/40 dark:bg-white/[0.04] dark:ring-white/10 dark:focus-within:ring-indigo-400/40"
        >
          <label htmlFor="scout-draft" className="sr-only">
            Ask the scout
          </label>
          <input
            id="scout-draft"
            type="text"
            autoComplete="off"
            placeholder="Ask about this market..."
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className={`w-full min-w-0 border-0 bg-transparent p-0 text-[13px] tracking-tight placeholder:text-slate-400 focus:outline-none focus:ring-0 dark:placeholder:text-slate-600 ${SURFACE.primary}`}
          />
          <motion.button
            type="submit"
            disabled={thinking || draft.trim().length === 0}
            whileHover={thinking || draft.trim().length === 0 ? undefined : { scale: 1.06 }}
            whileTap={thinking || draft.trim().length === 0 ? undefined : { scale: 0.94 }}
            transition={SPRING}
            aria-label="Send message"
            className={[
              'grid size-9 shrink-0 place-items-center rounded-xl outline-none transition-[box-shadow,filter] duration-300',
              BRAND.ring,
              thinking || draft.trim().length === 0
                ? 'cursor-not-allowed bg-slate-100 text-slate-300 dark:bg-white/[0.04] dark:text-slate-600'
                : `${BRAND.gradient} text-white ${BRAND.glow} ${BRAND.glowHover} hover:brightness-110`,
            ].join(' ')}
          >
            <span className={['material-symbols-outlined text-[18px] leading-none', thinking ? 'animate-spin' : ''].join(' ')} aria-hidden="true">
              {thinking ? 'progress_activity' : 'arrow_upward'}
            </span>
          </motion.button>
        </form>

        <p className={`mt-3 text-center text-[10px] leading-relaxed tracking-tight ${SURFACE.muted}`}>
          Deterministic feed explainer · paper ledger only · no sportsbook execution
        </p>
      </div>
    </div>
  );


};
