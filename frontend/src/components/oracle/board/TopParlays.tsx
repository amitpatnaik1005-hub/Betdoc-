/**
 * Top parlays on the match board (Group 103, point b.11 screenshot 3): Ashoka's vetted slips (10,000 Monte Carlo
 * paths, the anti-correlation gate, joint EV and joint probability thresholds), best first, each one click into the
 * slip. The 15-pillar slip score is the fortress's, given when the slip is inspected, so it is not shown before.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import type { Slip } from "../../../lib/oracle";
import { priceAt } from "../../../store/useParlaySlipStore";

interface Props {
  slips: Slip[];
  book: string | null;
  inSlip: (legId: string) => boolean;
  onAdd: (slip: Slip) => void;
  title?: string;
}

const pct = (p: number, digits = 1) => `${(p * 100).toFixed(digits)}%`;

export const TopParlays = ({ slips, book, inSlip, onAdd, title = "Top parlays" }: Props) => (
  <section aria-label={title} className="flex flex-col gap-2">
    <h3 className="text-[11px] font-bold uppercase tracking-wider" style={{ color: "var(--wb-muted)" }}>{title}</h3>
    <ul className="grid grid-cols-1 gap-2 xl:grid-cols-2">
      {slips.map((slip, i) => {
        const prices = slip.legs.map((l) => priceAt(l, book));
        const odds = prices.every((p) => p !== null) ? prices.reduce<number>((a, p) => a * (p as number), 1) : null;
        const held = slip.legs.every((l) => inSlip(l.leg_id));
        return (
          <li key={slip.slip_id} className="flex flex-col gap-2 rounded-2xl p-3" style={{ background: "var(--wb-card)" }}>
            <div className="flex flex-wrap items-center justify-between gap-1">
              <span className="text-sm font-black" style={{ color: "var(--wb-primary)" }}>#{i + 1} {slip.title}</span>
              <span className="flex gap-1 text-[10px] font-bold">
                <span className="rounded-full px-1.5 py-0.5" style={{ background: "color-mix(in srgb, #22C55E 20%, transparent)", color: "#22C55E" }}>EV {slip.simulation.joint_ev >= 0 ? "+" : ""}{pct(slip.simulation.joint_ev)}</span>
                <span className="rounded-full px-1.5 py-0.5" style={{ border: "1px solid var(--wb-primary)" }}>{slip.tier}</span>
              </span>
            </div>
            <ul className="flex flex-col gap-1 text-xs">
              {slip.legs.map((l, j) => (
                <li key={l.leg_id} className="flex items-center justify-between gap-2">
                  <span className="min-w-0">
                    <span className="block truncate font-semibold">{l.fixture}</span>
                    <span className="block truncate" style={{ color: "var(--wb-muted)" }}>{l.market}: {l.label}</span>
                  </span>
                  <span className="font-mono font-bold">{prices[j]?.toFixed(2) ?? "—"}</span>
                </li>
              ))}
            </ul>
            <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
              Joint win chance {pct(slip.simulation.joint_probability)} over {slip.simulation.paths.toLocaleString("en-IN")} simulated paths · {slip.badge}
            </p>
            <div className="flex items-center justify-between">
              <span className="text-xs">Odds <strong className="font-mono">{odds ? odds.toFixed(2) : "not all quoted here"}</strong></span>
              <button type="button" disabled={held} onClick={() => onAdd(slip)}
                      className="rounded-xl px-3 py-1.5 text-xs font-black disabled:opacity-50" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
                {held ? "In the slip" : "+ Add to betslip"}
              </button>
            </div>
          </li>
        );
      })}
    </ul>
  </section>
);
