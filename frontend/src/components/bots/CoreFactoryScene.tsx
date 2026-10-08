import React from "react";
import { AnimatePresence, motion } from "framer-motion";

export type PressState = "raised" | "stamping";

export interface CoreFactorySceneProps extends React.HTMLAttributes<HTMLDivElement> {
  beltSpeed?: number;
  isStamping?: boolean;
  ticketsGenerated?: number;
  machineColorClass?: string;
  sparkCount?: number;
  maxVisibleTickets?: number;
}

const MAX_SPARKS = 24;
const MAX_TICKETS = 8;

const cx = (...classes: Array<string | undefined>): string => classes.filter(Boolean).join(" ");

const clamp = (value: number, min: number, max: number, fallback: number): number =>
  Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;

const sparkOffset = (index: number, count: number): { x: number; y: number } => {
  const angle = (index / count) * Math.PI * 2 + (index % 3) * 0.37;
  const radius = 40 + ((index * 37) % 45);
  return { x: Math.cos(angle) * radius, y: -Math.abs(Math.sin(angle)) * radius - 10 };
};

const gearPath = (cx0: number, cy0: number, r: number, teeth: number): string => {
  const steps = teeth * 2;
  const points: string[] = [];
  for (let i = 0; i < steps; i += 1) {
    const radius = i % 2 === 0 ? r : r * 0.8;
    const angle = (i / steps) * Math.PI * 2;
    points.push(`${(cx0 + Math.cos(angle) * radius).toFixed(2)} ${(cy0 + Math.sin(angle) * radius).toFixed(2)}`);
  }
  return `M ${points.join(" L ")} Z`;
};

const pressStateMap: Record<PressState, number> = { raised: 0, stamping: 150 };

