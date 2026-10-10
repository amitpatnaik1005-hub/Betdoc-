/**
 * Referee tendencies from officiating records, shrunk to the league's mean (backend `referee_bias.py`).
 * Nothing is seeded: a referee nobody has recorded has no profile. ESPN does not name football referees, so an
 * administrator names a fixture's referee here. Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { assignReferee, type RefereeProfileRow, useReferees, type WireFixture } from "../../lib/the_wire";
import { useAuthStore } from "../../store/useAuthStore";
import { Async, Button, DataTable, EmptyState, Pill, Select, TextInput } from "../../ui/kit";

const COLUMNS = [
  { key: "ref", header: "Referee", render: (r: RefereeProfileRow) => <span className="font-semibold">{r.referee}</span> },
  { key: "league", header: "League", render: (r: RefereeProfileRow) => <span className="text-xs text-stone-500">{r.league}</span> },
  { key: "n", header: "Matches", align: "right" as const, render: (r: RefereeProfileRow) => <span className="font-mono">{r.matches}</span> },
  { key: "cards", header: "Cards / game", align: "right" as const, render: (r: RefereeProfileRow) => <span className="font-mono">{r.cards_per_game.toFixed(2)}</span> },
  { key: "pens", header: "Pens / 90", align: "right" as const, render: (r: RefereeProfileRow) => <span className="font-mono">{r.penalties_per_90.toFixed(2)}</span> },
  { key: "bias", header: "Away/home cards", align: "right" as const, render: (r: RefereeProfileRow) => <span className="font-mono">{r.home_bias_ratio.toFixed(2)}</span> },
  { key: "over", header: "Over 2.5", align: "right" as const, render: (r: RefereeProfileRow) => <span className="font-mono">{r.over_totals_pct.toFixed(0)}%</span> },
  { key: "ready", header: "Fortress", render: (r: RefereeProfileRow) => <Pill tone={r.fortress_ready ? "good" : "neutral"}>{r.fortress_ready ? "feeds pillar 9" : "too few"}</Pill> },
];

export const RefereeBoard = ({ fixtures }: { fixtures: WireFixture[] }) => {
  const referees = useReferees();
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const football = fixtures.filter((f) => (f.sport_key ?? "").startsWith("soccer"));
  const [fixtureId, setFixtureId] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-3">
      <Async resource={referees} isEmpty={(r) => r.profiles.length === 0}
             empty={<EmptyState icon="sports" title="No referee profiles yet" detail="Profiles grow from finished matches whose referee is named (by ESPN or here) and from imported records." />}>
        {(r) => <DataTable columns={COLUMNS} rows={r.profiles} rowKey={(p) => `${p.league}|${p.referee}`} dense />}
      </Async>
      {isAdmin && football.length > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          <Select value={fixtureId} onChange={(e) => setFixtureId(e.target.value)} aria-label="Fixture" className="min-w-[14rem] flex-1">
            <option value="">Name a fixture's referee…</option>
            {football.map((f) => <option key={f.fixture_id} value={f.fixture_id}>{f.home} v {f.away}{f.referee ? ` · ${f.referee}` : ""}</option>)}
          </Select>
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="Referee" aria-label="Referee name" className="w-48" />
          <Button icon="how_to_reg" busy={busy} disabled={!fixtureId || name.trim().length < 2} onClick={async () => {
            setBusy(true);
            try {
              const res = await assignReferee(fixtureId, name.trim());
              setNote(res.referee_section_written ? "Referee evidence written for the fixture" : "Assigned; too few recorded matches to feed the fortress yet");
              void referees.refresh();
            } catch (err) {
              setNote(err instanceof Error ? err.message : "Assignment failed");
            } finally {
              setBusy(false);
            }
          }}>Assign</Button>
        </div>
      )}
      {note && <p className="text-[11px] text-stone-500">{note}</p>}
    </div>
  );
};
