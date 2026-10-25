/**
 * The BetDoc Slip Score (Group 104, point b.5): the fortress's 15-pillar score with the badges the inspection earned,
 * 0-100, in four tiers (DIAMOND FORTRESS, GOLD GRADE, SILVER CAUTION, BRONZE / DANGER TRAP). Every badge is a
 * measurement (the book's margin, shared fixtures, the independence pillar, the scoreline sync, the Monte Carlo joint
 * EV) and shows its points; the sum is shown beside the fortress's own score. The auto-fix is offered when a pillar
 * vetoed the slip or a badge costs it points. Developed for Amit Ashok Kumar Patnaik.
 *
 * Group 131: the executive audit. With the slip's figures it reads as four plain numbers first (BetDoc's win chance,
 * the chance the book's odds imply, the joint EV, the pillars passed), then the badges and the advice. A figure the
 * inspection did not measure shows "—", never a default.
 */
import { type Inspection, TIER_STYLE } from "../../lib/backtest_and_parlay";
import { canAutoFix } from "../../lib/builder";

/** The slip's measured figures (an inspection's audit, or a served plate's). */
export interface SlipFigures {
  joint_probability: number | null;
  total_odds: string | number | null;
  joint_ev: number | null;
  pillars_passed: number | null;
}

interface Props {
  rating: Inspection["rating"];
  figures?: SlipFigures;
  onAutoFix?: () => void;
  fixing?: boolean;
}

const pct = (v: number | null | undefined) => (v === null || v === undefined || !Number.isFinite(v) ? "—" : `${(v * 100).toFixed(1)}%`);

const Figure = ({ label, value, color, hint }: { label: string; value: string; color?: string; hint: string }) => (
  <div className="rounded-xl p-2.5 text-center" style={{ background: "color-mix(in srgb, var(--wb-card) 55%, rgba(0,0,0,0.35))" }} title={hint}>
    <span className="block text-[10px] font-semibold uppercase tracking-wide" style={{ color: "var(--wb-muted)" }}>{label}</span>
    <span className="mt-0.5 block font-mono text-base font-bold tabular-nums" style={{ color: color ?? "inherit" }}>{value}</span>
  </div>
);

export const SlipScoreMeter = ({ rating, figures, onAutoFix, fixing = false }: Props) => {
  const tier = TIER_STYLE[rating.tier];
  const bonus = rating.badges.reduce((a, b) => a + b.points, 0);
  const fixable = canAutoFix(rating);
  const odds = figures?.total_odds !== null && figures?.total_odds !== undefined ? Number(figures.total_odds) : null;
  const implied = odds !== null && odds > 1 ? 1 / odds : null;
  const ev = figures?.joint_ev ?? null;
  const passed = figures?.pillars_passed ?? null;
  return (
    <section aria-label="BetDoc slip audit" className="flex flex-col gap-3 rounded-2xl p-4" style={{ border: `1px solid ${tier.color}`, background: "color-mix(in srgb, var(--wb-card) 85%, transparent)" }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-col">
          <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: "var(--wb-muted)" }}>BetDoc slip audit</span>
          <span className="text-base font-black" style={{ color: tier.color }}>{tier.label}</span>
          <span className="text-xs" style={{ color: "var(--wb-muted)" }}>{tier.meaning}</span>
        </div>
        <span className="font-mono tabular-nums" aria-label={`${Math.round(rating.score)} out of 100`}>
          <span className="text-4xl font-black" style={{ color: tier.color }}>{Math.round(rating.score)}</span>
          <span className="text-sm" style={{ color: "var(--wb-muted)" }}> / 100</span>
        </span>
      </div>
      <div className="h-2.5 overflow-hidden rounded-full" style={{ background: "color-mix(in srgb, var(--wb-muted) 25%, transparent)" }}>
        <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${Math.max(2, Math.min(100, rating.score))}%`, background: tier.color }} />
      </div>
      {figures && (
        <div className="grid grid-cols-2 gap-2">
          <Figure label="BetDoc win chance" value={pct(figures.joint_probability)} color="var(--wb-primary)" hint="The slip's joint win probability over the Monte Carlo paths" />
          <Figure label="Odds imply" value={pct(implied)} hint={odds ? `1 / ${odds.toFixed(2)}: the win chance the book's combined odds price in` : "No combined odds"} />
          <Figure label="Joint EV" value={ev === null ? "—" : `${ev >= 0 ? "+" : ""}${(ev * 100).toFixed(1)}%`} color={ev === null ? undefined : ev >= 0 ? "#22C55E" : "#FB7185"}
                  hint="Expected return on the stake at the book's prices" />
          <Figure label="Pillars passed" value={passed === null ? "—" : `${passed} / 15`} color={passed === 15 ? "#22C55E" : undefined} hint="The 15-pillar fortress at the book this slip is priced at" />
        </div>
      )}
      {rating.fortress_score !== null && (
        <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
          Fortress {Math.round(rating.fortress_score)} {bonus >= 0 ? "+" : "−"} badges {Math.abs(bonus)}
          {rating.capped ? " · held at the cap: a pillar vetoed this slip" : ""}
        </p>
      )}
      {rating.badges.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Badges">
          {rating.badges.map((b) => (
            <li key={b.code} title={b.detail} className="rounded-lg px-2 py-0.5 text-[11px] font-bold"
                style={b.positive ? { background: "color-mix(in srgb, #22C55E 18%, transparent)", color: "#22C55E" } : { background: "color-mix(in srgb, #F43F5E 18%, transparent)", color: "#FB7185" }}>
              {b.label} ({b.points > 0 ? "+" : ""}{b.points})
            </li>
          ))}
        </ul>
      )}
      <div className="rounded-xl p-2.5 text-xs leading-relaxed" style={{ background: "color-mix(in srgb, var(--wb-card) 55%, rgba(0,0,0,0.35))" }}>
        <p className="mb-1 text-[10px] font-bold uppercase tracking-wider" style={{ color: "var(--wb-muted)" }}>The audit's advice</p>
        {rating.advice.length ? (
          <ul className="flex flex-col gap-1">
            {rating.advice.slice(0, 3).map((a) => (
              <li key={a} className="flex items-start gap-1.5">
                <span className="material-symbols-outlined mt-px text-[14px]" aria-hidden="true" style={{ color: "var(--wb-primary)" }}>arrow_right</span>
                <span>{a}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p>{rating.tier === "DIAMOND" ? "Every enforced pillar passed and the badges back it: this is the slip the kitchen would serve." : "No pillar asks for a change; the score is what the evidence supports."}</p>
        )}
      </div>
      {onAutoFix && fixable && (
        <button type="button" onClick={onAutoFix} disabled={fixing}
                className="w-full rounded-xl py-2.5 text-sm font-black transition disabled:opacity-60" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
          {fixing ? "Trying the alternatives through the fortress…" : "✨ 1-click auto-fix my slip"}
        </button>
      )}
    </section>
  );
};
