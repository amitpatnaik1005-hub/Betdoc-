/**
 * The BetDoc Slip Score (Group 104, point b.5): the fortress's 15-pillar score with the badges the inspection earned,
 * 0-100, in four tiers (DIAMOND FORTRESS, GOLD GRADE, SILVER CAUTION, BRONZE / DANGER TRAP). Every badge is a
 * measurement (the book's margin, shared fixtures, the independence pillar, the scoreline sync, the Monte Carlo joint
 * EV) and shows its points; the sum is shown beside the fortress's own score. The auto-fix is offered when a pillar
 * vetoed the slip or a badge costs it points. Developed for Amit Ashok Kumar Patnaik.
 */
import { type Inspection, TIER_STYLE } from "../../lib/backtest_and_parlay";
import { canAutoFix } from "../../lib/builder";

interface Props {
  rating: Inspection["rating"];
  onAutoFix?: () => void;
  fixing?: boolean;
}

export const SlipScoreMeter = ({ rating, onAutoFix, fixing = false }: Props) => {
  const tier = TIER_STYLE[rating.tier];
  const bonus = rating.badges.reduce((a, b) => a + b.points, 0);
  const fixable = canAutoFix(rating);
  return (
    <section aria-label="BetDoc Slip Score" className="flex flex-col gap-2.5 rounded-2xl p-3" style={{ border: `1px solid ${tier.color}`, boxShadow: tier.glow === "none" ? undefined : tier.glow }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-col">
          <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: "var(--wb-muted)" }}>BetDoc Slip Score</span>
          <span className="text-sm font-black" style={{ color: tier.color }}>{tier.label}</span>
          <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>{tier.meaning}</span>
        </div>
        <span className="font-mono tabular-nums" aria-label={`${Math.round(rating.score)} out of 100`}>
          <span className="text-3xl font-black" style={{ color: tier.color }}>{Math.round(rating.score)}</span>
          <span className="text-xs" style={{ color: "var(--wb-muted)" }}> / 100</span>
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full" style={{ background: "color-mix(in srgb, var(--wb-muted) 25%, transparent)" }}>
        <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${Math.max(2, Math.min(100, rating.score))}%`, background: tier.color }} />
      </div>
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
      <p className="rounded-xl p-2 text-[11px] leading-relaxed" style={{ background: "color-mix(in srgb, var(--wb-card) 80%, transparent)" }}>
        <strong>Chef's take: </strong>
        {rating.advice[0] ?? (rating.tier === "DIAMOND" ? "Every enforced pillar passed and the badges back it: this is the slip the kitchen would serve." : "No pillar asks for a change; the score is what the evidence supports.")}
      </p>
      {onAutoFix && fixable && (
        <button type="button" onClick={onAutoFix} disabled={fixing}
                className="w-full rounded-xl py-2 text-xs font-black transition disabled:opacity-60" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
          {fixing ? "Trying the alternatives through the fortress…" : "✨ 1-click auto-fix my slip"}
        </button>
      )}
    </section>
  );
};
