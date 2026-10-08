import React from "react";
import { AnimatePresence, motion } from "framer-motion";

export type LabState = "idle" | "synthesizing" | "complete";

export interface TheLabSceneProps extends React.HTMLAttributes<HTMLDivElement> {
  analysisState?: LabState;
  /** Multiplier on particle speed. 1.0 = one full route traversal every `baseLoopSeconds`. */
  dataFlowRate?: number;
  activeBeakers?: number;
  statusLabel?: string;
  particlesPerRoute?: number;
  botCount?: number;
  baseLoopSeconds?: number;
  armCycleSeconds?: number;
}

const MAX_BEAKERS = 8;
const MAX_PARTICLES = 12;
const MAX_BOTS = 4;

const serverGlowMap: Record<LabState, string> = {
  idle: "#8fb3c9",
  synthesizing: "#a99bc4",
  complete: "#8cbfa5",
};

const labelClassMap: Record<LabState, string> = {
  idle: "text-sky-700 dark:text-sky-300/90",
  synthesizing: "text-violet-700 dark:text-violet-300/90",
  complete: "text-emerald-700 dark:text-emerald-300/90",
};

const beakerFillMap: Record<LabState, string> = {
  idle: "#7fa6bd",
  synthesizing: "#9d8fbf",
  complete: "#7fb398",
};

const cx = (...classes: Array<string | undefined>): string => classes.filter(Boolean).join(" ");

const clamp = (value: number, min: number, max: number, fallback: number): number =>
  Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

const buildRoute = (x: number): string => `M ${x} 720 C ${x} 560, 500 520, 500 430`;

