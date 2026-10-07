import React from "react";
import { AnimatePresence, motion } from "framer-motion";

export type HologramTone = "scanning" | "locked";

export interface PhantomHologramSceneProps extends React.HTMLAttributes<HTMLDivElement> {
  scanProgress?: number;
  isGlitching?: boolean;
  targetFound?: boolean;
  baseSpinSeconds?: number;
  glitchSeconds?: number;
  targetLabel?: string;
}

const cx = (...classes: Array<string | undefined>): string => classes.filter(Boolean).join(" ");

const clamp = (value: number, min: number, max: number, fallback: number): number =>
  Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

const toneStrokeMap: Record<HologramTone, string> = {
  scanning: "#06b6d4",
  locked: "#10b981",
};

const toneTextClassMap: Record<HologramTone, string> = {
  scanning: "text-cyan-300",
  locked: "text-emerald-300",
};

export const PhantomHologramScene: React.FC<PhantomHologramSceneProps> = ({
  scanProgress = 0,
  isGlitching = false,
  targetFound = false,
  baseSpinSeconds = 8,
  glitchSeconds = 0.1,
  targetLabel = "ARB TARGET",
  className,
  ...rest
}) => {
  const progress = clamp(scanProgress, 0, 100, 0);
  const spinSeconds = clamp(baseSpinSeconds, 0.5, 120, 8);
  const glitchDuration = clamp(glitchSeconds, 0.03, 1, 0.1);
  const tone: HologramTone = targetFound ? "locked" : "scanning";
  const stroke = toneStrokeMap[tone];
  const scanY = 760 - (progress / 100) * 520;

  return (
    <div className={cx("relative w-full aspect-square select-none", className)} {...rest}>
      <div className="absolute inset-x-[15%] bottom-[8%] h-[22%]" style={{ perspective: "900px" }}>
        <motion.div
          className="h-full w-full rounded-[50%] border-2 bg-slate-900/70"
          style={{ borderColor: stroke, boxShadow: `0 0 40px ${stroke}55, inset 0 0 30px ${stroke}33`, transformStyle: "preserve-3d", willChange: "transform" }}
          initial={{ rotateY: 0, rotateX: 70 }}
          animate={{ rotateY: 360, rotateX: 70 }}
          transition={{ duration: spinSeconds, repeat: Infinity, ease: "linear" }}
        >
          <div className="absolute inset-[12%] rounded-[50%] border border-dashed" style={{ borderColor: stroke }} />
          <div className="absolute inset-[30%] rounded-[50%] border" style={{ borderColor: stroke }} />
        </motion.div>
      </div>

      <svg viewBox="0 0 1000 1000" preserveAspectRatio="xMidYMid meet" className="relative w-full h-full" role="img" aria-label="Arbitrage scanner hologram">
        <defs>
          <linearGradient id="phantom-beam" x1="0" y1="1" x2="0" y2="0">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0} />
          </linearGradient>
          <clipPath id="phantom-reveal">
            <rect x="200" y={scanY} width="600" height={800 - scanY} />
          </clipPath>
        </defs>

        <motion.path
          d="M 380 790 L 300 240 L 700 240 L 620 790 Z"
          fill="url(#phantom-beam)"
          initial={{ opacity: 0 }}
          animate={{ opacity: isGlitching ? [0.6, 0.2, 0.8, 0.6] : 0.6 }}
          transition={{ duration: glitchDuration * 2, repeat: isGlitching ? Infinity : 0 }}
        />

        <motion.g
          initial={{ x: 0, opacity: 1, skewX: 0 }}
          animate={
            isGlitching
              ? { x: [-2, 5, -5, 2, 0], opacity: [1, 0.5, 0.9, 1], skewX: [0, 10, -10, 0] }
              : { x: 0, opacity: 1, skewX: 0 }
          }
          transition={{ duration: glitchDuration, repeat: isGlitching ? Infinity : 0, repeatType: "mirror" }}
          style={{ transformOrigin: "500px 500px", willChange: "transform" }}
        >
          <motion.circle cx="500" cy="500" r="220" fill="none" strokeWidth="3" initial={{ stroke: toneStrokeMap.scanning }} animate={{ stroke }} transition={{ duration: 0.6 }} />
          {[0.25, 0.55, 0.85].map((k) => (
            <motion.ellipse
              key={`lat-${k}`}
              cx="500"
              cy="500"
              rx="220"
              ry={220 * k}
              fill="none"
              strokeWidth="1.5"
              strokeDasharray="6 8"
              initial={{ stroke: toneStrokeMap.scanning, opacity: 0.4 }}
              animate={{ stroke, opacity: 0.6 }}
              transition={{ duration: 0.6 }}
            />
          ))}
          {[0.2, 0.6, 1].map((k) => (
            <motion.ellipse
              key={`lon-${k}`}
              cx="500"
              cy="500"
              rx={220 * k}
              ry="220"
              fill="none"
              strokeWidth="1.5"
              initial={{ stroke: toneStrokeMap.scanning, opacity: 0.4, rotate: 0 }}
              animate={{ stroke, opacity: 0.6, rotate: 360 }}
              transition={{ stroke: { duration: 0.6 }, rotate: { duration: spinSeconds * 2, repeat: Infinity, ease: "linear" } }}
              style={{ transformOrigin: "500px 500px", willChange: "transform" }}
            />
          ))}

          <g clipPath="url(#phantom-reveal)">
            {[
              [420, 380], [600, 420], [500, 520], [380, 600], [640, 600], [520, 680],
            ].map(([x, y], i) => (
              <motion.circle
                key={`node-${i}`}
                cx={x}
                cy={y}
                r="9"
                initial={{ fill: toneStrokeMap.scanning, opacity: 0.4 }}
                animate={{ fill: stroke, opacity: [0.4, 1, 0.4] }}
                transition={{ fill: { duration: 0.6 }, opacity: { duration: 1.2, repeat: Infinity, delay: i * 0.15 } }}
              />
            ))}
          </g>

          <motion.line
            x1="240"
            x2="760"
            strokeWidth="3"
            initial={{ y1: 760, y2: 760, stroke: toneStrokeMap.scanning, opacity: 0.9 }}
            animate={{ y1: scanY, y2: scanY, stroke, opacity: targetFound ? 0 : 0.9 }}
            transition={{ type: "spring", stiffness: 120, damping: 20 }}
            style={{ filter: `drop-shadow(0 0 6px ${stroke})` }}
          />
        </motion.g>

        <circle cx="500" cy="500" r="300" fill="none" className="stroke-slate-800" strokeWidth="10" />
        <motion.circle
          cx="500"
          cy="500"
          r="300"
          fill="none"
          strokeWidth="10"
          strokeLinecap="round"
          initial={{ pathLength: 0, stroke: toneStrokeMap.scanning }}
          animate={{ pathLength: progress / 100, stroke }}
          transition={{ pathLength: { type: "spring", stiffness: 60, damping: 18 }, stroke: { duration: 0.6 } }}
          style={{ rotate: -90, transformOrigin: "500px 500px" }}
        />

        <AnimatePresence>
          {targetFound && (
            <motion.g
              key="phantom-target-lock"
              initial={{ opacity: 0, scale: 1.6 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.6 }}
              transition={{ type: "spring", stiffness: 240, damping: 16 }}
              style={{ transformOrigin: "500px 500px" }}
            >
              {[0, 90, 180, 270].map((angle) => (
                <path
                  key={`bracket-${angle}`}
                  d="M 500 230 L 530 230 M 500 230 L 500 260"
                  fill="none"
                  stroke={toneStrokeMap.locked}
                  strokeWidth="8"
                  strokeLinecap="round"
                  transform={`rotate(${angle} 500 500)`}
                />
              ))}
            </motion.g>
          )}
        </AnimatePresence>
      </svg>

      <div className="pointer-events-none absolute inset-x-0 top-3 flex items-center justify-between px-4 font-mono text-[10px] uppercase tracking-widest">
        <span className={toneTextClassMap[tone]}>{targetFound ? `${targetLabel} LOCKED` : `SCANNING ${Math.round(progress)}%`}</span>
        <AnimatePresence>
          {isGlitching && (
            <motion.span
              key="phantom-glitch-flag"
              className="text-rose-400"
              initial={{ opacity: 0 }}
              animate={{ opacity: [1, 0.2, 1] }}
              exit={{ opacity: 0 }}
              transition={{ duration: glitchDuration * 3, repeat: Infinity }}
            >
              SIGNAL UNSTABLE
            </motion.span>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
};

export default PhantomHologramScene;
