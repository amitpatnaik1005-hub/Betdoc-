import React from "react";
import { AnimatePresence, motion } from "framer-motion";

export type BotStatus = "idle" | "active" | "success" | "error";
export type BotAvatarSize = "sm" | "md" | "lg" | "xl";

export interface BotAvatarProps extends React.HTMLAttributes<HTMLDivElement> {
  botName?: string;
  status?: BotStatus;
  size?: BotAvatarSize;
  customHexColor?: string;
  bobSeconds?: number;
  spinSeconds?: number;
  shakeSeconds?: number;
}

const cx = (...classes: Array<string | undefined>): string => classes.filter(Boolean).join(" ");

const clamp = (value: number, min: number, max: number, fallback: number): number =>
  Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

const sizeMap: Record<BotAvatarSize, string> = {
  sm: "w-8 h-8",
  md: "w-12 h-12",
  lg: "w-16 h-16",
  xl: "w-24 h-24",
};

const labelSizeMap: Record<BotAvatarSize, string> = {
  sm: "text-[8px]",
  md: "text-[10px]",
  lg: "text-xs",
  xl: "text-sm",
};

const statusColorMap: Record<BotStatus, string> = {
  idle: "#64748b",
  active: "#38bdf8",
  success: "#10b981",
  error: "#f43f5e",
};

const statusRingClassMap: Record<BotStatus, string> = {
  idle: "ring-stone-600",
  active: "ring-sky-500/60",
  success: "ring-emerald-500/70",
  error: "ring-rose-500/70",
};

const initialsOf = (name: string): string =>
  name
    .trim()
    .split(/\s+/)
    .map((part) => part.charAt(0).toUpperCase())
    .slice(0, 2)
    .join("") || "?";

export const BotAvatar: React.FC<BotAvatarProps> = ({
  botName = "bot",
  status = "idle",
  size = "md",
  customHexColor,
  bobSeconds = 2.4,
  spinSeconds = 1.2,
  shakeSeconds = 0.4,
  className,
  ...rest
}) => {
  const accent = customHexColor ?? statusColorMap[status];
  const bob = clamp(bobSeconds, 0.3, 30, 2.4);
  const spin = clamp(spinSeconds, 0.2, 30, 1.2);
  const shake = clamp(shakeSeconds, 0.1, 5, 0.4);
  const safeName = botName || "bot";

  const containerAnimate =
    status === "idle"
      ? { y: [0, -5, 0], x: 0, scale: 1 }
      : status === "success"
        ? { y: 0, x: 0, scale: [1, 1.1, 1] }
        : status === "error"
          ? { y: 0, x: [-5, 5, -5, 5, 0], scale: 1 }
          : { y: 0, x: 0, scale: 1 };

  const containerTransition =
    status === "idle"
      ? { duration: bob, repeat: Infinity, ease: "easeInOut" as const }
      : status === "success"
        ? { duration: 0.5, ease: "easeOut" as const }
        : status === "error"
          ? { duration: shake, ease: "easeInOut" as const }
          : { duration: 0.3 };

  return (
    <div className={cx("relative inline-flex items-center justify-center", sizeMap[size], className)} {...rest}>
      <motion.div
        layoutId={"bot-avatar-" + safeName}
        className={cx("relative flex h-full w-full items-center justify-center rounded-full bg-stone-800 ring-2", statusRingClassMap[status])}
        initial={{ y: 0, x: 0, scale: 1 }}
        animate={containerAnimate}
        transition={containerTransition}
        style={{ boxShadow: `0 0 ${status === "idle" ? 0 : 14}px ${accent}66`, willChange: "transform" }}
        title={`${safeName}: ${status}`}
      >
        <svg viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet" className="absolute inset-0 w-full h-full" aria-hidden>
          {/* Active: spinning dash ring */}
          <AnimatePresence>
            {status === "active" && (
              <motion.circle
                key={"bot-active-ring-" + safeName}
                cx="50"
                cy="50"
                r="44"
                fill="none"
                strokeWidth="5"
                strokeLinecap="round"
                strokeDasharray="70 206"
                initial={{ rotate: 0, opacity: 0, stroke: accent }}
                animate={{ rotate: 360, opacity: 1, stroke: accent }}
                exit={{ opacity: 0 }}
                transition={{ rotate: { duration: spin, repeat: Infinity, ease: "linear" }, opacity: { duration: 0.2 } }}
                style={{ transformOrigin: "50px 50px", willChange: "transform" }}
              />
            )}
          </AnimatePresence>

          {/* Success: instant checkmark */}
          <AnimatePresence>
            {status === "success" && (
              <motion.path
                key={"bot-success-check-" + safeName}
                d="M 30 52 L 44 66 L 72 36"
                fill="none"
                stroke={accent}
                strokeWidth="9"
                strokeLinecap="round"
                strokeLinejoin="round"
                initial={{ pathLength: 0, opacity: 0 }}
                animate={{ pathLength: 1, opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ pathLength: { duration: 0 }, opacity: { duration: 0.1 } }}
              />
            )}
          </AnimatePresence>

          {/* Error: alert glyph */}
          <AnimatePresence>
            {status === "error" && (
              <motion.g
                key={"bot-error-glyph-" + safeName}
                initial={{ opacity: 0, scale: 0.7 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.15 }}
                style={{ transformOrigin: "50px 50px" }}
              >
                <line x1="50" y1="28" x2="50" y2="58" stroke={accent} strokeWidth="9" strokeLinecap="round" />
                <circle cx="50" cy="72" r="5.5" fill={accent} />
              </motion.g>
            )}
          </AnimatePresence>
        </svg>

        {/* Initials */}
        <AnimatePresence>
          {(status === "idle" || status === "active") && (
            <motion.span
              key={"bot-initials-" + safeName}
              className={cx("relative font-semibold tracking-wide", labelSizeMap[size])}
              style={{ color: accent }}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
            >
              {initialsOf(safeName)}
            </motion.span>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
};

export default BotAvatar;
