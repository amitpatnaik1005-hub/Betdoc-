/**
 * A fixture's context from the sports-data feeds (Group 112, point b.13): what Football-Data.org or Sportmonks say about
 * the match the Odds API prices: postponed or cancelled, in play or finished with its score, its kickoff confirmed or
 * moved, and where both sides sit in their table. Each chip names its feed; a fixture no feed paired shows none.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { formatDateTime } from "../../../lib/format";
import { type FixtureContext, ordinal, PROVIDER_LABEL } from "../../../lib/match_board";

const ALERT = { background: "color-mix(in srgb, #EF4444 18%, transparent)", color: "#EF4444" };
const LIVE = { background: "color-mix(in srgb, #F59E0B 20%, transparent)", color: "#F59E0B" };
const QUIET = { background: "color-mix(in srgb, var(--wb-muted) 16%, transparent)", color: "var(--wb-muted)" };

export const FixtureContextChips = ({ context }: { context: FixtureContext | null }) => {
  if (!context) return null;
  const source = PROVIDER_LABEL[context.provider] ?? context.provider;
  const score = context.score ? `${context.score.home}–${context.score.away}` : null;
  const { home, away } = context.standings;
  const chips: { key: string; text: string; icon: string; style: typeof QUIET; title: string }[] = [];
  if (context.status === "POSTPONED" || context.status === "CANCELLED" || context.status === "SUSPENDED") {
    chips.push({ key: "status", text: context.status.toLowerCase(), icon: "event_busy", style: ALERT, title: `${source} lists this match as ${context.status.toLowerCase()}` });
  } else if (context.status === "IN_PLAY" || context.status === "PAUSED") {
    chips.push({ key: "status", text: `${context.status === "PAUSED" ? "HT" : "live"}${score ? ` ${score}` : ""}`, icon: "sports_score", style: LIVE,
                 title: `${source}: ${context.status === "PAUSED" ? "half-time" : "in play"}${score ? `, ${score}` : ""}${context.provider_updated_at ? ` (updated ${formatDateTime(context.provider_updated_at)})` : ""}` });
  } else if (context.status === "FINISHED") {
    chips.push({ key: "status", text: `FT${score ? ` ${score}` : ""}`, icon: "flag", style: QUIET, title: `${source}: finished${score ? `, ${score}` : ""}` });
  }
  if (context.kickoff.check === "MOVED") {
    chips.push({ key: "kickoff", text: `kickoff ${formatDateTime(context.kickoff.provider_kickoff)}`, icon: "schedule", style: ALERT,
                 title: `${source} has the kickoff ${Math.abs(context.kickoff.difference_minutes)} min ${context.kickoff.difference_minutes > 0 ? "later" : "earlier"} than the odds feed` });
  } else if (context.status === "SCHEDULED") {
    chips.push({ key: "kickoff", text: "kickoff confirmed", icon: "event_available", style: QUIET, title: `${source} confirms the kickoff time` });
  }
  if (home || away) {
    chips.push({ key: "table", text: `${home ? ordinal(home.position) : "—"} v ${away ? ordinal(away.position) : "—"}`, icon: "format_list_numbered", style: QUIET,
                 title: `${context.competition ?? "League"} table (${source})${home?.points != null ? ` · home ${home.points} pts` : ""}${away?.points != null ? ` · away ${away.points} pts` : ""}` });
  }
  return (
    <>
      {chips.map((c) => (
        <span key={c.key} title={c.title} className="inline-flex items-center gap-0.5 rounded-full px-1.5 font-semibold" style={c.style}>
          <span className="material-symbols-outlined text-[12px]" aria-hidden="true">{c.icon}</span>{c.text}
        </span>
      ))}
    </>
  );
};

/** The stored-prices notice: prices as bought from the Odds API, their age, and that nothing is staked from them. */
export const StoredPricesBadge = ({ asOf, ageMinutes }: { asOf: string | null; ageMinutes: number | null }) => (
  <span className="inline-flex items-center gap-0.5 rounded-full px-1.5 font-semibold" style={LIVE}
        title={`Stored prices${asOf ? ` bought ${formatDateTime(asOf)}` : ""}: the live feed is not pricing this fixture right now. Fair odds as de-vigged then; no EV and nothing to add until the feed prices it again.`}>
    <span className="material-symbols-outlined text-[12px]" aria-hidden="true">inventory_2</span>
    stored · {ageMinutes === null ? "—" : ageMinutes < 90 ? `${ageMinutes} min old` : `${Math.round(ageMinutes / 60)} h old`}
  </span>
);
