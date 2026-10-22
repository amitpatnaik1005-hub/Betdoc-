/** BetDoc's brand colours and surfaces (moved out of brand.tsx in Group 110). */

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
