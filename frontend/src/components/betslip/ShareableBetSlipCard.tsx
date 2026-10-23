import React from 'react';

export interface BetLeg {
  sport: string;
  eventName: string;
  market: string;
  selection: string;
  odds: number;
  bookmaker?: string;
}

export interface ShareableBetSlipCardProps {
  ticketReference: string;
  legs: BetLeg[];
  totalOdds: number;
  stakeInr: number;
  potentialPayoutInr: number;
  netPayoutInr?: number;
  tdsInr?: number;
  dateStr?: string;
  onClose?: () => void;
}

export const ShareableBetSlipCard: React.FC<ShareableBetSlipCardProps> = ({
  ticketReference,
  legs,
  totalOdds,
  stakeInr,
  potentialPayoutInr,
  netPayoutInr,
  tdsInr,
  dateStr = new Date().toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }),
  onClose,
}) => {
  const calculatedTds = tdsInr ?? Math.max(0, (potentialPayoutInr - stakeInr) * 0.30);
  const calculatedNet = netPayoutInr ?? (potentialPayoutInr - calculatedTds);

  const handleCopyText = () => {
    const textLines = [
      `🏛️ BETDOC QUANTITATIVE SLIP · ${ticketReference}`,
      `Authority: Amit Ashok Kumar Patnaik`,
      `Date: ${dateStr}`,
      `Total Odds: ${totalOdds.toFixed(2)}x | Stake: ₹${stakeInr.toLocaleString('en-IN')}`,
      `----------------------------------------`,
      ...legs.map((leg, idx) => `${idx + 1}. [${leg.sport.toUpperCase()}] ${leg.eventName}\n   👉 ${leg.selection} @ ${leg.odds.toFixed(2)} (${leg.market})`),
      `----------------------------------------`,
      `Gross Payout: ₹${Math.round(potentialPayoutInr).toLocaleString('en-IN')}`,
      `Est. 30% TDS: ₹${Math.round(calculatedTds).toLocaleString('en-IN')}`,
      `Net Take-Home: ₹${Math.round(calculatedNet).toLocaleString('en-IN')}`,
      `Verified by BetDoc Krishna-Arjuna Engine`,
    ];
    navigator.clipboard.writeText(textLines.join('\n'));
    alert('Bet slip summary copied to clipboard!');
  };

  const handlePrint = () => {
    window.print();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 overflow-y-auto">
      <div className="relative w-full max-w-lg bg-white rounded-xl shadow-2xl border-2 border-[#C89B3C] overflow-hidden my-8 font-sans text-stone-900">
        {/* Top Header Bar */}
        <div className="bg-[#1C1917] text-white p-5 flex items-center justify-between border-b-2 border-[#C89B3C]">
          <div className="flex items-center gap-3">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path
                d="M 2 14 H 5.5 L 6.5 17 L 9 6 L 10.5 18 L 12 14 H 14 V 11.5 H 16 V 9 H 18 V 6.5 H 20.5"
                stroke="#C89B3C"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <circle cx="20.5" cy="6.5" r="1.5" fill="#C89B3C" />
            </svg>
            <div>
              <div className="text-base font-bold tracking-wider uppercase text-white flex items-center gap-2">
                BetDoc Official Slip
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#C89B3C] text-black font-extrabold uppercase">
                  Verified
                </span>
              </div>
              <div className="text-[11px] text-stone-400 font-mono">
                Ref: {ticketReference} · {dateStr}
              </div>
            </div>
          </div>
          {onClose && (
            <button
              onClick={onClose}
              className="text-stone-400 hover:text-white p-1 rounded transition cursor-pointer"
            >
              ✕
            </button>
          )}
        </div>

        {/* Legs List */}
        <div className="p-5 max-h-80 overflow-y-auto space-y-2.5 bg-stone-50 border-b border-stone-200">
          <div className="text-[11px] font-bold text-stone-500 uppercase tracking-widest mb-1">
            Selected Legs ({legs.length})
          </div>
          {legs.map((leg, index) => (
            <div
              key={index}
              className="flex items-center justify-between p-2.5 bg-white rounded border border-stone-200 shadow-sm"
            >
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-stone-100 text-stone-700">
                    {leg.sport}
                  </span>
                  <span className="text-xs font-semibold text-stone-900">{leg.eventName}</span>
                </div>
                <div className="text-[11px] text-stone-600">
                  <strong className="text-stone-800">{leg.selection}</strong> · {leg.market}
                </div>
              </div>
              <div className="text-right">
                <div className="text-xs font-bold text-[#C89B3C] font-mono">{leg.odds.toFixed(2)}x</div>
                {leg.bookmaker && <div className="text-[9px] text-stone-400 capitalize">{leg.bookmaker}</div>}
              </div>
            </div>
          ))}
        </div>

        {/* Summary Numbers */}
        <div className="p-5 bg-white space-y-3">
          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="p-2.5 bg-stone-100 rounded border border-stone-200">
              <span className="text-stone-500 block text-[10px] uppercase font-bold">Total Combined Odds</span>
              <span className="text-base font-bold text-stone-900 font-mono">{totalOdds.toFixed(2)}x</span>
            </div>
            <div className="p-2.5 bg-stone-100 rounded border border-stone-200">
              <span className="text-stone-500 block text-[10px] uppercase font-bold">Recommended Stake</span>
              <span className="text-base font-bold text-stone-900 font-mono">₹{stakeInr.toLocaleString('en-IN')}</span>
            </div>
          </div>

          <div className="p-3 bg-emerald-50 border border-emerald-200 rounded text-xs space-y-1">
            <div className="flex justify-between items-center text-stone-700">
              <span>Gross Potential Payout:</span>
              <strong className="font-mono text-stone-900">₹{Math.round(potentialPayoutInr).toLocaleString('en-IN')}</strong>
            </div>
            <div className="flex justify-between items-center text-stone-500 text-[11px]">
              <span>Section 194BA 30% TDS:</span>
              <span className="font-mono text-stone-600">-₹{Math.round(calculatedTds).toLocaleString('en-IN')}</span>
            </div>
            <div className="border-t border-emerald-200 pt-1 flex justify-between items-center font-bold text-emerald-900 text-sm">
              <span>Estimated Net Take-Home:</span>
              <span className="font-mono">₹{Math.round(calculatedNet).toLocaleString('en-IN')}</span>
            </div>
          </div>

          {/* Footer Authority */}
          <div className="pt-2 text-center text-[10px] text-stone-400 border-t border-stone-100 space-y-0.5">
            <div className="font-semibold text-stone-600">Developed for Amit Ashok Kumar Patnaik</div>
            <div>Sovereign Non-Custodial Engine · Real-Money Krishna-Arjuna Advisor</div>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="bg-stone-100 p-4 border-t border-stone-200 flex items-center justify-between gap-3">
          <button
            onClick={handleCopyText}
            className="flex-1 py-2 px-3 bg-stone-800 hover:bg-stone-900 text-white text-xs font-semibold rounded shadow transition cursor-pointer"
          >
            📋 Copy Slip Text
          </button>
          <button
            onClick={handlePrint}
            className="flex-1 py-2 px-3 bg-[#C89B3C] hover:bg-[#b08732] text-white text-xs font-semibold rounded shadow transition cursor-pointer"
          >
            🖨️ Print / Save Card
          </button>
        </div>
      </div>
    </div>
  );
};

export default ShareableBetSlipCard;
