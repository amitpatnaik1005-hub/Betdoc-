/**
 * Currency Formatting Utility with Indian Numbering System (Lakh/Crore) support (Group 80, TODAR MAL).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * Rupees group the Indian way (₹1,00,000.00) and compact to lakh and crore (₹1.25 L, ₹2.40 Cr). Fiat currencies use
 * the platform's Intl formatter; the two crypto units Intl has no code for are drawn by hand (₮ to 2 places, ₿ to 8).
 */

const INR = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const INR_WHOLE = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const LAKH = 100_000;
const CRORE = 10_000_000;
const CRYPTO: Record<string, { symbol: string; places: number }> = { USDT: { symbol: "₮", places: 2 }, BTC: { symbol: "₿", places: 8 } };
const FIAT_LOCALE: Record<string, string> = { USD: "en-US", EUR: "de-DE", GBP: "en-GB" };

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

/** An amount already in `currency`: ₹1,00,000.00, $1,234.56, €1.234,56, £980.00, ₮1,250.00, ₿0.01234567. */
export function formatCurrency(amount: number | null | undefined, currency: string = "INR"): string {
  const code = currency.toUpperCase();
  const v = finite(amount);
  if (code === "INR") return formatINR(v);
  const crypto = CRYPTO[code];
  if (crypto) {
    const body = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: crypto.places, maximumFractionDigits: crypto.places });
    return `${v < 0 ? "-" : ""}${crypto.symbol}${body}`;
  }
  try {
    return new Intl.NumberFormat(FIAT_LOCALE[code] ?? "en-US", { style: "currency", currency: code, maximumFractionDigits: 2 }).format(v);
  } catch {
    return `${code} ${v.toFixed(2)}`;
  }
}

/** Rupees in the display currency, from TODAR MAL's INR-per-unit rate; null when that rate is missing (never guessed). */
export function fromINR(rupees: number, inrPerUnit: number | null | undefined): number | null {
  if (!inrPerUnit || !Number.isFinite(inrPerUnit) || inrPerUnit <= 0) return null;
  return rupees / inrPerUnit;
}

/** +₹12,450.00 / -₹3,200.00 in any supported currency. */
export function formatSignedCurrency(amount: number | null | undefined, currency: string = "INR"): string {
  const v = finite(amount);
  return `${v > 0 ? "+" : ""}${formatCurrency(v, currency)}`;
}
