import type { ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { BotAvatar } from "../components/bots/BotAvatar";
import { COMMANDER_REGISTRY, type CommanderId } from "../config/commanders.config";
import { avatarStatus, useCommanders } from "../lib/commanders";
import { formatAgo } from "../lib/format";
import { CARD_VARIANTS, SPRING, StatusBadge } from "./kit";

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
  /** Optional animated scene (Hive, Lab, Core, Phantom). */
  scene?: ReactNode;
}

export const CommanderHero = ({ commander, headline, detail, motif, actions, stats, scene }: CommanderHeroProps) => {
  const reduce = useReducedMotion();
  const profile = COMMANDER_REGISTRY[commander];
  const { byId } = useCommanders();
  const live = byId.get(commander);
  const status = live?.status ?? "NO SIGNAL";
  const words = headline.split(" ");

  return (
    <motion.section
      variants={CARD_VARIANTS}
      className="relative overflow-hidden rounded-3xl bg-white p-6 ring-1 ring-inset ring-slate-900/[0.06] lg:col-span-12 lg:p-8 dark:bg-[#161514] dark:ring-white/[0.08]"
    >
      {/* Commander glow + motif */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-32 -top-32 size-[420px] rounded-full opacity-60 blur-3xl dark:opacity-40"
        style={{ background: `radial-gradient(closest-side, ${profile.theme.glow}, transparent)` }}
      />
      {motif && (
        <div aria-hidden="true" className="pointer-events-none absolute -right-10 -top-10 size-[340px] opacity-[0.18] dark:opacity-25" style={{ color: profile.theme.primary }}>
          {motif}
        </div>
      )}

      <div className="relative z-[1] flex flex-col gap-6 xl:flex-row xl:items-center xl:justify-between">
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex items-center gap-3">
            <BotAvatar botName={profile.name} status={avatarStatus(status)} size="md" customHexColor={profile.theme.primary} />
            <div className="min-w-0">
              <p className="text-[11px] font-bold uppercase tracking-[0.2em] text-[var(--accent-text)]">
                {profile.domain}
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-slate-900 dark:text-slate-50">{profile.name}</span>
                <StatusBadge status={status === "NO SIGNAL" ? "OFFLINE" : status} label={status} />
                {live?.heartbeat && (
                  <span className="text-[11px] text-slate-400 dark:text-slate-500">heartbeat {formatAgo(live.heartbeat.last_ping_at)}</span>
                )}
              </div>
            </div>
          </div>

          <h1 className="max-w-3xl text-2xl font-semibold leading-tight tracking-tight text-slate-900 lg:text-[28px] dark:text-slate-50">
            {words.map((word, i) => (
              <motion.span
                key={`${headline}-${i}`}
                className="mr-[0.28em] inline-block"
                initial={reduce ? false : { opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ ...SPRING, delay: reduce ? 0 : 0.03 * i }}
              >
                {word}
              </motion.span>
            ))}
          </h1>
          {detail && <div className="max-w-2xl text-sm leading-relaxed text-slate-500 dark:text-slate-400">{detail}</div>}
          {stats}
          {actions && <div className="flex flex-wrap gap-2.5">{actions}</div>}
        </div>
        {scene && <div className="w-full shrink-0 xl:w-[360px]">{scene}</div>}
      </div>
    </motion.section>
  );
};

// --------------------------------------------------------------------------- motifs
// Faint section emblems drawn in currentColor (the commander's primary).
export const MOTIFS = {
  rings: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full animate-[spin_60s_linear_infinite]">
      {[180, 130, 80].map((r, i) => (
        <circle key={r} cx="200" cy="200" r={r} stroke="currentColor" strokeWidth={1.2 - i * 0.3} strokeDasharray="6 10" />
      ))}
    </svg>
  ),
  radar: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      {[170, 120, 70].map((r) => (
        <circle key={r} cx="200" cy="200" r={r} stroke="currentColor" strokeWidth="1" strokeDasharray="4 12" />
      ))}
      <line x1="200" y1="20" x2="200" y2="380" stroke="currentColor" strokeWidth="0.6" />
      <line x1="20" y1="200" x2="380" y2="200" stroke="currentColor" strokeWidth="0.6" />
      <g className="origin-center animate-[spin_4s_linear_infinite]" style={{ transformOrigin: "200px 200px" }}>
        <line x1="200" y1="200" x2="200" y2="30" stroke="currentColor" strokeWidth="2" />
      </g>
    </svg>
  ),
  constellation: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      {[[80, 120], [200, 60], [320, 140], [260, 260], [120, 300], [200, 200]].map(([x, y], i, pts) => (
        <g key={i}>
          <circle cx={x} cy={y} r="6" fill="currentColor" />
          <line x1={x} y1={y} x2={pts[(i + 2) % pts.length][0]} y2={pts[(i + 2) % pts.length][1]} stroke="currentColor" strokeWidth="1" />
        </g>
      ))}
    </svg>
  ),
  hex: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      {[0, 1, 2, 3].flatMap((row) =>
        [0, 1, 2, 3].map((col) => {
          const x = 70 + col * 80 + (row % 2) * 40;
          const y = 70 + row * 70;
          const pts = Array.from({ length: 6 }, (_, k) => {
            const a = (Math.PI / 3) * k + Math.PI / 6;
            return `${x + 34 * Math.cos(a)},${y + 34 * Math.sin(a)}`;
          }).join(" ");
          return <polygon key={`${row}-${col}`} points={pts} stroke="currentColor" strokeWidth="1.2" />;
        }),
      )}
    </svg>
  ),
  vault: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      <circle cx="200" cy="200" r="150" stroke="currentColor" strokeWidth="2" />
      <circle cx="200" cy="200" r="110" stroke="currentColor" strokeWidth="1" strokeDasharray="3 8" />
      {Array.from({ length: 12 }, (_, i) => {
        const a = (i / 12) * Math.PI * 2;
        return <line key={i} x1={200 + 120 * Math.cos(a)} y1={200 + 120 * Math.sin(a)} x2={200 + 150 * Math.cos(a)} y2={200 + 150 * Math.sin(a)} stroke="currentColor" strokeWidth="2" />;
      })}
      <circle cx="200" cy="200" r="30" stroke="currentColor" strokeWidth="2" />
    </svg>
  ),
  waves: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      {[0, 1, 2, 3, 4].map((i) => (
        <path key={i} d={`M0 ${120 + i * 40} Q100 ${80 + i * 40} 200 ${120 + i * 40} T400 ${120 + i * 40}`} stroke="currentColor" strokeWidth="1.2" />
      ))}
    </svg>
  ),
  stack: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      {[0, 1, 2, 3].map((i) => (
        <ellipse key={i} cx="200" cy={110 + i * 60} rx="130" ry="34" stroke="currentColor" strokeWidth="1.4" />
      ))}
      <line x1="70" y1="110" x2="70" y2="290" stroke="currentColor" strokeWidth="1.4" />
      <line x1="330" y1="110" x2="330" y2="290" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  ),
  circuit: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      <rect x="140" y="140" width="120" height="120" rx="12" stroke="currentColor" strokeWidth="2" />
      {[160, 200, 240].flatMap((p) => [
        <line key={`t${p}`} x1={p} y1="140" x2={p} y2="60" stroke="currentColor" strokeWidth="1.2" />,
        <line key={`b${p}`} x1={p} y1="260" x2={p} y2="340" stroke="currentColor" strokeWidth="1.2" />,
        <line key={`l${p}`} x1="140" y1={p} x2="60" y2={p} stroke="currentColor" strokeWidth="1.2" />,
        <line key={`r${p}`} x1="260" y1={p} x2="340" y2={p} stroke="currentColor" strokeWidth="1.2" />,
      ])}
    </svg>
  ),
  sliders: (
    <svg viewBox="0 0 400 400" fill="none" className="size-full">
      {[100, 200, 300].map((x, i) => (
        <g key={x}>
          <line x1={x} y1="60" x2={x} y2="340" stroke="currentColor" strokeWidth="1.4" />
          <rect x={x - 18} y={100 + i * 70} width="36" height="24" rx="6" stroke="currentColor" strokeWidth="2" />
        </g>
      ))}
    </svg>
  ),
} as const;
