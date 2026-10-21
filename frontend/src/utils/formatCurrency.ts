/**
 * Currency Formatting Utility with Indian Numbering System (Lakh/Crore) support (Group 80, TODAR MAL).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * Rupees group the Indian way (₹1,00,000.00) and compact to lakh and crore (₹1.25 L, ₹2.40 Cr). Group 98: the only
 * other currency is USD ($1,234.56), for display.
 */

const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const INR_WHOLE = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const LAKH = 100_000;
const CRORE = 10_000_000;
const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

const finite = (n: number | null | undefined): number => (typeof n === "number" && Number.isFinite(n) ? n : 0);

/** ₹1,00,000.00 */
export function formatINR(amount: number | null | undefined): string {
  return INR.format(finite(amount));
}

/** ₹1,02,500: whole rupees for dense tiles. */
export function formatINRWhole(amount: number | null | undefined): string {
  return INR_WHOLE.format(finite(amount));
}

/** ₹1.25 L / ₹2.40 Cr / ₹75,000.00 (below a lakh the full figure reads better). */
export function formatLakhCrore(amount: number | null | undefined, digits = 2): string {
  const v = finite(amount);
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= CRORE) return `${sign}₹${(abs / CRORE).toFixed(digits)} Cr`;
  if (abs >= LAKH) return `${sign}₹${(abs / LAKH).toFixed(digits)} L`;
  return formatINR(v);
}

/** An amount already in `currency`: ₹1,00,000.00 or $1,234.56 (INR and USD are the only two). */
export function formatCurrency(amount: number | null | undefined, currency: "INR" | "USD" | string = "INR"): string {
  const v = finite(amount);
  return currency.toUpperCase() === "USD" ? USD.format(v) : formatINR(v);
}

/** Rupees in the display currency, from TODAR MAL's INR-per-unit rate; null when that rate is missing (never guessed). */
export function fromINR(rupees: number, inrPerUnit: number | null | undefined): number | null {
  if (!inrPerUnit || !Number.isFinite(inrPerUnit) || inrPerUnit <= 0) return null;
  return rupees / inrPerUnit;
}

/** +₹12,450.00 / -₹3,200.00 (or the USD equivalent). */
export function formatSignedCurrency(amount: number | null | undefined, currency: string = "INR"): string {
  const v = finite(amount);
  return `${v > 0 ? "+" : ""}${formatCurrency(v, currency)}`;
}
