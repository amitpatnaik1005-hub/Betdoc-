import { useState, type ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { BotAvatar } from "../components/bots/BotAvatar";
import { COMMANDER_REGISTRY, type CommanderId } from "../config/commanders.config";
import { avatarStatus, formatProbeLatency, probeCommandersNow, useCommanders } from "../lib/commanders";
import { formatAgo } from "../lib/format";
import { StatusBadge } from "./kit";
import { CARD_VARIANTS, SPRING } from "./motion";

interface CommanderHeroProps {
  commander: CommanderId;
  /** Live one-liner built from real figures, e.g. "3 open positions · ₹4,200 at risk". */
  headline: string;
  detail?: ReactNode;
  /** Section motif (SVG), drawn faintly behind the content in the commander's colour. */
  motif?: ReactNode;
  actions?: ReactNode;
  /** Optional live figures strip under the headline. */
  stats?: ReactNode;
  /** Optional animated scene (Hive, Lab, Core). */
  scene?: ReactNode;
}

export const CommanderHero = ({ commander, headline, detail, motif, actions, stats, scene }: CommanderHeroProps) => {
  const reduce = useReducedMotion();
  const profile = COMMANDER_REGISTRY[commander];
  const { byId } = useCommanders();
  const live = byId.get(commander);
  const status = live?.status ?? "NO SIGNAL";
  const words = headline.split(" ");
  const [probing, setProbing] = useState(false);
  const probe = async () => {
    setProbing(true);
    try {
      await probeCommandersNow(commander);
    } finally {
      setProbing(false);
    }
  };

  return (
    <motion.section
      variants={CARD_VARIANTS}
      className="relative overflow-hidden rounded-[2rem] bg-white p-7 shadow-soft sm:p-10 lg:col-span-12 lg:p-12 dark:bg-stone-900 dark:shadow-none dark:ring-1 dark:ring-inset dark:ring-white/[0.04]"
    >
      {/* A whisper of the commander's colour: a flat wash and a faint, static emblem. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(135deg,transparent_55%,color-mix(in_srgb,var(--accent)_7%,transparent))]"
      />
      {motif && (
        <div aria-hidden="true" className="pointer-events-none absolute -right-16 -top-16 size-[380px] text-[var(--accent)] opacity-[0.07] dark:opacity-[0.09]">
          {motif}
        </div>
      )}

      <div className="relative z-[1] flex flex-wrap items-center justify-between gap-x-10 gap-y-8">
        <div className="flex min-w-[min(100%,26rem)] flex-1 basis-[26rem] flex-col gap-5">
          <div className="flex items-center gap-3">
            <BotAvatar botName={profile.name} status={avatarStatus(status)} size="md" customHexColor={profile.theme.primary} />
            <div className="min-w-0">
              <p className="text-xs font-medium text-[var(--accent-text)]">
                {profile.domain}
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <span className="font-display text-sm font-semibold text-stone-900 dark:text-stone-100">{profile.name}</span>
                <StatusBadge status={status === "NO SIGNAL" ? "OFFLINE" : status} label={status} />
                {live?.probeLatencyMs != null && (
                  <span
                    className="rounded-full bg-stone-100 px-2 py-0.5 font-mono text-[10px] tabular-nums text-stone-500 dark:bg-white/[0.06] dark:text-stone-400"
                    title="Duration of the last health probe"
                  >
                    {formatProbeLatency(live.probeLatencyMs)}
                  </span>
                )}
                {live?.heartbeat && (
                  <span className="text-xs text-stone-400 dark:text-stone-500">heartbeat {formatAgo(live.heartbeat.last_ping_at)}</span>
                )}
                <button
                  type="button"
                  onClick={() => void probe()}
                  disabled={probing}
                  aria-busy={probing || undefined}
                  title={`Run ${profile.name}'s health probe now`}
                  className="inline-flex items-center gap-1 rounded-full bg-stone-100 px-2 py-0.5 text-[10px] font-medium text-stone-600 transition-colors hover:bg-stone-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color-mix(in_srgb,var(--accent)_50%,transparent)] disabled:opacity-50 dark:bg-white/[0.06] dark:text-stone-300 dark:hover:bg-white/[0.12]"
                >
                  <span className={`material-symbols-outlined text-[12px] ${probing ? "animate-breathe" : ""}`} aria-hidden="true">sensors</span>
                  {probing ? "Probing…" : "Probe"}
                </button>
              </div>
              {live?.activitySummary && (
                <p className="mt-1 max-w-[28rem] truncate text-xs text-stone-500 dark:text-stone-400" title={live.activitySummary}>
                  {live.activitySummary}
                </p>
              )}
            </div>
          </div>

          <h1 className="max-w-3xl font-display text-[26px] font-bold leading-[1.15] tracking-[-0.025em] text-stone-900 sm:text-3xl lg:text-[34px] dark:text-stone-100">
            {words.map((word, i) => (
              <motion.span
                key={`${headline}-${i}`}
                className="mr-[0.28em] inline-block"
                initial={reduce ? false : { opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ ...SPRING, delay: reduce ? 0 : 0.035 * i }}
              >
                {word}
              </motion.span>
            ))}
          </h1>
          {detail && <div className="max-w-2xl text-[15px] leading-relaxed text-stone-500 dark:text-stone-400">{detail}</div>}
          {stats}
          {actions && <div className="mt-1 flex flex-wrap gap-3">{actions}</div>}
        </div>
        {scene && <div className="mx-auto w-full max-w-[340px] shrink-0 basis-[260px]">{scene}</div>}
      </div>
    </motion.section>
  );
};
