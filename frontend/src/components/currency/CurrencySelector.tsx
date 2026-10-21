/**
 * The one-click display-currency toggle: ₹ INR (canonical) ⇄ $ USD (display) (TODAR MAL, Groups 80 and 98).
 * Developed for Amit Ashok Kumar Patnaik | BetDoc.
 *
 * Ledgers stay in INR; USD figures are converted with TODAR MAL's ECB reference rate. When that rate is not fresh the
 * toggle says so and the app keeps showing rupees (never a guessed rate).
 */
import { CURRENCY_LABELS, useFxBoard, type CurrencyCode } from "../../lib/currency";
import { MOCK_AUTH } from "../../store/useAuthStore";
import { useCurrencyStore } from "../../store/useCurrencyStore";

interface CurrencySelectorProps {
  /** Controlled use (a converter); the header leaves it unset and drives the global display currency. */
  value?: CurrencyCode;
  onSelect?: (currency: CurrencyCode) => void;
  label?: string;
}

export const CurrencySelector = ({ value, onSelect, label = "Display currency" }: CurrencySelectorProps) => {
  const display = useCurrencyStore((s) => s.displayCurrency);
  const toggle = useCurrencyStore((s) => s.toggleCurrency);
  const board = useFxBoard(!MOCK_AUTH);
  const selected = value ?? display;
  const next: CurrencyCode = selected === "INR" ? "USD" : "INR";
  const usdMissing = Boolean(board.data?.missing.USD);

  const handleToggle = () => {
    if (onSelect) onSelect(next);
    else toggle();
  };

  const title = [
    `${label}: ${CURRENCY_LABELS[selected].name}.`,
    selected === "USD" && usdMissing ? "No fresh USD rate: amounts are shown in rupees until it returns." : null,
    `Click for ${CURRENCY_LABELS[next].symbol} ${next}.`,
  ].filter(Boolean).join(" ");

  return (
    <button
      type="button"
      onClick={handleToggle}
      aria-label={`${label}: ${selected}. Switch to ${next}`}
      title={title}
      className="inline-flex items-center gap-1.5 rounded-full bg-stone-100 px-3 py-1.5 text-[11px] font-semibold tracking-wide text-stone-700 transition-[background-color,transform] duration-200 hover:bg-stone-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color-mix(in_srgb,var(--accent)_50%,transparent)] active:scale-95 dark:bg-stone-800 dark:text-stone-200 dark:hover:bg-stone-700"
    >
      <span className="material-symbols-outlined text-[14px] text-[#C89B3C] dark:text-[#E3BE63]" aria-hidden>currency_exchange</span>
      <span className="font-mono font-bold">{CURRENCY_LABELS[selected].symbol} {selected}</span>
      <span className="text-[10px] font-medium text-stone-400 dark:text-stone-500">
        {selected === "INR" ? "base" : usdMissing ? "no rate · ₹ shown" : "display"}
      </span>
    </button>
  );
};
