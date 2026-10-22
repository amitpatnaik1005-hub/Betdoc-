/** A number input's raw text as a number (blank is NaN, which the inputs treat as empty). Group 110: out of kit.tsx. */
export const num = (raw: string): number => (raw.trim() === "" ? Number.NaN : Number(raw));
