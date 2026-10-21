/**
 * BETDOC SCORE PREDICTION on The Lab (Group 102, point b.16). Developed for Amit Ashok Kumar Patnaik.
 *
 * Football: a fixture the pre-match store holds (the scorelines its de-vigged prices imply, no provider call) or your
 * own expected goals. Cricket: limited-overs innings from the ball-by-ball Markov engine on the sides' ratings (100 is
 * an average side), the conditions (night match and dew, pitch wear) and an optional chase target. Score-backed parlay
 * legs live on The Oracle's workbench, where the board's own prices are fitted and its legs can be added.
 */
import { type FormEvent, useState } from "react";
import { formatDateTime, formatRatioPct } from "../../lib/format";
import { usePreMatchFixtures } from "../../lib/pre_match";
import { type CricketPrediction, type FootballPrediction, predictCricket, predictFootballFromXg, predictStoredFixture } from "../../lib/prediction";
import { runMutation } from "../../lib/resource";
import { Button, EmptyState, Field, KeyValues, num, NumberInput, Panel, Segmented, Select, TextInput, Toggle } from "../../ui/kit";
import { CricketInningsCard } from "./CricketInningsCard";
import { FootballScoreHeatmap } from "./FootballScoreHeatmap";

type Sport = "football" | "cricket";
type FootballMode = "stored" | "xg";

const FootballSummary = ({ p, home, away }: { p: FootballPrediction; home: string; away: string }) => {
  const ah = (line: string) => p.markets.find((m) => m.market === `Asian Handicap ${line}` && m.selection === "HOME");
  return (
    <KeyValues
      items={[
        { label: "1X2", value: `${home} ${formatRatioPct(p.home_win)} · draw ${formatRatioPct(p.draw)} · ${away} ${formatRatioPct(p.away_win)}` },
        { label: "Expected goals", value: `${p.expected_goals[0].toFixed(2)} – ${p.expected_goals[1].toFixed(2)}` },
        { label: "Both teams score", value: `yes ${formatRatioPct(p.btts_yes)} · no ${formatRatioPct(p.btts_no)}` },
        ...["1.5", "2.5", "3.5"].map((l) => ({ label: `Over / under ${l}`, value: `${formatRatioPct(p.over[l])} / ${formatRatioPct(p.under[l])}` })),
        ...["-0.5", "-1.5", "+0.5"].flatMap((l) => {
          const m = ah(l);
          return m ? [{ label: `${home} ${l} (Asian handicap)`, value: `${formatRatioPct(m.win_probability)} · fair ${m.fair_odds?.toFixed(2) ?? "—"}` }] : [];
        }),
      ]}
    />
  );
};

const FootballTab = () => {
  const [mode, setMode] = useState<FootballMode>("stored");
  const fixtures = usePreMatchFixtures("all", "72h");
  const football = (fixtures.data ?? []).filter((f) => f.sport_key.startsWith("soccer"));
  const [matchId, setMatchId] = useState("");
  const [xg, setXg] = useState({ home: "", away: "", homeName: "", awayName: "" });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ p: FootballPrediction; home: string; away: string } | null>(null);

  const run = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    if (mode === "stored") {
      const r = await runMutation(() => predictStoredFixture(matchId), { errorTitle: "No prediction for that fixture" });
      if (r) setResult({ p: r, home: r.fixture?.home_team ?? "Home", away: r.fixture?.away_team ?? "Away" });
    } else {
      const r = await runMutation(() => predictFootballFromXg(num(xg.home), num(xg.away)), { errorTitle: "Expected goals rejected" });
      if (r) setResult({ p: r, home: xg.homeName.trim() || "Home", away: xg.awayName.trim() || "Away" });
    }
    setBusy(false);
  };
  const xgValid = [num(xg.home), num(xg.away)].every((v) => Number.isFinite(v) && v > 0 && v <= 8);

  return (
    <div className="flex flex-col gap-5">
      <form onSubmit={run} className="flex flex-col gap-3">
        <Segmented label="Football input" size="sm" value={mode} onChange={(v) => { setMode(v); setResult(null); }}
                   options={[{ value: "stored", label: "Stored fixture (market)", icon: "storage" }, { value: "xg", label: "Your xG", icon: "edit" }]} />
        {mode === "stored" ? (
          football.length === 0 ? (
            <EmptyState icon="sports_soccer" title={fixtures.loading ? "Loading the pre-match store" : "No football fixture in the pre-match store"}
                        detail="The ingestion fleet stores every board it buys (Group 101); fixtures kicking off within 72 hours appear here." />
          ) : (
            <div className="flex flex-wrap items-end gap-3">
              <Field label="Fixture" className="min-w-[16rem] flex-1">
                <Select value={matchId} onChange={(e) => setMatchId(e.target.value)}>
                  <option value="">Choose a fixture</option>
                  {football.map((f) => (
                    <option key={f.match_id} value={f.match_id}>{f.home_team} v {f.away_team} · {formatDateTime(f.commence_time)}</option>
                  ))}
                </Select>
              </Field>
              <Button type="submit" variant="primary" icon="scoreboard" busy={busy} disabled={!matchId}>Predict</Button>
            </div>
          )
        ) : (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5 md:items-end">
            <Field label="Home side"><TextInput value={xg.homeName} onChange={(e) => setXg({ ...xg, homeName: e.target.value })} placeholder="Home" /></Field>
            <Field label="Away side"><TextInput value={xg.awayName} onChange={(e) => setXg({ ...xg, awayName: e.target.value })} placeholder="Away" /></Field>
            <Field label="Home xG"><NumberInput min="0.05" max="8" step="0.01" value={xg.home} onChange={(e) => setXg({ ...xg, home: e.target.value })} /></Field>
            <Field label="Away xG"><NumberInput min="0.05" max="8" step="0.01" value={xg.away} onChange={(e) => setXg({ ...xg, away: e.target.value })} /></Field>
            <Button type="submit" variant="primary" icon="scoreboard" busy={busy} disabled={!xgValid}>Predict</Button>
          </div>
        )}
      </form>
      {result && (
        <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <FootballScoreHeatmap heatmap={result.p.heatmap} top={result.p.top_scorelines} mass={result.p.heatmap_mass} homeLabel={result.home} awayLabel={result.away} />
          <div className="flex flex-col gap-3">
            <FootballSummary p={result.p} home={result.home} away={result.away} />
            <p className="text-[11px] text-stone-500 dark:text-stone-400">
              {result.p.source === "market"
                ? `Dixon-Coles rates fitted to ${result.p.fixture?.books ?? "the"} books' de-vigged prices (stored ${result.p.fixture ? formatDateTime(result.p.fixture.last_polled_at) : "—"}).`
                : "Dixon-Coles on your expected goals."}{" "}
              Low-score dependence ρ {result.p.rho?.toFixed(2) ?? "—"}. To back a parlay leg with a score, open the fixture's Scores on The Oracle.
            </p>
          </div>
        </div>
      )}
    </div>
  );
};

