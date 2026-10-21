/**
 * TODAR MAL's two-currency engine and the Section 115BBJ estimator (backend `app/api/v1/currency_tax.py`).
 * Developed for Amit Ashok Kumar Patnaik. Group 98: INR is canonical, USD a display conversion; nothing else.
 *
 * Money is INR on the wire; USD is converted with TODAR MAL's ECB rate, never a guessed constant: with no fresh rate
 * the board reports USD missing and amounts stay in rupees.
 */
import { apiClient } from "../api/client";
import { invalidate, useResource } from "./resource";

export const SUPPORTED_CURRENCIES = ["INR", "USD"] as const;
export type CurrencyCode = (typeof SUPPORTED_CURRENCIES)[number];
export const CURRENCY_LABELS: Record<CurrencyCode, { symbol: string; name: string }> = {
  INR: { symbol: "₹", name: "Indian Rupee (canonical)" },
  USD: { symbol: "$", name: "US Dollar (display, ECB reference rate)" },
};

export const isCurrencyCode = (value: unknown): value is CurrencyCode =>
  typeof value === "string" && (SUPPORTED_CURRENCIES as readonly string[]).includes(value);

export interface FxBoard {
  base: "INR";
  rates: Record<string, { inr_per_unit: number; as_of: string; provider: string; symbol: string }>;
  missing: Record<string, string>;
  max_age_seconds: number;
  developer_credit: string;
}

export interface Conversion {
  amount: number;
  from_currency: string;
  to_currency: string;
  converted: number;
  rate: number;
  last_updated_at: string;
  provider: string;
}

export interface TaxEstimate {
  gross_win: number;
  stake: number;
  gross_profit: number;
  tds_rate_pct: number;
  tds_deducted: number;
  net_take_home: number;
  cess: number;
  effective_rate_pct: number;
  currency: string;
  jurisdiction: string;
  compliance_notes: string;
  developer_credit: string;
}

export const useFxBoard = (enabled = true) =>
  useResource(enabled ? "fx:board" : null, () => apiClient.get<FxBoard>("/currency/board"), { intervalMs: 900_000 });

export const convertCurrency = (amount: number, from: CurrencyCode, to: CurrencyCode) =>
  apiClient.get<Conversion>("/currency/convert", { amount, from, to });

/** Admin only: pull fresh rates from the providers now. */
export const refreshFxRates = async () => {
  const board = await apiClient.post<FxBoard>("/currency/refresh");
  invalidate("fx");
  return board;
};

export const estimateTax = (grossWin: number, stake: number) => apiClient.get<TaxEstimate>("/tax/estimate", { gross_win: grossWin, stake });
