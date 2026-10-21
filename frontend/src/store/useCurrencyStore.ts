/**
 * The display currency the whole app shows money in: INR (canonical) or USD (display, at TODAR MAL's ECB rate).
 * Developed for Amit Ashok Kumar Patnaik. Group 98: two currencies, one click between them. A choice saved before
 * (including one from the retired Paper Trading store) is kept when it is INR or USD, else reset to INR.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import { isCurrencyCode, type CurrencyCode } from "../lib/currency";

const LEGACY_KEY = "betdoc-paper-trading-storage";

/** The display currency the retired paper store persisted, if it is still one of the two (storage can be unavailable). */
function legacyChoice(): CurrencyCode {
  try {
    const raw = typeof window !== "undefined" ? window.localStorage.getItem(LEGACY_KEY) : null;
    const saved: unknown = raw ? JSON.parse(raw)?.state?.displayCurrency : null;
    return isCurrencyCode(saved) ? saved : "INR";
  } catch {
    return "INR";
  }
}

interface CurrencyState {
  displayCurrency: CurrencyCode;
  setDisplayCurrency: (currency: CurrencyCode) => void;
  toggleCurrency: () => void;
}

export const useCurrencyStore = create<CurrencyState>()(
  persist(
    (set) => ({
      displayCurrency: legacyChoice(),
      setDisplayCurrency: (currency) => set({ displayCurrency: isCurrencyCode(currency) ? currency : "INR" }),
      toggleCurrency: () => set((s) => ({ displayCurrency: s.displayCurrency === "INR" ? "USD" : "INR" })),
    }),
    {
      name: "betdoc-display-currency",
      // A stored EUR, GBP, USDT or BTC (from before Group 98) falls back to INR instead of breaking the display
      merge: (persisted, current) => {
        const saved = (persisted as Partial<CurrencyState> | undefined)?.displayCurrency;
        return { ...current, displayCurrency: isCurrencyCode(saved) ? saved : "INR" };
      },
    },
  ),
);
