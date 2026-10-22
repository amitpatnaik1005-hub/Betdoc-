/** The desk's motion language (moved out of kit.tsx in Group 110 so the component modules export only components). */

/** Soft, organic spring: low stiffness, high damping, so nothing overshoots or snaps. */
export const SPRING = { type: "spring", stiffness: 140, damping: 24, mass: 0.9 } as const;
export const CARD_VARIANTS = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: { ...SPRING, opacity: { duration: 0.45, ease: [0.22, 1, 0.36, 1] } } },
};
export const GRID_VARIANTS = { hidden: { opacity: 0 }, show: { opacity: 1, transition: { staggerChildren: 0.07, delayChildren: 0.04 } } };
/** Lists and grids inside a card: children fade and rise in sequence. */
export const LIST_VARIANTS = { hidden: {}, show: { transition: { staggerChildren: 0.045 } } };
export const ITEM_VARIANTS = { hidden: { opacity: 0, y: 8 }, show: { opacity: 1, y: 0, transition: SPRING } };
