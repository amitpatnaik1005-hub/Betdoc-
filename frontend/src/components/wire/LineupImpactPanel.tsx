/**
 * Absences and what they cost (backend `lineup_impact.py`). Developed for Amit Ashok Kumar Patnaik.
 *
 * ESPN's injury lists arrive on their own; the player's worth is the operator's rating (0 .. scale), which carries
 * to the player's later absences. Until every OUT / SUSPENDED / DOUBTFUL absence of a fixture is rated, its injury
 * evidence is not written and the twin's injury pillar stays unverified.
 */
import { useMemo, useState } from "react";
import { formatTime } from "../../lib/format";
import { type AbsenceRow, rateAbsence, useLineup, type WireFixture } from "../../lib/the_wire";
import { useAuthStore } from "../../store/useAuthStore";
import { Async, Button, EmptyState, NumberInput, Pill, Select, num } from "../../ui/kit";

const STATUS_TONE = { OUT: "critical", SUSPENDED: "critical", DOUBTFUL: "warning", QUESTIONABLE: "info" } as const;
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);

const RateCell = ({ row, scale, onDone }: { row: AbsenceRow; scale: number; onDone: (message: string) => void }) => {
  const [value, setValue] = useState(row.rating === null ? "" : String(row.rating));
  const [busy, setBusy] = useState(false);
  const rating = num(value);
  const valid = Number.isFinite(rating) && rating >= 0 && rating <= scale;
  return (
    <span className="flex items-center gap-1.5">
      <NumberInput value={value} min={0} max={scale} step={0.5} onChange={(e) => setValue(e.target.value)} className="w-20" aria-label={`Rating for ${row.player}`} />
      <Button size="sm" busy={busy} disabled={!valid || rating === row.rating} onClick={async () => {
        setBusy(true);
        try {
          const res = await rateAbsence(row.id, rating);
          onDone(res.injury_section.written ? "Injury evidence written for the fixture" : `Still unrated: ${(res.injury_section.unrated ?? []).join(", ") || res.injury_section.reason}`);
        } catch (err) {
          onDone(err instanceof Error ? err.message : "Rating failed");
        } finally {
          setBusy(false);
        }
      }}>Rate</Button>
    </span>
  );
};

export const LineupImpactPanel = ({ fixtures }: { fixtures: WireFixture[] }) => {
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const options = useMemo(() => [...fixtures].sort((a, b) => b.absences - a.absences || a.kickoff.localeCompare(b.kickoff)), [fixtures]);
  const [chosen, setChosen] = useState<string | null>(null);
  const fixtureId = chosen ?? options[0]?.fixture_id ?? null;
  const lineup = useLineup(fixtureId);
  const [note, setNote] = useState<string | null>(null);
  if (options.length === 0) return <EmptyState icon="personal_injury" title="No tracked fixtures" detail="Fixtures appear once the odds board carries them." />;
  return (
    <div className="flex flex-col gap-3">
      <Select value={fixtureId ?? ""} onChange={(e) => { setChosen(e.target.value); setNote(null); }} aria-label="Fixture">
        {options.map((f) => (
          <option key={f.fixture_id} value={f.fixture_id}>{f.home} v {f.away} · {formatTime(f.kickoff)}{f.absences ? ` · ${f.absences} absent` : ""}</option>
        ))}
      </Select>
      <Async resource={lineup} skeletonRows={3}>
        {(v) => (
          <>
            <div className="grid grid-cols-2 gap-2 text-xs">
              {(["HOME", "AWAY"] as const).map((side) => (
                <div key={side} className="rounded-xl bg-stone-50 p-2.5 dark:bg-white/[0.03]">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-stone-700 dark:text-stone-200">{side === "HOME" ? v.home : v.away}</span>
                    <Pill tone={v.delta[side] < -0.02 ? "critical" : "neutral"}>{(v.delta[side] * 100).toFixed(1)} pts</Pill>
                  </div>
                  {v.probabilities && v.adjusted && (
                    <p className="mt-1 font-mono text-[11px] text-stone-500">win {pct(v.probabilities[side])} → {pct(v.adjusted[side])}</p>
                  )}
                </div>
              ))}
            </div>
            {v.absences.length === 0 ? (
              <EmptyState icon="health_and_safety" title="No absences recorded" detail="ESPN lists injuries for the NFL, NBA, MLB and NHL; an administrator adds any other." />
            ) : (
              <ul className="flex flex-col gap-1.5">
                {v.absences.map((a) => (
                  <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-stone-50 px-3 py-2 text-xs dark:bg-white/[0.03]">
                    <span className="min-w-0">
                      <span className="font-semibold text-stone-700 dark:text-stone-200">{a.player}</span>
                      <span className="ml-1 text-stone-400">{a.team} · {a.position ?? "—"} (λ {a.position_weight}){a.nature ? ` · ${a.nature}` : ""}</span>
                    </span>
                    <span className="flex items-center gap-2">
                      <Pill tone={STATUS_TONE[a.status]}>{a.status}</Pill>
                      {a.cost !== null && <span className="font-mono text-stone-500">−{(a.cost * 100).toFixed(1)} pts</span>}
                      {isAdmin ? <RateCell row={a} scale={v.rating_scale} onDone={(m) => { setNote(m); void lineup.refresh(); }} />
                        : <span className="font-mono">{a.rating === null ? "unrated" : `${a.rating}/${v.rating_scale}`}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {v.unrated.length > 0 && <p className="text-[11px] text-amber-600 dark:text-amber-300">Unrated, so counted in nothing: {v.unrated.join(", ")}.</p>}
            {note && <p className="text-[11px] text-stone-500">{note}</p>}
          </>
        )}
      </Async>
    </div>
  );
};
