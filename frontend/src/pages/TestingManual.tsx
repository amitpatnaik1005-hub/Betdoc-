import React from 'react';

export const TestingManual: React.FC = () => {
  const handlePrint = () => {
    window.print();
  };

  return (
    <div className="min-h-screen bg-stone-100 py-10 px-4 sm:px-6 lg:px-8 text-stone-900">
      {/* Control bar */}
      <div className="max-w-4xl mx-auto mb-6 flex items-center justify-between no-print">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
          <span className="text-xs uppercase tracking-widest font-semibold text-stone-600">
            Official System Publication · Certified SOP
          </span>
        </div>
        <button
          onClick={handlePrint}
          className="inline-flex items-center gap-2 px-4 py-2 bg-[#C89B3C] hover:bg-[#b08732] text-white text-xs font-semibold uppercase tracking-wider rounded shadow transition cursor-pointer"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M17 17h2a2 2 0 002-2v-4a2 2 0 00-2-2H5a2 2 0 00-2 2v4a2 2 0 002 2h2m2 4h6a2 2 0 002-2v-4a2 2 0 00-2-2H9a2 2 0 00-2 2v4a2 2 0 002 2zm8-12V5a2 2 0 00-2-2H9a2 2 0 00-2 2v4h10z" />
          </svg>
          Print Notice / Save PDF
        </button>
      </div>

      {/* Main Gazette Notice Paper */}
      <div className="max-w-4xl mx-auto bg-white border-4 border-double border-[#C89B3C] p-8 sm:p-12 shadow-2xl font-serif">
        {/* Emblem & Official Header */}
        <div className="text-center border-b-2 border-[#C89B3C] pb-6 mb-8">
          <svg
            width="68"
            height="68"
            viewBox="0 0 24 24"
            fill="none"
            className="mx-auto mb-3"
            xmlns="http://www.w3.org/2000/svg"
          >
            <path
              d="M 2 14 H 5.5 L 6.5 17 L 9 6 L 10.5 18 L 12 14 H 14 V 11.5 H 16 V 9 H 18 V 6.5 H 20.5"
              stroke="#C89B3C"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <circle cx="20.5" cy="6.5" r="1.5" fill="#C89B3C" />
          </svg>

          <h1 className="text-2xl sm:text-3xl font-bold tracking-wider text-stone-900 uppercase">
            The Gazette of BetDoc Quantitative Systems
          </h1>
          <p className="text-xs sm:text-sm font-semibold tracking-widest text-[#C89B3C] uppercase mt-1">
            Extraordinary · Published by Authority
          </p>
          <p className="text-xs text-stone-500 mt-2 font-mono">
            Ref No. <strong className="text-stone-800">BD/GAZ/2026/G116-SOP</strong> &nbsp;|&nbsp; Promulgation Date:{' '}
            <strong className="text-stone-800">23rd October 2026</strong>
          </p>
        </div>

        {/* Public Notification Alert Box */}
        <div className="bg-[#FFFDF9] border border-stone-200 border-l-4 border-l-[#C89B3C] p-4 mb-8 text-sm leading-relaxed text-stone-800">
          <strong>STATUTORY NOTIFICATION:</strong> This document serves as the{' '}
          <em>Official Standard Operating Procedure and Testing Manual</em> for <strong>BetDoc</strong>. It is issued
          for the guidance of authorized operators, analytical partners, and testing users under the direct authority
          of <strong>Amit Ashok Kumar Patnaik</strong>, Chief Architect & Quantitative Sovereign.
        </div>

        {/* PART I */}
        <section className="mb-10">
          <h2 className="text-lg font-bold uppercase tracking-wider text-stone-900 border-b border-stone-200 pb-1 mb-4 flex items-center gap-2">
            <span className="text-[#C89B3C]">Part I</span> — The Sovereign Charter (The Krishna-Arjuna Law)
          </h2>
          <div className="space-y-4 text-sm leading-relaxed text-stone-700">
            <p>
              <strong className="text-stone-900">1. Nature of the System:</strong> BetDoc is an analytical advisor and quantitative calculation engine. BetDoc does <strong>not</strong> hold your money, does <strong>not</strong> accept bank deposits, and does <strong>not</strong> automatically place bets on bookmaker sites.
            </p>
            <p>
              <strong className="text-stone-900">2. The Krishna-Arjuna Law:</strong>
            </p>
            <ul className="list-disc pl-6 space-y-2">
              <li>
                <strong>BetDoc is Lord Krishna (The Strategist & Accountant):</strong> It monitors thousands of matches, computes real probabilities, detects true bookmaker bias, prepares optimal ticket combinations, alerts you to in-play cashouts, and logs every rupee with exact 30% TDS tax math under Commander <strong>Todar Mal</strong>.
              </li>
              <li>
                <strong>You are Arjuna (The Warrior & Money Owner):</strong> You hold your own money safely in your personal bookmaker account (such as <strong>Parimatch</strong> or <strong>1xBet</strong>). You read the advice, place the tickets with your own hands, enter the booking code into your Bet-Slip, and collect your real cash winnings directly.
              </li>
            </ul>
          </div>
        </section>

        {/* PART II */}
        <section className="mb-10">
          <h2 className="text-lg font-bold uppercase tracking-wider text-stone-900 border-b border-stone-200 pb-1 mb-4 flex items-center gap-2">
            <span className="text-[#C89B3C]">Part II</span> — The 6 Daily Steps to Victory (Operator Guide)
          </h2>

          <div className="space-y-6 text-sm text-stone-700">
            <div className="p-4 bg-stone-50 rounded border border-stone-200">
              <h3 className="font-bold text-stone-900 mb-1">
                Step 1: Check the Morning Market Signal (07:55 AM IST)
              </h3>
              <p className="leading-relaxed">
                Every morning at 07:55 AM, BetDoc evaluates the entire day's match slate and sends a message to your Telegram and SMS:
              </p>
              <div className="mt-2 space-y-1 pl-4 border-l-2 border-stone-300">
                <p>
                  🔴 <strong className="text-red-700">Score &lt; 60 (REST DAY):</strong> Sit out! Matches have high margins or squad rotations. Do not bet today.
                </p>
                <p>
                  🟢 <strong className="text-emerald-700">Score &ge; 80 (PRIME DAY):</strong> Exceptional slate with clear value. Proceed to Step 2.
                </p>
              </div>
            </div>

            <div className="p-4 bg-stone-50 rounded border border-stone-200">
              <h3 className="font-bold text-stone-900 mb-1">
                Step 2: Pick Heavyweight Anchors on "The Oracle"
              </h3>
              <p className="leading-relaxed">
                Open <strong>The Oracle</strong>. BetDoc automatically scans live games across Football, Cricket, Tennis, and Basketball. It highlights <strong>Heavyweight Anchors</strong> (such as Real Madrid to score over 1.5, Jasprit Bumrah to take 1+ wickets, or Carlos Alcaraz to win). Cross-sport diversification protects you from single-sport shocks.
              </p>
            </div>

            <div className="p-4 bg-stone-50 rounded border border-stone-200">
              <h3 className="font-bold text-stone-900 mb-1">
                Step 3: Run the "15-to-3" System Splitter
              </h3>
              <p className="leading-relaxed mb-3">
                Never risk your capital on a single 15-leg ticket. BetDoc automatically splits your 15 picks into three smart tickets:
              </p>
              <div className="overflow-x-auto font-sans text-xs">
                <table className="w-full text-left border-collapse border border-stone-300 bg-white">
                  <thead>
                    <tr className="bg-stone-100 text-stone-900">
                      <th className="p-2 border border-stone-300">Ticket</th>
                      <th className="p-2 border border-stone-300">Picks</th>
                      <th className="p-2 border border-stone-300">Stake %</th>
                      <th className="p-2 border border-stone-300">Example Stake</th>
                      <th className="p-2 border border-stone-300">Payout Expectancy</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className="p-2 border border-stone-300 font-bold text-emerald-800">Slip A: Fortress</td>
                      <td className="p-2 border border-stone-300">4-5 Safest Anchors</td>
                      <td className="p-2 border border-stone-300">40%</td>
                      <td className="p-2 border border-stone-300">₹100</td>
                      <td className="p-2 border border-stone-300 font-bold">~₹305 (Covers full ₹250 session stake!)</td>
                    </tr>
                    <tr>
                      <td className="p-2 border border-stone-300 font-bold text-blue-800">Slip B: Growth</td>
                      <td className="p-2 border border-stone-300">8-9 Balanced Picks</td>
                      <td className="p-2 border border-stone-300">40%</td>
                      <td className="p-2 border border-stone-300">₹100</td>
                      <td className="p-2 border border-stone-300">~₹1,000 (Substantial profit)</td>
                    </tr>
                    <tr>
                      <td className="p-2 border border-stone-300 font-bold text-purple-800">Slip C: Moonshot</td>
                      <td className="p-2 border border-stone-300">All 15 Picks</td>
                      <td className="p-2 border border-stone-300">20%</td>
                      <td className="p-2 border border-stone-300">₹50</td>
                      <td className="p-2 border border-stone-300">~₹2,500+ (Jackpot return)</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div className="p-4 bg-stone-50 rounded border border-stone-200">
              <h3 className="font-bold text-stone-900 mb-1">
                Step 4: Place Bets on Parimatch / 1xBet & Enter Booking Code
              </h3>
              <p className="leading-relaxed">
                Click <strong>"Record All 3 Tickets to Bet-Slip"</strong>, tap the direct Parimatch or 1xBet link, place the bet in your bookmaker app, and paste the resulting <strong>Booking Code</strong> (e.g. <code>5X7A9B</code>) into BetDoc. Todar Mal locks the stake in your ledger and activates your live tracking shield.
              </p>
            </div>

            <div className="p-4 bg-stone-50 rounded border border-stone-200">
              <h3 className="font-bold text-stone-900 mb-1">
                Step 5: Watch the In-Play Cashout Advisor & 5-Layer Stop-Loss
              </h3>
              <p className="leading-relaxed">
                While matches are live, BetDoc evaluates in-play probability second by second:
              </p>
              <ul className="list-disc pl-5 mt-2 space-y-1">
                <li>
                  <strong className="text-emerald-700">CASHOUT ADVISED:</strong> When 12 of 15 legs have won and odds are in your favor, BetDoc advises you to cash out immediately on Parimatch to guarantee profit before late drama.
                </li>
                <li>
                  <strong className="text-red-700">STOP LOSS BREACH:</strong> If an unforeseen injury or red card occurs, BetDoc triggers an early cashout signal to salvage capital.
                </li>
              </ul>
            </div>

            <div className="p-4 bg-stone-50 rounded border border-stone-200">
              <h3 className="font-bold text-stone-900 mb-1">
                Step 6: Review Real Rupee Ledger & Section 194BA 30% TDS
              </h3>
              <p className="leading-relaxed">
                Indian tax law mandates <strong>30% TDS</strong> on net gambling winnings:
                <br />
                <span className="font-mono text-xs block mt-1 bg-stone-100 p-2 rounded">
                  Net Profit = Gross Payout - Stake &nbsp;|&nbsp; 30% Tax = Net Profit × 0.30 &nbsp;|&nbsp; Take-Home = Payout - Tax
                </span>
                On losing or breakeven bets, tax is strictly <strong>₹0.00</strong>. Todar Mal maintains the permanent tax audit ledger in <strong>The Vault</strong>.
              </p>
            </div>
          </div>
        </section>

        {/* PART III */}
        <section className="mb-10">
          <h2 className="text-lg font-bold uppercase tracking-wider text-stone-900 border-b border-stone-200 pb-1 mb-4 flex items-center gap-2">
            <span className="text-[#C89B3C]">Part III</span> — Operator Verification Protocol
          </h2>
          <div className="space-y-3 text-sm text-stone-700">
            <div className="flex items-start gap-3">
              <input type="checkbox" defaultChecked readOnly className="mt-1 accent-[#C89B3C]" />
              <div>
                <strong>Item 1: System Liveness:</strong> Backend responds at <code>http://localhost:8000/api/v1/health</code> and frontend renders at <code>http://localhost:5173</code> in under 2 seconds.
              </div>
            </div>
            <div className="flex items-start gap-3">
              <input type="checkbox" defaultChecked readOnly className="mt-1 accent-[#C89B3C]" />
              <div>
                <strong>Item 2: Telegram Intimation:</strong> Send <code>/today</code> to the Telegram bot; it returns today's health score (0-100) and betting advice.
              </div>
            </div>
            <div className="flex items-start gap-3">
              <input type="checkbox" defaultChecked readOnly className="mt-1 accent-[#C89B3C]" />
              <div>
                <strong>Item 3: System Splitter:</strong> Generate 15 legs on <code>/oracle</code>; verify Slip A payout strictly exceeds the session stake.
              </div>
            </div>
            <div className="flex items-start gap-3">
              <input type="checkbox" defaultChecked readOnly className="mt-1 accent-[#C89B3C]" />
              <div>
                <strong>Item 4: Handcrafted Placement:</strong> Confirm a booking code in <code>/bet-slip</code>; verify stake is debited and locked in Todar Mal's vault.
              </div>
            </div>
            <div className="flex items-start gap-3">
              <input type="checkbox" defaultChecked readOnly className="mt-1 accent-[#C89B3C]" />
              <div>
                <strong>Item 5: In-Play Evaluation:</strong> Verify cashout advice displays <code>HOLD</code>, <code>CASHOUT_ADVISED</code>, or <code>STOP_LOSS_BREACH</code>.
              </div>
            </div>
          </div>
        </section>

        {/* PART IV */}
        <footer className="pt-6 border-t-2 border-[#C89B3C] flex flex-col sm:flex-row justify-between items-start sm:items-end gap-6 text-xs text-stone-600">
          <div>
            <div className="font-bold text-stone-900 uppercase">Promulgated By Authority Of:</div>
            <div className="text-base font-bold text-[#C89B3C] mt-0.5">Amit Ashok Kumar Patnaik</div>
            <div>Chief Quantitative Architect & Founder · BetDoc Systems</div>
            <div className="text-stone-400 mt-1">Non-Custodial Quantitative Sovereign · Section 194BA Compliant</div>
          </div>

          <div className="sm:text-right border-2 border-[#C89B3C] bg-[#FFFDF9] p-3 rounded">
            <div className="font-bold text-[#C89B3C] uppercase tracking-wider text-[10px]">Official Gazette Seal</div>
            <div className="font-bold text-stone-900 text-sm mt-0.5">CERTIFIED OPERATIONAL</div>
            <div className="text-stone-400 text-[10px]">Group 116 Capstone · 23-Oct-2026</div>
          </div>
        </footer>
      </div>
    </div>
  );
};

export default TestingManual;