export const CoreFactoryScene: React.FC<CoreFactorySceneProps> = ({
  beltSpeed = 1.0,
  isStamping = false,
  ticketsGenerated = 0,
  machineColorClass = "#475569",
  sparkCount = 10,
  maxVisibleTickets = 5,
  className,
  ...rest
}) => {
  const speed = clamp(beltSpeed, 0.05, 20, 1);
  const gearSeconds = 6 / speed;
  const beltSeconds = 2 / speed;
  const sparks = Math.round(clamp(sparkCount, 1, MAX_SPARKS, 10));
  const tickets = Math.round(clamp(ticketsGenerated, 0, Number.MAX_SAFE_INTEGER, 0));
  const visibleTickets = Math.min(tickets, Math.round(clamp(maxVisibleTickets, 0, MAX_TICKETS, 5)));
  const pressState: PressState = isStamping ? "stamping" : "raised";

  const gears = [
    { cx: 180, cy: 300, r: 90, teeth: 12, direction: 1 },
    { cx: 330, cy: 340, r: 64, teeth: 9, direction: -1 },
    { cx: 820, cy: 300, r: 90, teeth: 12, direction: -1 },
    { cx: 670, cy: 340, r: 64, teeth: 9, direction: 1 },
  ];

  return (
    <div className={cx("relative w-full aspect-square select-none", className)} {...rest}>
      <svg viewBox="0 0 1000 1000" preserveAspectRatio="xMidYMid meet" className="w-full h-full" role="img" aria-label="Model assembly line">
        <defs>
          <linearGradient id="factory-ticket" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#fde68a" />
            <stop offset="100%" stopColor="#f59e0b" />
          </linearGradient>
        </defs>

        <rect x="0" y="0" width="1000" height="1000" className="fill-transparent" />

        {gears.map((gear, i) => (
          <motion.g
            key={`gear-${i}`}
            initial={{ rotate: 0 }}
            animate={{ rotate: 360 * gear.direction }}
            transition={{ duration: gearSeconds * (gear.r / 90), repeat: Infinity, ease: "linear" }}
            style={{ transformOrigin: `${gear.cx}px ${gear.cy}px`, willChange: "transform" }}
          >
            <path d={gearPath(gear.cx, gear.cy, gear.r, gear.teeth)} style={{ fill: machineColorClass }} className="stroke-stone-900" strokeWidth="3" />
            <circle cx={gear.cx} cy={gear.cy} r={gear.r * 0.3} className="fill-stone-900" />
          </motion.g>
        ))}

        <rect x="400" y="120" width="200" height="60" rx="8" style={{ fill: machineColorClass }} />
        <rect x="430" y="180" width="30" height="330" style={{ fill: machineColorClass }} />
        <rect x="540" y="180" width="30" height="330" style={{ fill: machineColorClass }} />

        <motion.g
          initial={{ y: pressStateMap.raised }}
          animate={{ y: pressStateMap[pressState] }}
          transition={{ type: "spring", stiffness: 300, damping: 10, mass: 2 }}
          style={{ willChange: "transform" }}
        >
          <rect x="488" y="180" width="24" height="240" className="fill-stone-500" />
          <rect x="420" y="410" width="160" height="60" rx="6" className="fill-stone-300 stroke-stone-500" strokeWidth="4" />
          <rect x="440" y="470" width="120" height="16" rx="4" className="fill-amber-400" />
        </motion.g>

        <rect x="380" y="640" width="240" height="30" rx="4" className="fill-stone-700" />
        <rect x="60" y="670" width="880" height="40" rx="10" className="fill-stone-800 stroke-stone-600" strokeWidth="4" />
        <motion.line
          x1="70"
          y1="690"
          x2="930"
          y2="690"
          className="stroke-stone-500"
          strokeWidth="6"
          strokeDasharray="30 30"
          initial={{ strokeDashoffset: 0 }}
          animate={{ strokeDashoffset: -60 }}
          transition={{ duration: beltSeconds, repeat: Infinity, ease: "linear" }}
        />
        {[120, 300, 500, 700, 880].map((x) => (
          <motion.circle
            key={`roller-${x}`}
            cx={x}
            cy="730"
            r="22"
            className="fill-stone-700 stroke-stone-500"
            strokeWidth="4"
            strokeDasharray="12 12"
            initial={{ rotate: 0 }}
            animate={{ rotate: 360 }}
            transition={{ duration: beltSeconds * 2, repeat: Infinity, ease: "linear" }}
            style={{ transformOrigin: `${x}px 730px`, willChange: "transform" }}
          />
        ))}

        {Array.from({ length: visibleTickets }, (_, i) => (
          <motion.g
            key={`ticket-${i}`}
            initial={{ x: 0, opacity: 0 }}
            animate={{ x: [0, 360], opacity: [0, 1, 1, 0] }}
            transition={{ duration: beltSeconds * 3, repeat: Infinity, ease: "linear", delay: (beltSeconds * 3 * i) / Math.max(visibleTickets, 1) }}
            style={{ willChange: "transform" }}
          >
            <rect x="470" y="630" width="60" height="36" rx="4" fill="url(#factory-ticket)" />
            <line x1="482" y1="642" x2="518" y2="642" className="stroke-amber-900" strokeWidth="3" />
            <line x1="482" y1="654" x2="508" y2="654" className="stroke-amber-900" strokeWidth="3" />
          </motion.g>
        ))}

        <text x="500" y="860" textAnchor="middle" className="fill-stone-400 font-sans" fontSize="24" letterSpacing="1">
          Tickets
        </text>
        <AnimatePresence mode="popLayout">
          <motion.text
            key={`ticket-count-${tickets}`}
            x="500"
            y="930"
            textAnchor="middle"
            className="fill-stone-700 font-mono dark:fill-stone-300"
            fontSize="64"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            transition={{ duration: 0.25 }}
          >
            {tickets.toLocaleString()}
          </motion.text>
        </AnimatePresence>
      </svg>

      <div className="pointer-events-none absolute left-1/2 top-[64%] h-0 w-0">
        <AnimatePresence>
          {isStamping &&
            Array.from({ length: sparks }, (_, i) => {
              const offset = sparkOffset(i, sparks);
              return (
                <motion.div
                  key={`spark-${i}`}
                  className="absolute h-1.5 w-1.5 rounded-full bg-amber-300/80"
                  initial={{ x: 0, y: 0, opacity: 1, scale: 1 }}
                  animate={{ x: [0, offset.x], y: [0, offset.y], opacity: [1, 0], scale: [1, 0.3] }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.45 + (i % 4) * 0.08, ease: "easeOut", delay: 0.12 }}
                  style={{ willChange: "transform" }}
                />
              );
            })}
        </AnimatePresence>
      </div>
    </div>
  );
};

export default CoreFactoryScene;
