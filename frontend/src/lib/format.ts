/** Display formatting shared by every section. Money is INR with Indian digit grouping. */

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const INR_COMPACT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  notation: "compact",
  maximumFractionDigits: 1,
});

const NUM = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const INT = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

const safe = (n: number | null | undefined): number => (typeof n === "number" && Number.isFinite(n) ? n : 0);

/** ₹1,00,000.00 */
export const formatINR = (rupees: number | null | undefined): string => INR.format(safe(rupees));

/** ₹1.2L-style compact amount for dense tiles. */
export const formatINRCompact = (rupees: number | null | undefined): string => INR_COMPACT.format(safe(rupees));

/** +₹12,450.00 / -₹3,200.00 */
export const formatSignedINR = (rupees: number | null | undefined): string => {
  const v = safe(rupees);
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}${INR.format(Math.abs(v))}`;
};

/** Decimal odds, two places: 1.90, 2.05 */
export const formatOdds = (odds: number | null | undefined): string => safe(odds).toFixed(2);

export const formatNumber = (n: number | null | undefined): string => NUM.format(safe(n));
export const formatInt = (n: number | null | undefined): string => INT.format(safe(n));

/** Ratio (0.123) -> "12.3%". */
export const formatRatioPct = (ratio: number | null | undefined, digits = 1): string =>
  `${(safe(ratio) * 100).toFixed(digits)}%`;

/** Already-a-percentage (12.3) -> "12.3%"; signed variant for P&L-style deltas. */
export const formatPct = (pct: number | null | undefined, digits = 1): string => `${safe(pct).toFixed(digits)}%`;
export const formatSignedPct = (pct: number | null | undefined, digits = 1): string => {
  const v = safe(pct);
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
};

const TIME = new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const DATE_TIME = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
const DATE = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", year: "numeric" });

const asDate = (value: string | number | Date | null | undefined): Date | null => {
  if (value === null || value === undefined) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
};

export const formatTime = (v: string | number | Date | null | undefined): string => {
  const d = asDate(v);
  return d ? TIME.format(d) : "—";
};
export const formatDateTime = (v: string | number | Date | null | undefined): string => {
  const d = asDate(v);
  return d ? DATE_TIME.format(d) : "—";
};
export const formatDate = (v: string | number | Date | null | undefined): string => {
  const d = asDate(v);
  return d ? DATE.format(d) : "—";
};

/** "12s ago", "4m ago", "3h ago", "2d ago". */
export const formatAgo = (v: string | number | Date | null | undefined, now: number = Date.now()): string => {
  const d = asDate(v);
  if (!d) return "never";
  const s = Math.max(0, Math.round((now - d.getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86_400)}d ago`;
};

/** "HOME_WIN" / "home_team" -> "Home Win" / "Home Team". */
export const humanize = (raw: string): string =>
  raw
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());

/** Trigger a real browser download of rows as CSV (export buttons). */
export function downloadCsv(filename: string, rows: readonly Record<string, unknown>[]): void {
  if (rows.length === 0) return;
  const headers = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const cell = (v: unknown): string => {
    const text = v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const csv = [headers.join(","), ...rows.map((r) => headers.map((h) => cell(r[h])).join(","))].join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
