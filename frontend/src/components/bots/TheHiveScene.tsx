import React from "react";
import { AnimatePresence, motion } from "framer-motion";

export type KautilyaState = "observing" | "directing" | "alert";

export interface TheHiveSceneProps extends React.HTMLAttributes<HTMLDivElement> {
  activeDesks?: number;
  kautilyaState?: KautilyaState;
  /** Seconds for one dash cycle along the command links. */
  networkPulseSpeed?: number;
  gridColumns?: number;
  gridRows?: number;
  bossName?: string;
}

const MAX_COLUMNS = 10;
const MAX_ROWS = 10;

const ISOMETRIC_TRANSFORM = "rotateX(54.736deg) rotateZ(45deg)";

const cx = (...classes: Array<string | undefined>): string => classes.filter(Boolean).join(" ");

const clamp = (value: number, min: number, max: number, fallback: number): number =>
  Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

const bossRingClassMap: Record<KautilyaState, string> = {
  observing: "ring-amber-400/40",
  directing: "ring-amber-300",
  alert: "ring-rose-500",
};

const bossGlowMap: Record<KautilyaState, string> = {
  observing: "rgba(251, 191, 36, 0.35)",
  directing: "rgba(252, 211, 77, 0.8)",
  alert: "rgba(244, 63, 94, 0.8)",
};

const deskActiveClassMap: Record<KautilyaState, string> = {
  observing: "bg-sky-300/80 dark:bg-sky-400/50",
  directing: "bg-amber-300/80 dark:bg-amber-400/50",
  alert: "bg-rose-300/80 dark:bg-rose-400/50",
};

const linkStrokeMap: Record<KautilyaState, string> = {
  observing: "#38bdf8",
  directing: "#fbbf24",
  alert: "#f43f5e",
};

