/**
 * Limited-overs innings projection of BETDOC SCORE PREDICTION (Group 102). Developed for Amit Ashok Kumar Patnaik.
 *
 * From the ball-by-ball Markov engine: the first innings' median and central interval, its powerplay, the expected
 * runs of each phase (powerplay / middle / death overs), and the chase, against a stated target or against the whole
 * first-innings distribution. Dew, pitch wear and The Wire's weather are already inside the numbers; the card says
 * which applied.
 */
import { formatRatioPct } from "../../lib/format";
import type { CricketPrediction, InningsProjection } from "../../lib/prediction";
import { KeyValues, Meter, Pill } from "../../ui/kit";

const PHASES: { key: "powerplay" | "middle" | "death"; label: (overs: number) => string }[] = [
  { key: "powerplay", label: (o) => (o === 50 ? "Powerplay (1-10)" : "Powerplay (1-6)") },
  { key: "middle", label: (o) => (o === 50 ? "Middle (11-40)" : "Middle (7-15)") },
  { key: "death", label: (o) => (o === 50 ? "Death (41-50)" : "Death (16-20)") },
];

const Innings = ({ title, innings, overs, coverage }: { title: string; innings: InningsProjection; overs: number; coverage: number }) => (
  <div className="flex min-w-0 flex-col gap-3 rounded-2xl bg-stone-900/[0.03] p-4 dark:bg-white/[0.04]">
    <div className="flex items-baseline justify-between gap-2">
      <p className="text-xs font-medium text-stone-500 dark:text-stone-400">{title}</p>
      <p className="text-right">
        <span className="font-mono text-2xl font-bold tabular-nums text-stone-900 dark:text-stone-50">{innings.median}</span>
        <span className="ml-1 text-[11px] text-stone-500">median</span>
      </p>
    </div>
    <KeyValues
      items={[
        { label: `${Math.round(coverage * 100)}% range`, value: <span className="font-mono tabular-nums">{innings.interval[0]} – {innings.interval[1]}</span> },
        ...(innings.powerplay_median !== null && innings.powerplay_interval
          ? [{ label: "Powerplay", value: <span className="font-mono tabular-nums">{innings.powerplay_median} ({innings.powerplay_interval[0]} – {innings.powerplay_interval[1]})</span> }]
          : []),
        ...PHASES.filter((ph) => innings.phase_expected_runs[ph.key] !== undefined).map((ph) => ({
          label: `${ph.label(overs)} · expected`,
          value: <span className="font-mono tabular-nums">{innings.phase_expected_runs[ph.key]?.toFixed(1)}</span>,
        })),
        { label: "Expected sixes", value: <span className="font-mono tabular-nums">{innings.expected_sixes.toFixed(1)}</span> },
      ]}
    />
  </div>
);

export const CricketInningsCard = ({ prediction, homeLabel, awayLabel }: { prediction: CricketPrediction; homeLabel: string; awayLabel: string }) => {
  const p = prediction;
  const name = (side: "home" | "away") => (side === "home" ? homeLabel : awayLabel);
  const chasing = p.batting_first === "home" ? "away" : "home";
  const dew = p.conditions.dew_impact_applied === true;
  const c = p.chase;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone="neutral" icon="sports_cricket">{p.format.toUpperCase()} · {p.overs} overs</Pill>
        {dew ? (
          <Pill tone="info" icon="water_drop">Dew: {String(p.conditions.dew_intensity)} · chase {Number(p.conditions.dew_runs_shift ?? 0) >= 0 ? "+" : ""}{Number(p.conditions.dew_runs_shift ?? 0).toFixed(1)} runs</Pill>
        ) : (
          <Pill tone="neutral" icon="wb_sunny">No dew applied</Pill>
        )}
        {Number(p.conditions.pitch_wear ?? 0) > 0 && <Pill tone="warning" icon="grass">Pitch wear {Number(p.conditions.pitch_wear).toFixed(2)}</Pill>}
        {p.intel_sections.length > 0 && <Pill tone="good" icon="satellite_alt">Intel: {p.intel_sections.join(", ")}</Pill>}
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <Innings title={`1st innings · ${name(p.batting_first)} bat first`} innings={p.first_innings} overs={p.overs} coverage={p.coverage} />
        <Innings title={`${name(chasing)} · full innings if not chasing`} innings={p.second_innings} overs={p.overs} coverage={p.coverage} />
      </div>
      <div className="flex flex-col gap-2 rounded-2xl border border-stone-900/[0.06] p-4 dark:border-white/[0.08]">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
          <span className="font-medium text-stone-800 dark:text-stone-100">
            {name(chasing)} chasing {c.target !== null ? `${c.target} to win` : `${name(p.batting_first)}'s total`}
          </span>
          <span className="font-mono font-bold tabular-nums text-stone-900 dark:text-stone-50">{formatRatioPct(c.win)} reach it</span>
        </div>
        <Meter value={c.win} tone="accent" label={`chance ${name(chasing)} completes the chase`} />
        <p className="text-[11px] text-stone-500 dark:text-stone-400">
          Tie {formatRatioPct(c.tie, 2)}{c.win_with_super_over !== null ? ` · with a super over (a coin flip): ${formatRatioPct(c.win_with_super_over)}` : ""} · falls short {formatRatioPct(c.lose)}.
          {c.source === "stated" ? " Solved exactly ball by ball against your target." : " Against the whole first-innings distribution."}
        </p>
      </div>
      <p className="text-[11px] text-stone-500 dark:text-stone-400">
        Match winner (sub-model consensus): {homeLabel} {formatRatioPct(p.match_winner.home)} · {awayLabel} {formatRatioPct(p.match_winner.away)}
        {p.match_winner.tie > 0 ? ` · tie ${formatRatioPct(p.match_winner.tie)}` : ""}.
      </p>
    </div>
  );
};
