/**
 * Parimatch Live Slip Execution Terminal (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Assisted execution for Parimatch (INR) & 1xBet:
 * 1. Deep link to the bookmaker event
 * 2. 1-click clipboard slip payload
 * 3. Kelly stake (KUMBHA) & minimum profitable odds floor (ASHVATTHAMA)
 * 4. Odds decay shield & override
 * 5. Placement confirmation with exact locked exposure
 * 6. Settle ticket with Section 115BBJ 30% TDS breakdown
 */

import { useEffect, useState } from "react";
import { checkTicketOdds, type OddsCheckResult, type RealBetTicket, type RealTicketStatus } from "../../lib/real_money";
import { useRealMoneyStore } from "../../store/useRealMoneyStore";
import { toast } from "../../store/useToastStore";
import { formatINR } from "../../utils/formatCurrency";

interface Props {
  ticket: RealBetTicket | null;
  isOpen: boolean;
  onClose: () => void;
}

export const ParimatchExecutionModal = ({ ticket, isOpen, onClose }: Props) => {
  const [copied, setCopied] = useState(false);
  const [placedOdds, setPlacedOdds] = useState<string>("");
  const [placedStake, setPlacedStake] = useState<string>("");
  const [slipId, setSlipId] = useState<string>("");
  const [overrideDecay, setOverrideDecay] = useState(false);
  const [checkingOdds, setCheckingOdds] = useState(false);
  const [oddsCheck, setOddsCheck] = useState<OddsCheckResult | null>(null);

  // Settlement state
  const [manualOutcome, setManualOutcome] = useState<RealTicketStatus>("WON");
  const [settlementNotes, setSettlementNotes] = useState("");

  const confirmPlacement = useRealMoneyStore((s) => s.confirmPlacement);
  const settleTicket = useRealMoneyStore((s) => s.settleTicket);
  const isLoading = useRealMoneyStore((s) => s.isLoading);

  useEffect(() => {
    if (ticket) {
      setPlacedOdds(String(ticket.placed_odds ?? ticket.target_odds));
      setPlacedStake(String(ticket.placed_stake > 0 ? ticket.placed_stake : ticket.recommended_stake));
      setSlipId(ticket.bookmaker_slip_id ?? "");
      setOverrideDecay(ticket.odds_decay_flag);
      setOddsCheck(null);
    }
  }, [ticket]);

  if (!isOpen || !ticket) return null;

  const isPending = ticket.status === "PENDING_PLACEMENT";
  const isPlaced = ticket.status === "PLACED";
  const isSettled = !isPending && !isPlaced;

  const copyToClipboard = async () => {
    try {
      await navigator.clipboard.writeText(ticket.clipboard_payload);
      setCopied(true);
      toast.success("Slip Copied", "Formatted betting slip copied to clipboard.");
      setTimeout(() => setCopied(false), 2500);
    } catch {
      toast.error("Copy Failed", "Unable to write to clipboard.");
    }
  };

  const handleOddsCheck = async () => {
    setCheckingOdds(true);
    try {
      const res = await checkTicketOdds(ticket.ticket_reference);
      setOddsCheck(res);
      if (res.odds_decay_detected) {
        toast.warning(
          "Odds Decay Detected",
          `Live odds (${res.current_odds?.toFixed(2) ?? "N/A"}) fell below floor ${res.minimum_profitable_odds.toFixed(2)}.`
        );
      } else {
        toast.success(
          "Odds Floor Safe",
          `Live odds are acceptable (Floor: ${res.minimum_profitable_odds.toFixed(2)}).`
        );
      }
    } catch {
      toast.error("Odds Check Failed", "Could not check current live feed.");
    } finally {
      setCheckingOdds(false);
    }
  };

  const handleConfirmPlacement = async () => {
    const odds = parseFloat(placedOdds);
    const stake = parseFloat(placedStake);
    if (!odds || odds <= 1.0) {
      toast.error("Invalid Odds", "Placed odds must be greater than 1.00");
      return;
    }
    if (!stake || stake <= 0) {
      toast.error("Invalid Stake", "Placed stake must be positive");
      return;
    }

    await confirmPlacement({
      ticket_reference: ticket.ticket_reference,
      placed_odds: odds,
      placed_stake: stake,
      bookmaker_slip_id: slipId.trim() || undefined,
      override_odds_decay: overrideDecay,
    });
  };

  const handleSettle = async () => {
    await settleTicket({
      ticket_reference: ticket.ticket_reference,
      outcome: manualOutcome,
      notes: settlementNotes.trim() || undefined,
    });
  };

  // Preview TDS calculation on win
  const previewStake = parseFloat(placedStake) || ticket.recommended_stake;
  const previewOdds = parseFloat(placedOdds) || ticket.target_odds;
  const previewGross = previewStake * previewOdds;
  const previewNetWinnings = Math.max(0, previewGross - previewStake);
  const previewTds = previewNetWinnings * 0.3;
  const previewNetProfit = previewNetWinnings - previewTds;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs">
      <div className="flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-stone-200 bg-white shadow-2xl dark:border-stone-800 dark:bg-stone-900">
        {/* Terminal Header */}
        <div className="flex items-center justify-between border-b border-stone-200 px-6 py-4 dark:border-stone-800">
          <div className="flex items-center gap-2.5">
            <span className="flex size-7 items-center justify-center rounded-lg bg-orange-500/10 text-orange-600 dark:bg-orange-500/20 dark:text-orange-400">
              🎯
            </span>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="font-mono text-sm font-bold tracking-tight text-stone-900 uppercase dark:text-stone-100">
                  ASHVATTHAMA LIVE EXECUTION TERMINAL
                </h3>
                <span className="rounded bg-stone-100 px-1.5 py-0.5 font-mono text-[10px] text-stone-600 dark:bg-stone-800 dark:text-stone-400">
                  {ticket.ticket_reference}
                </span>
              </div>
              <p className="text-[11px] text-stone-500 dark:text-stone-400">
                Book: <span className="font-semibold capitalize text-stone-800 dark:text-stone-200">{ticket.bookmaker}</span> |
                Status: <span className="font-semibold text-amber-600 dark:text-amber-400">{ticket.status}</span>
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-full text-stone-400 hover:bg-stone-100 hover:text-stone-700 dark:hover:bg-stone-800 dark:hover:text-stone-200"
          >
            ✕
          </button>
        </div>

        {/* Terminal Body */}
        <div className="flex-1 space-y-5 overflow-y-auto p-6">
          {/* Match & Market Card */}
          <div className="rounded-2xl border border-stone-200/80 bg-stone-50/80 p-4 dark:border-stone-800 dark:bg-stone-800/40">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold tracking-wide text-stone-500 uppercase dark:text-stone-400">
                {ticket.sport} · {ticket.league}
              </span>
              <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 font-mono text-[11px] font-bold text-emerald-600 dark:text-emerald-400">
                Vetting: {ticket.vetting_pillar_score.toFixed(0)}/100
              </span>
            </div>
            <h4 className="mt-1 text-base font-bold text-stone-900 dark:text-stone-100">{ticket.event_name}</h4>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="rounded-lg bg-white px-2.5 py-1 text-xs font-semibold text-stone-800 shadow-xs dark:bg-stone-700 dark:text-stone-200">
                Market: {ticket.market_type}
              </span>
              <span className="rounded-lg bg-orange-500/10 px-2.5 py-1 text-xs font-bold text-orange-600 dark:bg-orange-500/20 dark:text-orange-300">
                Selection: {ticket.selection}
              </span>
            </div>
          </div>

          {/* Pricing & Kelly Floor Matrix */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="flex flex-col rounded-xl bg-stone-100/60 p-3 dark:bg-stone-800/60">
              <span className="text-[10px] text-stone-500 dark:text-stone-400">Target Odds</span>
              <span className="font-mono text-lg font-bold text-stone-900 dark:text-stone-100">
                {ticket.target_odds.toFixed(2)}
              </span>
            </div>
            <div className="flex flex-col rounded-xl bg-amber-500/10 p-3 dark:bg-amber-500/15">
              <span className="text-[10px] text-amber-800 dark:text-amber-300">Min Odds Floor</span>
              <span className="font-mono text-lg font-bold text-amber-600 dark:text-amber-400">
                {ticket.minimum_profitable_odds.toFixed(2)}
              </span>
            </div>
            <div className="flex flex-col rounded-xl bg-stone-100/60 p-3 dark:bg-stone-800/60">
              <span className="text-[10px] text-stone-500 dark:text-stone-400">True Probability</span>
              <span className="font-mono text-lg font-bold text-stone-900 dark:text-stone-100">
                {(ticket.true_probability * 100).toFixed(1)}%
              </span>
            </div>
            <div className="flex flex-col rounded-xl bg-emerald-500/10 p-3 dark:bg-emerald-500/15">
              <span className="text-[10px] text-emerald-800 dark:text-emerald-300">Kelly Stake</span>
              <span className="font-mono text-lg font-bold text-emerald-600 dark:text-emerald-400">
                {formatINR(ticket.recommended_stake)}
              </span>
            </div>
          </div>

          {/* Action Row: Open Book & Copy Slip */}
          <div className="flex flex-wrap gap-2.5">
            <a
              href={ticket.deep_link_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-xl bg-amber-500 px-4 py-2.5 text-xs font-bold text-stone-950 shadow-sm transition hover:bg-amber-400 active:scale-95"
            >
              <span>🚀 Open Parimatch Live Event</span>
            </a>
            <button
              type="button"
              onClick={copyToClipboard}
              className={`inline-flex flex-1 items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-xs font-bold shadow-sm transition active:scale-95 ${
                copied
                  ? "bg-emerald-600 text-white"
                  : "border border-stone-200 bg-white text-stone-800 hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
              }`}
            >
              <span>{copied ? "✓ Copied to Clipboard!" : "📋 Copy One-Click Slip"}</span>
            </button>
            <button
              type="button"
              disabled={checkingOdds}
              onClick={handleOddsCheck}
              className="inline-flex items-center justify-center gap-1.5 rounded-xl border border-stone-200 bg-stone-50 px-3 py-2.5 text-xs font-medium text-stone-700 hover:bg-stone-100 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300"
              title="Query real-time line drift vs minimum profitable odds floor"
            >
              <span>{checkingOdds ? "Checking..." : "🔄 Odds Check"}</span>
            </button>
          </div>

          {/* Odds Decay Warning Banner */}
          {oddsCheck?.odds_decay_detected && (
            <div className="rounded-xl border border-rose-300 bg-rose-50 p-3 text-xs text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200">
              <div className="flex items-center gap-2 font-bold">
                <span>⚠️ Sub-Floor Decay Alert</span>
              </div>
              <p className="mt-1">
                Live price ({oddsCheck.current_odds?.toFixed(2)}) is below the required floor (
                {oddsCheck.minimum_profitable_odds.toFixed(2)}). Placing this wager would have negative mathematical expectancy.
              </p>
            </div>
          )}

          {/* Placement Form (When Pending) */}
          {isPending && (
            <div className="space-y-3 rounded-2xl border border-stone-200 p-4 dark:border-stone-800">
              <h5 className="text-xs font-bold tracking-wider text-stone-700 uppercase dark:text-stone-300">
                Confirm Real Execution on Book
              </h5>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <label className="text-xs font-medium text-stone-600 dark:text-stone-400">
                  Actual Placed Odds
                  <input
                    type="number"
                    step="0.01"
                    min="1.01"
                    value={placedOdds}
                    onChange={(e) => setPlacedOdds(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-stone-200 bg-stone-50 p-2 font-mono text-sm dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
                  />
                </label>
                <label className="text-xs font-medium text-stone-600 dark:text-stone-400">
                  Actual Placed Stake (₹)
                  <input
                    type="number"
                    step="10"
                    min="1"
                    value={placedStake}
                    onChange={(e) => setPlacedStake(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-stone-200 bg-stone-50 p-2 font-mono text-sm dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
                  />
                </label>
                <label className="text-xs font-medium text-stone-600 dark:text-stone-400">
                  Bookmaker Slip ID
                  <input
                    type="text"
                    placeholder="e.g. PM-881923"
                    value={slipId}
                    onChange={(e) => setSlipId(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-stone-200 bg-stone-50 p-2 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
                  />
                </label>
              </div>

              {/* Sub-floor override checkbox */}
              <label className="flex items-center gap-2 text-xs text-stone-600 dark:text-stone-400">
                <input
                  type="checkbox"
                  checked={overrideDecay}
                  onChange={(e) => setOverrideDecay(e.target.checked)}
                  className="rounded border-stone-300 text-orange-600 focus:ring-orange-500"
                />
                <span>Trader Override: authorize placement even if odds are below minimum floor</span>
              </label>

              {/* Potential Net Winnings & Section 115BBJ 30% TDS breakdown */}
              <div className="rounded-xl bg-stone-100/70 p-3 text-xs dark:bg-stone-800/40">
                <div className="flex justify-between font-medium text-stone-700 dark:text-stone-300">
                  <span>Gross Payout (if win):</span>
                  <span className="font-mono">{formatINR(previewGross)}</span>
                </div>
                <div className="flex justify-between text-rose-600 dark:text-rose-400">
                  <span>Section 115BBJ TDS (30% on net win):</span>
                  <span className="font-mono">-{formatINR(previewTds)}</span>
                </div>
                <div className="mt-1 flex justify-between border-t border-stone-200 pt-1 font-bold text-emerald-600 dark:border-stone-700 dark:text-emerald-400">
                  <span>Net Realized Profit:</span>
                  <span className="font-mono">+{formatINR(previewNetProfit)}</span>
                </div>
              </div>

              <button
                type="button"
                disabled={isLoading}
                onClick={handleConfirmPlacement}
                className="w-full rounded-xl bg-orange-600 py-3 text-xs font-bold text-white shadow-md transition hover:bg-orange-500 active:scale-95 disabled:opacity-50"
              >
                {isLoading ? "Locking Exposure..." : `Confirm Placement & Lock ${formatINR(previewStake)}`}
              </button>
            </div>
          )}

          {/* Settle Section (When Placed) */}
          {isPlaced && (
            <div className="space-y-3 rounded-2xl border border-amber-200 bg-amber-50/40 p-4 dark:border-amber-900/40 dark:bg-amber-950/20">
              <div className="flex items-center justify-between">
                <h5 className="text-xs font-bold tracking-wider text-amber-800 uppercase dark:text-amber-300">
                  Ticket Active on Book — Locked: {formatINR(ticket.placed_stake)}
                </h5>
                <span className="rounded bg-amber-500/20 px-2 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300">
                  PLACED @ {ticket.placed_odds?.toFixed(2)}
                </span>
              </div>
              <p className="text-xs text-stone-600 dark:text-stone-400">
                This wager will automatically settle once the match score is final. Or you can record the manual settlement result:
              </p>

              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {(["WON", "HALF_WON", "LOST", "PUSH", "VOID"] as RealTicketStatus[]).map((st) => (
                  <button
                    key={st}
                    type="button"
                    onClick={() => setManualOutcome(st)}
                    className={`rounded-lg py-2 text-xs font-bold transition ${
                      manualOutcome === st
                        ? "bg-stone-900 text-white dark:bg-white dark:text-stone-900"
                        : "border border-stone-200 bg-white text-stone-700 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300"
                    }`}
                  >
                    {st}
                  </button>
                ))}
              </div>

              <textarea
                rows={2}
                placeholder="Optional notes or settlement details..."
                value={settlementNotes}
                onChange={(e) => setSettlementNotes(e.target.value)}
                className="w-full rounded-xl border border-stone-200 bg-white p-2.5 text-xs dark:border-stone-700 dark:bg-stone-800 dark:text-stone-100"
              />

              <button
                type="button"
                disabled={isLoading}
                onClick={handleSettle}
                className="w-full rounded-xl bg-stone-900 py-2.5 text-xs font-bold text-white shadow-sm transition hover:bg-stone-800 active:scale-95 disabled:opacity-50 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-white"
              >
                {isLoading ? "Settling Ticket..." : `Settle Ticket as ${manualOutcome}`}
              </button>
            </div>
          )}

          {/* Settled Details (When Settled) */}
          {isSettled && (
            <div className="space-y-2 rounded-2xl border border-stone-200 bg-stone-50 p-4 text-xs dark:border-stone-800 dark:bg-stone-800/40">
              <h5 className="font-bold text-stone-800 dark:text-stone-200">Settlement Complete</h5>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <span className="text-stone-500">Gross Return:</span>{" "}
                  <span className="font-mono font-bold">{formatINR(ticket.gross_return)}</span>
                </div>
                <div>
                  <span className="text-stone-500">TDS (Sec 115BBJ):</span>{" "}
                  <span className="font-mono font-bold text-rose-600">-{formatINR(ticket.tds_deducted)}</span>
                </div>
                <div>
                  <span className="text-stone-500">Net Profit:</span>{" "}
                  <span className={`font-mono font-bold ${ticket.net_profit >= 0 ? "text-emerald-600" : "text-rose-600"}`}>
                    {ticket.net_profit >= 0 ? "+" : ""}{formatINR(ticket.net_profit)}
                  </span>
                </div>
                <div>
                  <span className="text-stone-500">Settled At:</span>{" "}
                  <span className="font-mono">{ticket.settled_at ? new Date(ticket.settled_at).toLocaleTimeString() : "N/A"}</span>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Terminal Footer */}
        <div className="flex items-center justify-between border-t border-stone-200 bg-stone-50 px-6 py-3 text-[11px] text-stone-500 dark:border-stone-800 dark:bg-stone-900/60 dark:text-stone-400">
          <span>Engineered for Amit Ashok Kumar Patnaik</span>
          <span>Parimatch INR live feed</span>
        </div>
      </div>
    </div>
  );
};