export const TheHiveScene: React.FC<TheHiveSceneProps> = ({
  activeDesks = 12,
  kautilyaState = "observing",
  networkPulseSpeed = 1.5,
  gridColumns = 6,
  gridRows = 6,
  bossName = "Kautilya",
  className,
  ...rest
}) => {
  const columns = Math.round(clamp(gridColumns, 1, MAX_COLUMNS, 6));
  const rows = Math.round(clamp(gridRows, 1, MAX_ROWS, 6));
  const total = columns * rows;
  const lit = Math.round(clamp(activeDesks, 0, total, Math.min(12, total)));
  const pulseSeconds = clamp(networkPulseSpeed, 0.1, 30, 1.5);
  const isAlert = kautilyaState === "alert";

  const bossCenter = { x: 100 - 100 / (columns * 2), y: 100 / (rows * 2) };
  const deskCenters = Array.from({ length: total }, (_, index) => ({
    index,
    x: ((index % columns) + 0.5) * (100 / columns),
    y: (Math.floor(index / columns) + 0.5) * (100 / rows),
  }));
  const linkTargets = deskCenters.filter((d) => d.index < lit && !(d.index === columns - 1));

  return (
    <div className={cx("relative w-full aspect-square overflow-hidden select-none", className)} {...rest}>
      <div className="absolute inset-0" style={{ perspective: "1600px" }}>
        <div
          className="absolute inset-[18%]"
          style={{ transform: ISOMETRIC_TRANSFORM, transformStyle: "preserve-3d" }}
        >
          <div
            className="absolute inset-0 grid gap-[4%] rounded-2xl bg-stone-200/60 p-[3%] dark:bg-stone-800/60"
            style={{
              gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
              gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
              transformStyle: "preserve-3d",
            }}
          >
            {deskCenters.map((desk) => {
              const isBossCell = desk.index === columns - 1;
              const isActive = desk.index < lit;
              if (isBossCell) {
                return <div key={`desk-${desk.index}`} aria-hidden className="opacity-0" />;
              }
              return (
                <motion.div
                  key={`desk-${desk.index}`}
                  className={cx(
                    "relative rounded-sm border border-stone-700/80",
                    isActive ? deskActiveClassMap[kautilyaState] : "bg-stone-300/70 dark:bg-stone-700/60"
                  )}
                  initial={{ opacity: 0, translateZ: 0 }}
                  animate={{
                    opacity: 1,
                    translateZ: isActive ? 10 : 2,
                    scale: isActive && isAlert ? [1, 1.08, 1] : 1,
                  }}
                  transition={{
                    duration: isAlert ? pulseSeconds / 3 : 0.4,
                    repeat: isActive && isAlert ? Infinity : 0,
                    delay: (desk.index % columns) * 0.03,
                  }}
                  style={{ transformStyle: "preserve-3d", willChange: "transform" }}
                >
                  <div className="absolute inset-x-[20%] top-[15%] h-[30%] rounded-sm bg-stone-950/60" />
                </motion.div>
              );
            })}
          </div>

          <svg
            viewBox="0 0 100 100"
            preserveAspectRatio="xMidYMid meet"
            className="pointer-events-none absolute inset-0 w-full h-full overflow-visible"
            style={{ transform: "translateZ(14px)" }}
          >
            <AnimatePresence>
              {kautilyaState === "directing" &&
                linkTargets.map((desk) => (
                  <motion.path
                    key={`link-${desk.index}`}
                    d={`M ${bossCenter.x} ${bossCenter.y} Q ${(bossCenter.x + desk.x) / 2} ${Math.min(bossCenter.y, desk.y) - 6} ${desk.x} ${desk.y}`}
                    fill="none"
                    stroke={linkStrokeMap[kautilyaState]}
                    strokeWidth="0.6"
                    strokeLinecap="round"
                    strokeDasharray="3 4"
                    initial={{ opacity: 0, strokeDashoffset: 0 }}
                    animate={{ opacity: 0.9, strokeDashoffset: [0, -14] }}
                    exit={{ opacity: 0 }}
                    transition={{
                      opacity: { duration: 0.3 },
                      strokeDashoffset: { duration: pulseSeconds, repeat: Infinity, ease: "linear" },
                    }}
                    style={{ willChange: "transform" }}
                  />
                ))}
            </AnimatePresence>
          </svg>

          <motion.div
            className={cx(
              "absolute flex items-center justify-center rounded-md border border-amber-500/60 bg-stone-800 ring-2",
              bossRingClassMap[kautilyaState]
            )}
            style={{
              right: 0,
              top: 0,
              width: `${100 / columns}%`,
              height: `${100 / rows}%`,
              transformStyle: "preserve-3d",
              boxShadow: `0 0 24px ${bossGlowMap[kautilyaState]}`,
              willChange: "transform",
            }}
            initial={{ translateZ: 0, opacity: 0 }}
            animate={{ translateZ: 36, opacity: 1, scale: isAlert ? [1, 1.06, 1] : 1 }}
            transition={{ duration: isAlert ? pulseSeconds / 2 : 0.5, repeat: isAlert ? Infinity : 0 }}
          >
            <div
              className="flex flex-col items-center"
              style={{ transform: "rotateZ(-45deg) rotateX(-54.736deg)", transformOrigin: "center" }}
            >
              <motion.span
                className="text-amber-400 text-[1.1em] leading-none"
                initial={{ y: 0 }}
                animate={{ y: kautilyaState === "directing" ? [0, -3, 0] : 0 }}
                transition={{ duration: 0.8, repeat: kautilyaState === "directing" ? Infinity : 0, ease: "easeInOut" }}
                aria-label="crown"
              >
                ♛
              </motion.span>
              <span className="mt-0.5 h-3 w-3 rounded-full bg-stone-200 ring-1 ring-stone-400" />
            </div>
          </motion.div>
        </div>
      </div>

      <div className="pointer-events-none absolute left-3 top-3 text-xs text-stone-400 dark:text-stone-500">
        <span className="font-medium text-stone-600 dark:text-stone-300">{bossName}</span> · {kautilyaState} · {lit}/{total} desks
      </div>
    </div>
  );
};

export default TheHiveScene;
