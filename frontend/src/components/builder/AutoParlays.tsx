/**
 * 5(a) Automatic parlays (Group 104, point b.5): nothing to pick. Ashoka scans every quoted fixture across the sports,
 * prices each candidate slip over 10,000 Monte Carlo paths, drops correlated combinations, and keeps the slips that
 * clear the joint EV and joint probability bars; here they are grouped into singles, parlays and system bets. Load
 * one into the workbench to see its BetDoc Slip Score at your book. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { AUTO_GROUPS, groupSlips, slipSports } from "../../lib/builder";
import type { Slip, SlipsPayload } from "../../lib/oracle";
import { formatINR } from "../../lib/format";

const SPORT_LABEL: Record<string, string> = {
  cricket: "Cricket", soccer: "Football", tennis: "Tennis", basketball: "Basketball", icehockey: "Ice hockey", baseball: "Baseball",
  americanfootball: "American football", mma: "MMA", boxing: "Boxing", esports: "Esports",
};
const PRIORITY = ["cricket", "soccer"];
const pct = (p: number, digits = 1) => `${(p * 100).toFixed(digits)}%`;

export const AutoParlays = ({ payload, onLoad }: { payload: SlipsPayload | undefined; onLoad: (slip: Slip) => void }) => {
  const [sport, setSport] = useState<string | null>(null);
  if (!payload) return <p className="text-xs" style={{ color: "var(--wb-muted)" }}>Waiting for Ashoka's slips (the live market lives in Redis).</p>;
  const sports = [...new Set(payload.slips.flatMap((s) => [...slipSports(s)]))].sort((a, b) => (PRIORITY.indexOf(a) + 1 || 99) - (PRIORITY.indexOf(b) + 1 || 99));
  const groups = groupSlips(payload.slips, sport);
  return (
    <div className="flex flex-col gap-4">
      <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
        Automatic: {payload.scanned.toLocaleString("en-IN")} combinations scanned, {payload.simulated.toLocaleString("en-IN")} simulated over
        {" "}{payload.thresholds.paths.toLocaleString("en-IN")} paths. VETTED slips clear joint EV ≥ {pct(payload.thresholds.min_joint_ev)} and a win chance ≥ {pct(payload.thresholds.min_joint_probability, 0)};
        VALUE slips have positive joint EV below those bars and carry a strictly bounded stake.
        {Object.keys(payload.rejected).length > 0 && ` Rejected: ${Object.entries(payload.rejected).map(([why, n]) => `${why} (${n})`).join(", ")}.`}
      </p>
      <div className="flex gap-1.5 overflow-x-auto pb-1" role="tablist" aria-label="Sport">
        {[null, ...sports].map((s) => (
          <button key={s ?? "all"} type="button" role="tab" aria-selected={sport === s} onClick={() => setSport(s)}
                  className="shrink-0 rounded-full px-3 py-1 text-xs font-semibold"
                  style={sport === s ? { background: "var(--wb-primary)", color: "var(--wb-on-primary)" } : { background: "var(--wb-card)" }}>
            {s ? SPORT_LABEL[s] ?? s : "All sports"}
          </button>
        ))}
      </div>
      {AUTO_GROUPS.map((g) => (
        <section key={g.id} aria-label={g.label} className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="text-sm font-bold" style={{ color: "var(--wb-primary)" }}>{g.label} · {groups[g.id].length}</h3>
            <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>{g.note}</span>
          </div>
          {groups[g.id].length === 0 ? (
            <p className="rounded-xl p-3 text-xs" style={{ background: "var(--wb-card)", color: "var(--wb-muted)" }}>None clears the bars right now.</p>
          ) : (
            <ul className="grid grid-cols-1 gap-2 md:grid-cols-2 2xl:grid-cols-3">
              {groups[g.id].map((slip) => (
                <li key={slip.slip_id} className="flex flex-col gap-2 rounded-2xl p-3" style={{ background: "var(--wb-card)" }}>
                  <div className="flex flex-wrap items-center justify-between gap-1">
                    <span className="text-xs font-black">{slip.title}</span>
                    <span className="rounded-full px-1.5 py-0.5 text-[10px] font-bold" style={{ border: "1px solid var(--wb-primary)" }}>{slip.tier}</span>
                  </div>
                  <ul className="flex flex-col gap-1 text-xs">
                    {slip.legs.map((l) => (
                      <li key={l.leg_id} className="flex items-center justify-between gap-2">
                        <span className="min-w-0">
                          <span className="block truncate font-semibold">{l.fixture}</span>
                          <span className="block truncate" style={{ color: "var(--wb-muted)" }}>{l.market}: {l.label}</span>
                        </span>
                        <span className="font-mono font-bold">{l.prices[slip.book]?.toFixed(2) ?? "—"}</span>
                      </li>
                    ))}
                  </ul>
                  <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
                    {slip.book} {slip.odds.toFixed(2)} · joint EV {slip.simulation.joint_ev >= 0 ? "+" : ""}{pct(slip.simulation.joint_ev)} · wins {pct(slip.simulation.joint_probability)}
                    {Number(slip.stake_inr) > 0 ? ` · stake ${formatINR(Number(slip.stake_inr))}` : " · fund the CFO account to size a stake"}
                  </p>
                  <button type="button" onClick={() => onLoad(slip)} className="rounded-xl py-1.5 text-xs font-black" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
                    Load into the workbench and rate it
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </div>
  );
};
