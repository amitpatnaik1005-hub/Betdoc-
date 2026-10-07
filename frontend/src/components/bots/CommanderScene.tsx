import React, { useId } from "react";
import { motion } from "framer-motion";
import type { CommanderProfile } from "../../config/commanders.config";

export interface CommanderSceneProps extends React.HTMLAttributes<HTMLDivElement> {
  commander: CommanderProfile;
  isLive?: boolean;
  /** Seconds for one dash cycle along the command links. */
  pulseSeconds?: number;
  /** Seconds for one full rotation of the outer ring. */
  ringSeconds?: number;
  minNodes?: number;
  maxNodes?: number;
}

const clamp = (value: number, min: number, max: number, fallback: number): number =>
  Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

const initialsOf = (name: string): string =>
  name
    .trim()
    .split(/\s+/)
    .map((part) => part.charAt(0).toUpperCase())
    .slice(0, 2)
    .join("") || "?";

function CommanderSceneBase({
  commander,
  isLive = false,
  pulseSeconds = 2,
  ringSeconds = 18,
  minNodes = 4,
  maxNodes = 12,
  className,
  ...rest
}: CommanderSceneProps): React.ReactNode {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const { primary, glow, accent } = commander.theme;
  const streaming = commander.networkProtocol !== "REST";
  const engaged = commander.status === "ENGAGED";
  const pulse = clamp(pulseSeconds, 0.2, 30, 2);
  const ring = clamp(ringSeconds, 1, 120, 18);
  const lo = Math.max(1, Math.round(minNodes));
  const hi = Math.max(lo, Math.round(maxNodes));
  const nodeCount = Math.round(clamp(commander.clearanceLevel + 2, lo, hi, lo));

  const nodes = Array.from({ length: nodeCount }, (_, i) => {
    const angle = (i / nodeCount) * Math.PI * 2 - Math.PI / 2;
    return { x: 500 + Math.cos(angle) * 320, y: 500 + Math.sin(angle) * 320 };
  });

  return (
    <div className={["relative aspect-square w-full select-none", className].filter(Boolean).join(" ")} {...rest}>
      <svg
        viewBox="0 0 1000 1000"
        preserveAspectRatio="xMidYMid meet"
        className="h-full w-full"
        role="img"
        aria-label={`${commander.name} command scene`}
      >
        <defs>
          <radialGradient id={`${uid}-core`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={primary} stopOpacity={0.85} />
            <stop offset="100%" stopColor={primary} stopOpacity={0.05} />
          </radialGradient>
        </defs>

        {/* Outer ring */}
        <motion.g
          initial={{ rotate: 0 }}
          animate={{ rotate: 360 }}
          transition={{ duration: ring, repeat: Infinity, ease: "linear" }}
          style={{ transformOrigin: "500px 500px", willChange: "transform", filter: `drop-shadow(0 0 12px ${glow})` }}
        >
          <circle cx="500" cy="500" r="420" fill="none" stroke={primary} strokeWidth="3" strokeDasharray="14 22" />
        </motion.g>

        {/* Inner counter-rotating ring */}
        <motion.g
          initial={{ rotate: 0 }}
          animate={{ rotate: -360 }}
          transition={{ duration: ring * 1.5, repeat: Infinity, ease: "linear" }}
          style={{ transformOrigin: "500px 500px", willChange: "transform" }}
        >
          <circle cx="500" cy="500" r="230" fill="none" stroke={accent} strokeOpacity={0.6} strokeWidth="2" strokeDasharray="4 12" />
        </motion.g>

        {/* Command links + nodes */}
        {nodes.map((node, i) => (
          <g key={`link-${commander.id}-${i}`}>
            <motion.path
              d={`M 500 500 L ${node.x.toFixed(1)} ${node.y.toFixed(1)}`}
              fill="none"
              stroke={primary}
              strokeWidth="3"
              strokeLinecap="round"
              strokeDasharray="10 14"
              initial={{ strokeDashoffset: 0, opacity: 0.3 }}
              animate={
                streaming
                  ? { strokeDashoffset: [0, -48], opacity: isLive ? 0.9 : 0.5 }
                  : { strokeDashoffset: 0, opacity: [0.25, 0.6, 0.25] }
              }
              transition={{ duration: pulse, repeat: Infinity, ease: "linear", delay: (pulse / nodeCount) * i }}
              style={{ filter: `drop-shadow(0 0 4px ${glow})` }}
            />
            <motion.circle
              cx={node.x}
              cy={node.y}
              r="14"
              fill={accent}
              stroke={primary}
              strokeWidth="3"
              initial={{ scale: 1 }}
              animate={{ scale: isLive ? [1, 1.3, 1] : 1 }}
              transition={{ duration: pulse, repeat: isLive ? Infinity : 0, delay: (pulse / nodeCount) * i }}
              style={{ transformOrigin: `${node.x}px ${node.y}px`, willChange: "transform" }}
            />
          </g>
        ))}

        {/* Core emblem */}
        <motion.circle
          cx="500"
          cy="500"
          r="150"
          fill={`url(#${uid}-core)`}
          stroke={primary}
          strokeWidth="6"
          initial={{ scale: 0.9, opacity: 0 }}
          animate={{ scale: engaged ? [1, 1.06, 1] : 1, opacity: 1 }}
          transition={{ duration: engaged ? pulse : 0.4, repeat: engaged ? Infinity : 0, ease: "easeInOut" }}
          style={{ transformOrigin: "500px 500px", willChange: "transform", filter: `drop-shadow(0 0 24px ${glow})` }}
        />
        <text x="500" y="532" textAnchor="middle" fontSize="96" fontWeight="800" style={{ fill: accent }}>
          {initialsOf(commander.name)}
        </text>
        <text x="500" y="965" textAnchor="middle" fontSize="28" letterSpacing="8" style={{ fill: primary }}>
          {commander.domain.toUpperCase()}
        </text>
      </svg>
    </div>
  );
}

const CommanderScene = React.memo(CommanderSceneBase);
export default CommanderScene;