const RATING_FIELDS = [["home", "batting"], ["home", "bowling"], ["away", "batting"], ["away", "bowling"]] as const;

const CricketTab = () => {
  const [form, setForm] = useState({
    homeName: "", awayName: "", format: "t20", battingFirst: "home", night: false, dew: "none", wear: "0", target: "",
    home_batting: "100", home_bowling: "100", away_batting: "100", away_bowling: "100",
  });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ p: CricketPrediction; home: string; away: string } | null>(null);
  const ratings = RATING_FIELDS.map(([side, kind]) => num(form[`${side}_${kind}`]));
  const wear = num(form.wear);
  const target = form.target.trim() === "" ? undefined : num(form.target);
  const valid = ratings.every((r) => Number.isFinite(r) && r >= 40 && r <= 250) && Number.isFinite(wear) && wear >= 0 && wear <= 1
    && (target === undefined || (Number.isInteger(target) && target >= 1 && target <= 1000));

  const run = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    const r = await runMutation(() => predictCricket({
      home_stats: { batting_rating: num(form.home_batting), bowling_rating: num(form.home_bowling) },
      away_stats: { batting_rating: num(form.away_batting), bowling_rating: num(form.away_bowling) },
      context: { format: form.format, batting_first: form.battingFirst, is_night_match: form.night, ...(form.night && form.dew !== "none" ? { dew_intensity: form.dew } : {}), pitch_wear: wear },
      ...(target !== undefined ? { chase_target: target } : {}),
    }), { errorTitle: "Innings model rejected the inputs" });
    if (r) setResult({ p: r, home: form.homeName.trim() || "Home", away: form.awayName.trim() || "Away" });
    setBusy(false);
  };
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  return (
    <div className="flex flex-col gap-5">
      <form onSubmit={run} className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Field label="Home side"><TextInput value={form.homeName} onChange={set("homeName")} placeholder="Home" /></Field>
          <Field label="Away side"><TextInput value={form.awayName} onChange={set("awayName")} placeholder="Away" /></Field>
          <Field label="Format"><Select value={form.format} onChange={set("format")}><option value="t20">T20 (20 overs)</option><option value="odi">ODI (50 overs)</option></Select></Field>
          <Field label="Bats first"><Select value={form.battingFirst} onChange={set("battingFirst")}><option value="home">Home</option><option value="away">Away</option></Select></Field>
          {RATING_FIELDS.map(([side, kind]) => (
            <Field key={`${side}_${kind}`} label={`${side === "home" ? "Home" : "Away"} ${kind} rating`} hint="100 = an average side">
              <NumberInput min="40" max="250" step="1" value={form[`${side}_${kind}`]} onChange={set(`${side}_${kind}`)} />
            </Field>
          ))}
          <Field label="Pitch wear" hint="0 fresh · 1 worn (helps spin)"><NumberInput min="0" max="1" step="0.05" value={form.wear} onChange={set("wear")} /></Field>
          <Field label="Dew (night match only)">
            <Select value={form.dew} onChange={set("dew")} disabled={!form.night}>
              <option value="none">None</option><option value="light">Light</option><option value="moderate">Moderate</option><option value="heavy">Heavy</option>
            </Select>
          </Field>
          <Field label="Chase target (optional)" hint="runs to win"><NumberInput min="1" max="1000" step="1" value={form.target} onChange={set("target")} /></Field>
          <div className="flex items-end"><Toggle checked={form.night} onChange={(night) => setForm({ ...form, night })} label="Night match" /></div>
        </div>
        <div><Button type="submit" variant="primary" icon="sports_cricket" busy={busy} disabled={!valid}>Project innings</Button></div>
      </form>
      {result && <CricketInningsCard prediction={result.p} homeLabel={result.home} awayLabel={result.away} />}
    </div>
  );
};

export const ScorePredictionPanel = () => {
  const [sport, setSport] = useState<Sport>("football");
  return (
    <Panel title="BetDoc Score Prediction" icon="scoreboard" className="lg:col-span-12"
           subtitle="Scorelines and innings from the engines the desk prices with"
           actions={<Segmented label="Sport" size="sm" value={sport} onChange={setSport}
                               options={[{ value: "football", label: "Football", icon: "sports_soccer" }, { value: "cricket", label: "Cricket", icon: "sports_cricket" }]} />}>
      {sport === "football" ? <FootballTab /> : <CricketTab />}
    </Panel>
  );
};