export const TheLabScene: React.FC<TheLabSceneProps> = ({
  analysisState = "idle",
  dataFlowRate = 1.0,
  activeBeakers = 3,
  statusLabel = "Awaiting data",
  particlesPerRoute = 3,
  botCount = 2,
  baseLoopSeconds = 3,
  armCycleSeconds = 0.2,
  className,
  ...rest
}) => {
  const beakers = Math.round(clamp(activeBeakers, 1, MAX_BEAKERS, 3));
  const particles = Math.round(clamp(particlesPerRoute, 1, MAX_PARTICLES, 3));
  const bots = Math.round(clamp(botCount, 1, MAX_BOTS, 2));
  const rate = clamp(dataFlowRate, 0.05, 20, 1);
  const loopSeconds = clamp(baseLoopSeconds, 0.2, 60, 3) / rate;
  const armSeconds = clamp(armCycleSeconds, 0.05, 5, 0.2);
  const isSynthesizing = analysisState === "synthesizing";
  const isComplete = analysisState === "complete";
  const glow = serverGlowMap[analysisState];

  const spacing = 700 / (beakers + 1);
  const beakerXs = Array.from({ length: beakers }, (_, i) => 150 + spacing * (i + 1));
  const botXs = Array.from({ length: bots }, (_, i) => 260 + (480 / (bots + 1)) * (i + 1));

  return (
    <div className={cx("relative w-full aspect-square select-none", className)} {...rest}>
      <svg
        viewBox="0 0 1000 1000"
        preserveAspectRatio="xMidYMid meet"
        className="w-full h-full"
        role="img"
        aria-label={`Lab scene: ${analysisState}`}
      >
        <defs>
          <radialGradient id="lab-server-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={glow} stopOpacity={0.9} />
            <stop offset="100%" stopColor={glow} stopOpacity={0} />
          </radialGradient>
          <filter id="lab-blur" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="12" />
          </filter>
        </defs>

        {/* Floor */}
        <rect x="0" y="720" width="1000" height="280" className="fill-stone-200/70 dark:fill-stone-800/70" />
        <line x1="0" y1="720" x2="1000" y2="720" className="stroke-stone-300 dark:stroke-stone-700" strokeWidth="2" />

        {/* Server glow halo */}
        <motion.circle
          cx="500"
          cy="340"
          r="160"
          fill="url(#lab-server-glow)"
          initial={{ opacity: 0.16, scale: 1 }}
          animate={{
            opacity: isSynthesizing ? [0.18, 0.3, 0.18] : isComplete ? 0.26 : 0.16,
            scale: isSynthesizing ? [1, 1.05, 1] : 1,
          }}
          transition={{ duration: loopSeconds / 2, repeat: isSynthesizing ? Infinity : 0, ease: "easeInOut" }}
          style={{ transformOrigin: "500px 340px", willChange: "transform" }}
        />

        {/* Central database server */}
        <g>
          <rect x="420" y="240" width="160" height="200" rx="14" className="fill-stone-800 stroke-stone-600" strokeWidth="3" />
          {[0, 1, 2, 3].map((row) => (
            <g key={`server-row-${row}`}>
              <rect x="436" y={256 + row * 46} width="128" height="34" rx="6" className="fill-stone-700" />
              <motion.circle
                cx="548"
                cy={273 + row * 46}
                r="6"
                initial={{ opacity: 0.3 }}
                animate={{ opacity: isSynthesizing ? [0.3, 1, 0.3] : isComplete ? 1 : 0.5 }}
                transition={{ duration: 0.6, repeat: isSynthesizing ? Infinity : 0, delay: row * 0.12 }}
                style={{ fill: glow }}
              />
            </g>
          ))}
        </g>

        {/* Data routes + particles */}
        {beakerXs.map((x, routeIndex) => {
          const d = buildRoute(x);
          return (
            <g key={`route-${routeIndex}`}>
              <path d={d} fill="none" className="stroke-stone-700" strokeWidth="3" strokeDasharray="6 10" />
              <motion.path
                d={d}
                fill="none"
                stroke={glow}
                strokeWidth="4"
                strokeLinecap="round"
                initial={{ pathLength: 0, pathOffset: 0, opacity: 0 }}
                animate={{
                  pathLength: isSynthesizing ? [0, 0.35, 0] : isComplete ? 1 : 0,
                  pathOffset: isSynthesizing ? [0, 0.65, 1] : 0,
                  opacity: analysisState === "idle" ? 0 : 1,
                }}
                transition={{ duration: loopSeconds, repeat: isSynthesizing ? Infinity : 0, ease: "linear" }}
                style={{ willChange: "transform" }}
              />
              {Array.from({ length: particles }, (_, p) => (
                <motion.circle
                  key={`particle-${routeIndex}-${p}`}
                  r={isSynthesizing ? 7 : 5}
                  initial={{ offsetDistance: "0%", opacity: 0 }}
                  animate={{
                    offsetDistance: ["0%", "100%"],
                    opacity: analysisState === "idle" ? [0, 0.35, 0] : [0, 1, 0],
                  }}
                  transition={{
                    duration: loopSeconds,
                    repeat: Infinity,
                    ease: "linear",
                    delay: (loopSeconds / particles) * p + routeIndex * 0.15,
                  }}
                  style={{ offsetPath: `path("${d}")`, offsetRotate: "0deg", fill: glow, willChange: "transform" }}
                />
              ))}
            </g>
          );
        })}

        {/* Beakers */}
        {beakerXs.map((x, i) => (
          <g key={`beaker-${i}`}>
            <path
              d={`M ${x - 28} 640 L ${x - 28} 700 Q ${x - 28} 720 ${x - 8} 720 L ${x + 8} 720 Q ${x + 28} 720 ${x + 28} 700 L ${x + 28} 640 Z`}
              className="fill-stone-800/70 stroke-stone-500"
              strokeWidth="3"
            />
            <motion.rect
              x={x - 24}
              width="48"
              rx="4"
              initial={{ y: 700, height: 16 }}
              animate={{ y: isComplete ? 652 : isSynthesizing ? [690, 660, 690] : 684, height: isComplete ? 64 : isSynthesizing ? [26, 56, 26] : 32 }}
              transition={{ duration: loopSeconds / 1.5, repeat: isSynthesizing ? Infinity : 0, ease: "easeInOut" }}
              style={{ fill: beakerFillMap[analysisState] }}
            />
            <rect x={x - 34} y="630" width="68" height="12" rx="4" className="fill-stone-600" />
          </g>
        ))}

        {/* Lab bots with animated arms */}
        {botXs.map((x, i) => (
          <g key={`bot-${i}`}>
            <rect x={x - 30} y="560" width="60" height="70" rx="12" className="fill-stone-700 stroke-stone-500" strokeWidth="3" />
            <circle cx={x} cy="540" r="24" className="fill-stone-600 stroke-stone-400" strokeWidth="3" />
            <circle cx={x - 8} cy="536" r="4" style={{ fill: glow }} />
            <circle cx={x + 8} cy="536" r="4" style={{ fill: glow }} />
            {[-1, 1].map((side) => (
              <motion.rect
                key={`arm-${i}-${side}`}
                x={side < 0 ? x - 48 : x + 32}
                y="570"
                width="16"
                height="44"
                rx="6"
                className="fill-stone-500"
                initial={{ y: 0 }}
                animate={{ y: isSynthesizing ? [0, -10, 0] : 0 }}
                transition={{ repeat: isSynthesizing ? Infinity : 0, duration: armSeconds, delay: side < 0 ? 0 : armSeconds / 2, ease: "easeInOut" }}
                style={{ willChange: "transform" }}
              />
            ))}
          </g>
        ))}

        {/* Completion badge */}
        <AnimatePresence>
          {isComplete && (
            <motion.g
              key="lab-complete-badge"
              initial={{ opacity: 0, scale: 0.6 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.6 }}
              transition={{ type: "spring", stiffness: 260, damping: 18 }}
              style={{ transformOrigin: "500px 160px" }}
            >
              <circle cx="500" cy="160" r="44" className="fill-emerald-500/20 stroke-emerald-400" strokeWidth="4" />
              <motion.path
                d="M 478 160 L 494 176 L 524 144"
                fill="none"
                className="stroke-emerald-300"
                strokeWidth="8"
                strokeLinecap="round"
                strokeLinejoin="round"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 0.4, ease: "easeOut" }}
              />
            </motion.g>
          )}
        </AnimatePresence>
      </svg>

      {/* Status label */}
      <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
        <AnimatePresence mode="wait">
          <motion.span
            key={`lab-label-${analysisState}-${statusLabel}`}
            className={cx("rounded-full bg-white/90 px-4 py-1.5 text-xs font-medium capitalize shadow-soft backdrop-blur-sm dark:bg-stone-800/90", labelClassMap[analysisState])}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.25 }}
          >
            {statusLabel}
          </motion.span>
        </AnimatePresence>
      </div>
    </div>
  );
};

export default TheLabScene;
