/**
 * Code 39 (Group 111): the Bet-Slip's barcode encodes its ticket number, so a scanner reads back BD-…, not decoration.
 * Each character is nine elements, bar and space alternating from a bar, three of them wide; characters are separated
 * by a narrow space and the code is framed by the start/stop character `*`. Wide elements are WIDE_RATIO modules.
 * Developed for Amit Ashok Kumar Patnaik.
 */

// n = narrow, w = wide, in bar-space-bar-space-bar-space-bar-space-bar order
const PATTERNS: Record<string, string> = {
  "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw", "5": "wnnwwnnnn", "6": "nnwwwnnnn",
  "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn", A: "wnnnnwnnw", B: "nnwnnwnnw", C: "wnwnnwnnn", D: "nnnnwwnnw", E: "wnnnwwnnn",
  F: "nnwnwwnnn", G: "nnnnnwwnw", H: "wnnnnwwnn", I: "nnwnnwwnn", J: "nnnnwwwnn", K: "wnnnnnnww", L: "nnwnnnnww", M: "wnwnnnnwn",
  N: "nnnnwnnww", O: "wnnnwnnwn", P: "nnwnwnnwn", Q: "nnnnnnwww", R: "wnnnnnwwn", S: "nnwnnnwwn", T: "nnnnwnwwn", U: "wwnnnnnnw",
  V: "nwwnnnnnw", W: "wwwnnnnnn", X: "nwnnwnnnw", Y: "wwnnwnnnn", Z: "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn",
  "*": "nwnnwnwnn",
};
const WIDE_RATIO = 2.5;

export interface Bar {
  x: number;  // in modules from the left
  width: number;
}

/** The bars (only the dark elements) and the total width in modules, or null when a character has no Code 39 form. */
export function code39(text: string): { bars: Bar[]; width: number } | null {
  const chars = `*${text.toUpperCase()}*`.split("");
  if (chars.some((c) => !(c in PATTERNS))) return null;
  const bars: Bar[] = [];
  let x = 0;
  chars.forEach((c, i) => {
    PATTERNS[c].split("").forEach((kind, j) => {
      const w = kind === "w" ? WIDE_RATIO : 1;
      if (j % 2 === 0) bars.push({ x, width: w });
      x += w;
    });
    if (i < chars.length - 1) x += 1;  // the narrow gap between characters
  });
  return { bars, width: x };
}
