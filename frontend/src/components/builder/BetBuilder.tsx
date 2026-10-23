/**
 * The BetDoc Bet Builder (Group 104, point b.5): three modes on The Oracle, in the skin of the book you bet from.
 *
 * - 5(a) Auto parlays: fully automatic (Ashoka's vetted singles, parlays and system bets).
 * - 5(b) Manual workbench: the only place legs are picked by hand; one leg is a single, two or more a parlay; rated by
 *   the BetDoc Slip Score with the 1-click auto-fix, then recorded and watched by the stop-loss shield.
 * - 5(c) The Chef's Table: fully automatic (three plates re-rated through the fortress, with recipe cards).
 * - 5(d) Street-smart & system splitter (Group 115): a stake -> return target as a cross-sport slip of the board's anchors,
 *   split into Fortress, Growth and Moonshot tickets placed by hand, with TODAR MAL's market health for the day.
 *
 * The automatic modes load into the same slip the workbench and the match board share. Developed for Amit Ashok Kumar
 * Patnaik.
 */
import type { CSSProperties } from "react";
import { useSearchParams } from "react-router-dom";
import { BOOKMAKER_THEMES } from "../../lib/backtest_and_parlay";
import { type BuilderMode, type ChefPlate, toSlipLeg } from "../../lib/builder";
import type { Slip, SlipsPayload } from "../../lib/oracle";
import { useParlaySlipStore } from "../../store/useParlaySlipStore";
import { toast } from "../../store/useToastStore";
import { Panel, Segmented } from "../../ui/kit";
import { ManualParlayWorkbench } from "../parlay/ManualParlayWorkbench";
import { SmallcaseSlipRebalancer } from "../smallcase/SmallcaseSlipRebalancer";
import { AutoParlays } from "./AutoParlays";
import { ChefsTable } from "./ChefsTable";
import { StreetSmartSynthesizer } from "./StreetSmartSynthesizer";

const MODES: { value: BuilderMode; label: string; icon: string }[] = [
  { value: "auto", label: "5(a) Auto parlays", icon: "smart_toy" },
  { value: "manual", label: "5(b) Manual workbench", icon: "construction" },
  { value: "chef", label: "5(c) The Chef's Table", icon: "restaurant" },
  { value: "synthesizer", label: "5(d) Street-smart & splitter", icon: "auto_awesome" },
];
const NOTE: Record<BuilderMode, string> = {
  auto: "Fully automatic: Ashoka builds and vets every slip; you only choose which to load.",
  manual: "The only manual mode: pick legs on the match board or below; one leg is a single, two or more a parlay.",
  chef: "Fully automatic: the best vetted slips re-rated through the fortress and served three ways, each leg with its recipe card.",
  synthesizer: "Your stake and target: the board's anchors across sports, split into three tickets you place by hand, with today's market health.",
};
const isMode = (v: string | null): v is BuilderMode => v === "auto" || v === "manual" || v === "chef" || v === "synthesizer";

export const BetBuilder = ({ slips }: { slips: SlipsPayload | undefined }) => {
  const [params, setParams] = useSearchParams();
  const raw = params.get("mode");
  const mode: BuilderMode = isMode(raw) ? raw : "manual";
  const setMode = (next: BuilderMode) => setParams((prev) => { const p = new URLSearchParams(prev); p.set("mode", next); return p; }, { replace: true });
  const skin = useParlaySlipStore((s) => s.skin);
  const clear = useParlaySlipStore((s) => s.clear);
  const addMany = useParlaySlipStore((s) => s.addMany);
  const theme = BOOKMAKER_THEMES[skin];
  const style = { "--wb-primary": theme.primary, "--wb-on-primary": theme.onPrimary, "--wb-card": theme.card, "--wb-muted": theme.muted,
                  background: theme.surface, color: theme.text, fontFamily: theme.font } as CSSProperties;

  const load = (title: string, legs: Pick<Slip["legs"][number], "leg_id" | "fixture" | "label" | "market" | "prices" | "fair_probability">[]) => {
    clear();
    addMany(legs.map((l) => toSlipLeg(l as Slip["legs"][number])));
    setMode("manual");
    toast.success(`${title} is in the workbench`, "Press Inspect for its Slip Score at your book.");
    window.setTimeout(() => document.getElementById("parlay-workbench")?.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
  };

  return (
    <Panel title="Bet builder" icon="construction" className="lg:col-span-12" subtitle={NOTE[mode]}
           actions={<Segmented label="Bet builder mode" size="sm" value={mode} onChange={setMode} options={MODES} />}>
      {mode === "manual" ? (
        <div className="flex flex-col gap-6">
          <ManualParlayWorkbench embedded />
          {/* Group 107: the same slip as a smallcase of singles, sized by KUMBHA */}
          <SmallcaseSlipRebalancer />
        </div>
      ) : (
        <div className="rounded-3xl p-4 transition-colors duration-300" style={style}>
          {mode === "auto"
            ? <AutoParlays payload={slips} onLoad={(slip) => load(slip.title, slip.legs)} />
            : mode === "chef"
              ? <ChefsTable onLoad={(plate: ChefPlate) => load(plate.title, plate.legs)} />
              : <StreetSmartSynthesizer onLoad={load} />}
        </div>
      )}
    </Panel>
  );
};
