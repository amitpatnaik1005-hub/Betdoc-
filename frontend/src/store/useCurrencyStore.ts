/**
 * The display currency the whole app shows money in (INR on the wire, converted with TODAR MAL's rate board).
 * Developed for Amit Ashok Kumar Patnaik. Group 97 moved it out of the retired Paper Trading store; a choice saved
 * there is carried over once.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import { SUPPORTED_CURRENCIES, type CurrencyCode } from "../lib/currency";

const LEGACY_KEY = "betdoc-paper-trading-storage";

/** The display currency the retired paper store persisted, if any (storage can be unavailable: then INR). */
function legacyChoice(): CurrencyCode {
  try {
    const raw = typeof window !== "undefined" ? window.localStorage.getItem(LEGACY_KEY) : null;
    const saved: unknown = raw ? JSON.parse(raw)?.state?.displayCurrency : null;
    return (SUPPORTED_CURRENCIES as readonly string[]).includes(saved as string) ? (saved as CurrencyCode) : "INR";
  } catch {
    return "INR";
  }
}

interface CurrencyState {
  displayCurrency: CurrencyCode;
  setDisplayCurrency: (currency: CurrencyCode) => void;
}

export const useCurrencyStore = create<CurrencyState>()(
  persist(
    (set) => ({
      displayCurrency: legacyChoice(),
      setDisplayCurrency: (currency) => set({ displayCurrency: currency }),
    }),
    { name: "betdoc-display-currency" },
  ),
);
