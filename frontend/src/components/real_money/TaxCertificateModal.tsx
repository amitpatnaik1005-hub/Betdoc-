/**
 * Section 115BBJ 30% TDS Tax Certificate Modal (Group 95).
 * Engineered for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet.
 *
 * Generates an official withholding certificate for Indian Income Tax compliance.
 */

import { useState } from "react";
import { useTaxCertificate } from "../../lib/real_money";
import { useRealMoneyStore } from "../../store/useRealMoneyStore";
import { toast } from "../../store/useToastStore";
import { formatINR } from "../../utils/formatCurrency";

export const TaxCertificateModal = () => {
  const isTaxCertOpen = useRealMoneyStore((s) => s.isTaxCertModalOpen);
  const closeTaxCert = useRealMoneyStore((s) => s.closeTaxCertModal);

  const [fy, setFy] = useState<string>("2026-27");
  const certResource = useTaxCertificate(isTaxCertOpen, fy);
  const cert = certResource.data;

  if (!isTaxCertOpen) return null;

  const handlePrint = () => {
    window.print();
  };

  const handleCopySummary = async () => {
    if (!cert) return;
    const text = `BetDoc Section 115BBJ TDS Certificate
Developer: Amit Ashok Kumar Patnaik
Financial Year: ${cert.financial_year}
Winning Tickets: ${cert.total_winning_tickets}
Gross Winnings: ${formatINR(cert.gross_winnings)}
30% TDS Withheld: ${formatINR(cert.total_tds_withheld)}
Health & Education Cess (4%): ${formatINR(cert.cess_amount)}
Effective Rate: ${cert.effective_rate_pct}%
Issued: ${new Date(cert.issued_at).toLocaleString()}`;
    await navigator.clipboard.writeText(text);
    toast.success("Certificate Copied", "Tax breakdown copied to clipboard.");
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs">
      <div className="flex max-h-[90vh] w-full max-w-xl flex-col overflow-hidden rounded-3xl border border-stone-200 bg-white shadow-2xl dark:border-stone-800 dark:bg-stone-900">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-stone-200 px-6 py-4 dark:border-stone-800">
          <div className="flex items-center gap-2.5">
            <span className="flex size-7 items-center justify-center rounded-lg bg-amber-500/10 text-amber-600 dark:bg-amber-500/20 dark:text-amber-400">
              📜
            </span>
            <div>
              <h3 className="text-sm font-bold text-stone-900 dark:text-stone-100">
                SECTION 115BBJ TAX CERTIFICATE
              </h3>
              <p className="text-[11px] text-stone-500 dark:text-stone-400">
                Government of India | Income Tax Act, 1961
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={closeTaxCert}
            className="flex size-8 items-center justify-center rounded-full text-stone-400 hover:bg-stone-100 dark:hover:bg-stone-800"
          >
            ✕
          </button>
        </div>

        {/* Certificate Body */}
        <div className="flex-1 space-y-5 overflow-y-auto p-6 font-sans">
          {/* Financial Year Selector */}
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-stone-700 dark:text-stone-300">
              Assessment / Financial Year:
            </span>
            <select
              value={fy}
              onChange={(e) => setFy(e.target.value)}
              className="rounded-xl border border-stone-200 bg-stone-50 px-3 py-1.5 font-mono text-xs font-bold text-stone-800 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"
            >
              <option value="2026-27">FY 2026-27 (Current)</option>
              <option value="2025-26">FY 2025-26</option>
              <option value="2024-25">FY 2024-25</option>
            </select>
          </div>

          {certResource.loading && !cert ? (
            <div className="py-12 text-center text-xs text-stone-400">
              Generating withholding certificate...
            </div>
          ) : cert ? (
            <div className="space-y-4">
              {/* Official Seal / Header Box */}
              <div className="rounded-2xl border border-amber-300/60 bg-amber-50/50 p-4 text-xs text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-200">
                <div className="flex items-center justify-between font-mono font-bold">
                  <span>JURISDICTION: INDIA (IN)</span>
                  <span>SECTION: 115BBJ / 194BA</span>
                </div>
                <p className="mt-1 text-[11px] opacity-90">
                  Flat 30% Tax Deducted at Source (TDS) on net winnings per settled wager. No expenditure or allowance
                  is allowable in respect of such income, and no set-off of losses from other sources is permitted.
                </p>
              </div>

              {/* Data Table */}
              <div className="divide-y divide-stone-100 rounded-2xl border border-stone-200 bg-stone-50/50 text-xs dark:divide-stone-800 dark:border-stone-800 dark:bg-stone-800/30">
                <div className="flex justify-between p-3">
                  <span className="text-stone-500 dark:text-stone-400">Total Winning Wagers</span>
                  <span className="font-mono font-bold text-stone-900 dark:text-stone-100">
                    {cert.total_winning_tickets}
                  </span>
                </div>
                <div className="flex justify-between p-3">
                  <span className="text-stone-500 dark:text-stone-400">Gross Net Winnings</span>
                  <span className="font-mono font-bold text-emerald-600 dark:text-emerald-400">
                    {formatINR(cert.gross_winnings)}
                  </span>
                </div>
                <div className="flex justify-between p-3">
                  <span className="text-stone-500 dark:text-stone-400">Base TDS Withheld (30%)</span>
                  <span className="font-mono font-bold text-rose-600 dark:text-rose-400">
                    {formatINR(cert.total_tds_withheld)}
                  </span>
                </div>
                <div className="flex justify-between p-3">
                  <span className="text-stone-500 dark:text-stone-400">Health & Education Cess (4%)</span>
                  <span className="font-mono font-bold text-stone-700 dark:text-stone-300">
                    {formatINR(cert.cess_amount)}
                  </span>
                </div>
                <div className="flex justify-between bg-stone-100/70 p-3 font-bold dark:bg-stone-800/60">
                  <span className="text-stone-800 dark:text-stone-200">Effective Withholding Rate</span>
                  <span className="font-mono text-stone-900 dark:text-stone-100">{cert.effective_rate_pct}%</span>
                </div>
              </div>

              {/* Certificate Metadata */}
              <div className="text-[11px] text-stone-400">
                <p>Issued at: {new Date(cert.issued_at).toLocaleString()}</p>
                <p>Tax Entity: BetDoc Quantitative Sports Trading Fleet</p>
                <p>Engineered for: Amit Ashok Kumar Patnaik</p>
              </div>
            </div>
          ) : null}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 border-t border-stone-200 bg-stone-50 px-6 py-4 dark:border-stone-800 dark:bg-stone-900/60">
          <button
            type="button"
            onClick={handleCopySummary}
            className="rounded-xl border border-stone-200 bg-white px-4 py-2 text-xs font-semibold text-stone-700 shadow-xs hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-300"
          >
            📋 Copy Breakdown
          </button>
          <button
            type="button"
            onClick={handlePrint}
            className="rounded-xl bg-stone-900 px-4 py-2 text-xs font-bold text-white shadow-sm hover:bg-stone-800 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-white"
          >
            🖨️ Print Certificate
          </button>
        </div>
      </div>
    </div>
  );
};
