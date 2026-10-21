/**
 * TODAR MAL's currency desk and the Section 115BBJ estimator (backend `app/api/v1/currency_tax.py`).
 * Developed for Amit Ashok Kumar Patnaik. Moved out of the Paper Trading Sandbox when Group 97 retired it.
 *
 * Money is INR on the wire; the display currency is converted with TODAR MAL's rate board, never a guessed constant.
 */
import { apiClient } from "../api/client";
import { invalidate, useResource } from "./resource";

export const SUPPORTED_CURRENCIES = ["INR", "USD", "EUR", "GBP", "USDT", "BTC"] as const;
export type CurrencyCode = (typeof SUPPORTED_CURRENCIES)[number];
export const CURRENCY_LABELS: Record<CurrencyCode, { symbol: string; name: string }> = {
  INR: { symbol: "₹", name: "Indian Rupee (Parimatch primary)" },
  USD: { symbol: "$", name: "US Dollar (Pinnacle benchmark)" },
  EUR: { symbol: "€", name: "Euro (Betfair Exchange)" },
  GBP: { symbol: "£", name: "British Pound" },
  USDT: { symbol: "₮", name: "Tether (Stake crypto)" },
  BTC: { symbol: "₿", name: "Bitcoin" },
};

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
