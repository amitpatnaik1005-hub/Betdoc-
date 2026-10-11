/**
 * Global Currency Selector (TODAR MAL, Group 80).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Fleet.
 *
 * Picks the desk's display currency. Ledgers stay in INR (Parimatch's capital currency); figures are converted for
 * display with TODAR MAL's live rate board. A currency without a fresh rate is marked and shows rupees instead.
 */
import { CURRENCY_LABELS, SUPPORTED_CURRENCIES, useFxBoard, type CurrencyCode } from "../../lib/paper_trading";
import { MOCK_AUTH } from "../../store/useAuthStore";
import { usePaperTradingStore } from "../../store/usePaperTradingStore";

interface CurrencySelectorProps {
  /** Controlled use (a converter); the header leaves it unset and drives the global display currency. */
  value?: CurrencyCode;
  onSelect?: (currency: CurrencyCode) => void;
  label?: string;
}

export const CurrencySelector = ({ value, onSelect, label = "Display currency" }: CurrencySelectorProps) => {
  const display = usePaperTradingStore((s) => s.displayCurrency);
  const setDisplay = usePaperTradingStore((s) => s.setDisplayCurrency);
  const board = useFxBoard(!MOCK_AUTH);
  const selected = value ?? display;
  const missing = board.data?.missing ?? {};

  const choose = (code: CurrencyCode) => {
    if (onSelect) onSelect(code);
    else setDisplay(code);
  };

  return (
    <label className="inline-flex items-center gap-1.5 rounded-full bg-stone-100 px-3 py-1.5 text-[11px] font-medium text-stone-600 dark:bg-stone-800 dark:text-stone-300" title={CURRENCY_LABELS[selected].name}>
      <span className="material-symbols-outlined text-[15px] text-[#C89B3C] dark:text-[#E3BE63]" aria-hidden>currency_exchange</span>
      <span className="sr-only">{label}</span>
      <select
        value={selected}
        onChange={(e) => choose(e.target.value as CurrencyCode)}
        className="cursor-pointer bg-transparent font-mono text-[11px] text-stone-800 focus:outline-none dark:text-stone-100"
      >
        {SUPPORTED_CURRENCIES.map((code) => (
          <option key={code} value={code} className="bg-white text-stone-800 dark:bg-stone-900 dark:text-stone-100">
            {CURRENCY_LABELS[code].symbol} {code}{missing[code] ? " (no rate)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
};
