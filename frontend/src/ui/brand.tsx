import { motion, useReducedMotion } from 'framer-motion';
import type { ReactElement } from 'react';

export const BRAND = {
  dominant: '#C89B3C',
  azure: '#EAB308',
  violet: '#D97706',
  gradient: 'bg-gradient-to-tr from-[#9F7A2A] via-[#C89B3C] to-[#E3BE63]',
  gradientText: 'bg-gradient-to-tr from-[#9F7A2A] via-[#C89B3C] to-[#E3BE63] bg-clip-text text-transparent dark:from-[#C89B3C] dark:via-[#E3BE63] dark:to-[#F1D898]',
  glow: 'shadow-soft',
  glowHover: 'hover:shadow-soft-lg',
  ring: 'focus-visible:ring-2 focus-visible:ring-[#C89B3C]/40 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-[#0c0a09]',
} as const;

export const SURFACE = {
  card: 'bg-white ring-1 ring-inset ring-stone-900/[0.06] dark:bg-[#1c1917] dark:ring-white/10',
  canvas: 'bg-[#F8F6F0] dark:bg-[#0c0a09]',
  divider: 'border-stone-200/60 dark:border-white/[0.06]',
  eyebrow: 'text-xs font-medium text-stone-400 dark:text-stone-500',
  primary: 'text-stone-900 dark:text-[#E8E6E3]',
  secondary: 'text-stone-500 dark:text-[#A8A49F]',
  muted: 'text-stone-400 dark:text-[#73706C]',
} as const;

export type LogoVariant = 'full' | 'mark';

const BetdocVector = ({
  variant = 'full',
  className = '',
}: {
  readonly variant?: LogoVariant;
  readonly className?: string;
}): ReactElement => {
  const lineClass =
    variant === 'mark' ? 'stroke-[#C89B3C]' : 'stroke-stone-900 dark:stroke-[#C89B3C]';

  return (
    <svg viewBox="0 0 24 24" className={className} fill="none" aria-hidden="true" focusable="false">
      <path
        d="M 2 14 H 5.5 L 6.5 17 L 9 6 L 10.5 18 L 12 14 H 14 V 11.5 H 16 V 9 H 18 V 6.5 H 20.5"
        className={`${lineClass} transition-colors duration-300`}
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx="20.5" cy="6.5" r="1.5" className="fill-[#C89B3C]" stroke="none" />
    </svg>
  );
};

export const BetdocLogo = ({
  variant = 'full',
  className = '',
}: {
  readonly variant?: LogoVariant;
  readonly className?: string;
}): ReactElement => {
  if (variant === 'mark') {
    return (
      <span
        role="img"
        aria-label="betdoc"
        className={`grid size-12 shrink-0 place-items-center rounded-2xl bg-[#1c1917] shadow-soft ${className}`}
      >
        <BetdocVector
          variant="mark"
          className="size-7"
        />
      </span>
    );
  }

  return (
    <div role="img" aria-label="betdoc" className={`flex items-center gap-2 ${className}`}>
      <BetdocVector
        variant="full"
        className="h-9 w-9 shrink-0"
      />
      <span className="select-none whitespace-nowrap text-2xl font-bold leading-none tracking-tight">
        <span className="text-stone-900 dark:text-[#E8E6E3]">betd</span>
        <span className="text-[#C89B3C]">oc.</span>
      </span>
    </div>
  );
};

export type GlyphMotion = 'pulse' | 'breathe' | 'sway' | 'drift' | 'tick' | 'none';

export const GLYPH_MOTION: Record<GlyphMotion, { animate: any; transition: any }> = {
  pulse: {
    animate: { opacity: [0.55, 1, 0.55], scale: [1, 1.06, 1] },
    transition: { duration: 2.2, repeat: Infinity, ease: 'easeInOut' as any },
  },
  breathe: {
    animate: { scale: [1, 1.1, 1], y: [0, -1.5, 0] },
    transition: { duration: 2.8, repeat: Infinity, ease: 'easeInOut' as any },
  },
  sway: {
    animate: { rotate: [0, -6, 0, 6, 0] },
    transition: { duration: 3.6, repeat: Infinity, ease: 'easeInOut' as any },
  },
  drift: {
    animate: { x: [0, 2, 0], y: [0, -2, 0] },
    transition: { duration: 2.4, repeat: Infinity, ease: 'easeInOut' as any },
  },
  tick: {
    animate: { y: [0, -2, 0, 0, 0] },
    transition: { duration: 1.6, repeat: Infinity, ease: 'easeInOut' as any },
  },
  none: { animate: {}, transition: {} },
};

export const AnimatedGlyph = ({
  icon,
  motionPreset,
  className = '',
  filled = false,
}: {
  readonly icon: string;
  readonly motionPreset: GlyphMotion;
  readonly className?: string;
  readonly filled?: boolean;
}): ReactElement => {
  const reduceMotion = useReducedMotion();
  const m = GLYPH_MOTION[reduceMotion ? 'none' : motionPreset];
  return (
    <motion.span
      animate={m.animate}
      transition={m.transition}
      className={`material-symbols-outlined inline-block leading-none ${className}`}
      style={{ fontVariationSettings: `'FILL' ${filled ? 1 : 0}, 'wght' 400` }}
      aria-hidden="true"
    >
      {icon}
    </motion.span>
  );
};

export const SectionHeadline = ({
  title,
  subtitle,
  icon,
  motionPreset,
  accent = 'brand',
}: {
  readonly title: string;
  readonly subtitle?: string;
  readonly icon: string;
  readonly motionPreset: GlyphMotion;
  readonly accent?: 'brand' | 'neutral';
}): ReactElement => (
  <div className="flex min-w-0 items-center gap-3">
    <span
      className={`relative grid size-9 shrink-0 place-items-center rounded-xl ${SURFACE.card}`}
    >
      <AnimatedGlyph
        icon={icon}
        motionPreset={motionPreset}
        className={`text-[18px] ${
          accent === 'brand'
            ? 'text-[#C89B3C] dark:text-[#E0B85A]'
            : 'text-stone-500 dark:text-[#A6A39E]'
        }`}
        filled
      />
    </span>
    <div className="min-w-0">
      <h2 className={`truncate text-[15px] font-semibold leading-none tracking-tight ${SURFACE.primary}`}>
        {title}
      </h2>
      {subtitle && <p className={`mt-1 truncate text-[11px] ${SURFACE.secondary}`}>{subtitle}</p>}
    </div>
  </div>
);
