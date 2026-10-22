/**
 * 5(c) The Chef's Table (Group 104, point b.5): BetDoc's own generator, served ready-made. Ashoka's best vetted slips
 * are re-rated through the 15-pillar fortress at the retail books into Slip Scores; only slips with positive joint EV
 * there and no vetoing pillar are served, as #1 the handcrafted parlay of the day, #2 the safe fortress and #3 the
 * high-yield special. Every leg has its recipe card: why it was picked (fair against implied), its scoreline link,
 * its stress test, and why the other slips were not served. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { type ChefPlate, refreshChefTable, type RecipeCard, useChefTable } from "../../lib/builder";
import { formatINR } from "../../lib/format";
import { invalidate, runMutation } from "../../lib/resource";
import { Async } from "../../ui/kit";
import { SlipScoreMeter } from "./SlipScoreMeter";

const pct = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`);

const Recipe = ({ card }: { card: RecipeCard }) => {
  const [open, setOpen] = useState(false);
  return (
    <li className="overflow-hidden rounded-xl" style={{ background: "color-mix(in srgb, var(--wb-card) 70%, transparent)" }}>
      <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)} className="flex w-full items-center justify-between gap-2 p-2.5 text-left text-xs">
        <span className="min-w-0">
          <span className="block truncate font-semibold">{card.fixture}</span>
          <span className="block truncate" style={{ color: "var(--wb-muted)" }}>{card.market}: {card.label} @ {card.price ?? "—"}{card.book ? ` (${card.book})` : ""}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {card.why.edge !== null && <span className="rounded-md px-1.5 font-bold" style={{ color: card.why.edge > 0 ? "#22C55E" : "#FB7185" }}>edge {card.why.edge >= 0 ? "+" : ""}{pct(card.why.edge)}</span>}
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{open ? "expand_less" : "expand_more"}</span>
        </span>
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t p-2.5 text-[11px] leading-relaxed" style={{ borderColor: "color-mix(in srgb, var(--wb-muted) 25%, transparent)" }}>
          <p><strong>1 · Why it was picked.</strong> {card.why.text}
            {Object.keys(card.why.models).length > 0 && ` The models: ${Object.entries(card.why.models).map(([m, p]) => `${m.replace("_", "-")} ${pct(p, 0)}`).join(", ")}.`}</p>
          <p><strong>2 · Score prediction.</strong>{" "}
            {card.scoreline
              ? `Likeliest score ${card.scoreline.likeliest_score} (${pct(card.scoreline.likeliest_probability)}): this leg ${card.scoreline.result_at_likeliest.toLowerCase().replace("_", " ")} there; ${pct(card.scoreline.leg_probability)} to pay over every scoreline of the market-fitted Dixon-Coles matrix.`
              : "No scoreline model for this market (the scoreline fit covers three-way football)."}</p>
          <div><strong>3 · Stress test.</strong>{" "}
            {card.stress_test.length === 0 ? "Needs a scoreline model; none for this leg." : (
              <ul className="ml-4 mt-1 list-disc">
                {card.stress_test.map((s) => <li key={s.code}>{s.scenario} ({pct(s.probability)} likely): pays {pct(s.leg_pays)}</li>)}
              </ul>
            )}
          </div>
        </div>
      )}
    </li>
  );
};

const Plate = ({ plate, onLoad }: { plate: ChefPlate; onLoad: (plate: ChefPlate) => void }) => (
  <li className="flex flex-col gap-3 rounded-2xl p-3" style={{ background: "var(--wb-card)" }}>
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-base font-black" style={{ color: "var(--wb-primary)" }}>{plate.title}</h3>
      <span className="text-[11px]" style={{ color: "var(--wb-muted)" }}>{plate.slip_title} · {plate.kind.toLowerCase().replace("_", " ")}</span>
    </div>
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <SlipScoreMeter rating={plate.rating} />
      <div className="flex flex-col gap-2">
        <p className="text-xs">
          Odds <strong className="font-mono">{plate.odds}</strong>{plate.book ? ` at ${plate.book}` : ""} · joint EV {plate.joint_ev !== null && plate.joint_ev >= 0 ? "+" : ""}{pct(plate.joint_ev)}
          {" "}· wins {pct(plate.joint_probability)} · {plate.pillars_passed}/15 pillars{plate.is_vetted ? " · vetted" : ""}
          {" "}· {Number(plate.stake_inr) > 0 ? `KUMBHA's stake ${formatINR(Number(plate.stake_inr))}` : "no stake sized (fund the CFO account)"}
        </p>
        <ul className="flex flex-col gap-1.5" aria-label="Recipe cards">{plate.recipe.map((c) => <Recipe key={c.leg_id} card={c} />)}</ul>
        <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
          <strong>4 · Why not the others.</strong>{" "}
          {plate.outscored.length ? `It out-scored ${plate.outscored.length} other re-rated slip${plate.outscored.length === 1 ? "" : "s"} (${plate.outscored.slice(0, 4).map((o) => Math.round(o.score)).join(", ")}${plate.outscored.length > 4 ? "…" : ""}).` : "It was the only one of its kind to clear the kitchen."}
        </p>
        <button type="button" onClick={() => onLoad(plate)} className="self-start rounded-xl px-4 py-2 text-xs font-black" style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
          Load into the workbench
        </button>
      </div>
    </div>
  </li>
);

export const ChefsTable = ({ onLoad }: { onLoad: (plate: ChefPlate) => void }) => {
  const table = useChefTable(true);
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    await runMutation(refreshChefTable, { errorTitle: "The kitchen could not re-rate", success: "Served fresh" });
    invalidate("oracle:chef-table");
    setBusy(false);
  };
  return (
    <Async resource={table} skeletonRows={4}>
      {(t) => (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]" style={{ color: "var(--wb-muted)" }}>
            <span>Automatic: {t.rated} of {t.considered} best vetted slips re-rated through the fortress · served {new Date(t.generated_at).toLocaleTimeString()}</span>
            <button type="button" onClick={() => void refresh()} disabled={busy} className="underline disabled:opacity-50">{busy ? "Re-rating…" : "Re-rate now"}</button>
          </div>
          {t.plates.length === 0 ? (
            <p className="rounded-xl p-4 text-xs" style={{ background: "var(--wb-card)" }}>
              Nothing passes the kitchen right now: no vetted slip has positive joint EV at the retail books with no pillar vetoing it. The kitchen serves nothing rather than a loser.
            </p>
          ) : (
            <ul className="flex flex-col gap-3">{t.plates.map((p) => <Plate key={p.code} plate={p} onLoad={onLoad} />)}</ul>
          )}
          <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
            Why the rest were not served: Ashoka vets slips at joint EV ≥ {pct(t.thresholds.min_joint_ev)} and a win chance ≥ {pct(t.thresholds.min_joint_probability, 0)} over
            {" "}{t.thresholds.paths?.toLocaleString("en-IN")} paths (positive-EV slips below those bars stay as value slips){t.scanned ? `; ${t.scanned.toLocaleString("en-IN")} combinations scanned` : ""}
            {Object.keys(t.rejected).length > 0 ? `; rejected ${Object.entries(t.rejected).map(([why, n]) => `${why} (${n})`).join(", ")}` : ""}.
          </p>
        </div>
      )}
    </Async>
  );
};
