/**
 * Rupee amounts in the desk's display currency (TODAR MAL, Group 80). Developed for Amit Ashok Kumar Patnaik.
 *
 * The ledgers stay in INR; this converts for display with the rate board's INR-per-unit figure. A currency the
 * board has no fresh rate for falls back to rupees (and says so), never to a guessed constant.
 */
import { useCallback } from "react";
import { useFxBoard } from "../../lib/paper_trading";
import { usePaperTradingStore } from "../../store/usePaperTradingStore";
import { formatCurrency, formatINR, formatINRWhole, fromINR } from "../../utils/formatCurrency";

/** `format(rupees)` to the paisa (or the unit's own places); `format(rupees, true)` in whole rupees for dense tiles. */
export function useDisplayMoney(): { currency: string; converted: boolean; format: (rupees: number | null | undefined, whole?: boolean) => string } {
  const currency = usePaperTradingStore((s) => s.displayCurrency);
  const board = useFxBoard(currency !== "INR");
  const rate = currency === "INR" ? 1 : board.data?.rates[currency]?.inr_per_unit;
  const usable = currency !== "INR" && typeof rate === "number" && rate > 0;
  const format = useCallback(
    (rupees: number | null | undefined, whole = false): string => {
      if (rupees === null || rupees === undefined || !Number.isFinite(rupees)) return "—";
      const rupeeText = whole ? formatINRWhole(rupees) : formatINR(rupees);
      if (!usable) return rupeeText;
      const value = fromINR(rupees, rate);
      return value === null ? rupeeText : formatCurrency(value, currency);
    },
    [currency, rate, usable],
  );
  return { currency: usable ? currency : "INR", converted: usable, format };
}
