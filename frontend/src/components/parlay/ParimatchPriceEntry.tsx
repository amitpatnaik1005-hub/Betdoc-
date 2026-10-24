/**
 * Parimatch's own prices, typed from its site or app (Group 127). The Odds API does not carry Parimatch, and BetDoc never
 * copies another book's price or derives one and calls it Parimatch's: a slip staked on a price Parimatch never offered
 * would be vetted on numbers that are not real. Here an administrator types what Parimatch shows for one market now; the
 * prices go through Parimatch's direct door (`POST /parimatch/odds`, Group 70), sit beside the other books' and are
 * priced, rated and vetted like any feed. Prices older than ten minutes are refused there. Developed for Amit Ashok Kumar Patnaik.
 */
import { useId, useState } from "react";
import { ApiError, apiClient } from "../../api/client";
import type { FixtureCard, MarketCard } from "../../lib/backtest_and_parlay";
import { invalidate } from "../../lib/resource";
import { useAuthStore } from "../../store/useAuthStore";
import { toast } from "../../store/useToastStore";

interface Props {
  card: Pick<FixtureCard, "fixture_id" | "home" | "away" | "sport_key" | "kickoff">;
  market: MarketCard;
}

interface InjectResult {
  frames: number;
  rejected: { reason: string }[];
}

const MIN_ODDS = 1.01;
const MAX_ODDS = 1000;

export const ParimatchPriceEntry = ({ card, market }: Props) => {
  const isAdmin = useAuthStore((s) => s.user?.role === "ADMIN");
  const formId = useId();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isAdmin) {
    return <p className="text-[10px]" style={{ color: "var(--wb-muted)" }}>Parimatch is not in the odds feed: an administrator can type its prices here.</p>;
  }
  if (!card.sport_key || !card.kickoff) {
    return <p className="text-[10px]" style={{ color: "var(--wb-muted)" }}>Parimatch is not in the odds feed, and this fixture has no sport or kickoff on record to file its prices under.</p>;
  }

  const parsed = market.selections.map((s) => {
    const raw = (values[s.selection] ?? "").trim().replace(",", ".");
    const odds = raw ? Number(raw) : null;
    return { s, raw, odds, valid: odds === null || (Number.isFinite(odds) && odds >= MIN_ODDS && odds <= MAX_ODDS) };
  });
  const filled = parsed.filter((p) => p.odds !== null);
  const ready = filled.length >= 2 && parsed.every((p) => p.valid) && !saving;

  const save = async () => {
    if (!ready) return;
    setSaving(true);
    setError(null);
    try {
      const result = await apiClient.post<InjectResult>("/parimatch/odds", {
        events: [{
          fixture_id: card.fixture_id, home: card.home, away: card.away, sport_key: card.sport_key, kickoff: card.kickoff,
          markets: [{ market: market.market, prices: Object.fromEntries(filled.map((p) => [p.s.selection, p.odds])) }],
        }],
        observed_at: new Date().toISOString(),
      });
      if (!result.frames) {
        setError(result.rejected[0]?.reason ?? "Parimatch's prices were not accepted");
        return;
      }
      invalidate("oracle:board", "parlay:board");
      toast.success("Parimatch prices saved", `${card.home} vs ${card.away} · ${market.market}: ${filled.map((p) => `${p.s.selection} ${p.odds}`).join(", ")}`);
      setOpen(false);
      setValues({});
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "No answer from the server");
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="inline-flex items-center gap-1 self-start rounded-full px-2.5 py-1 text-[11px] font-semibold"
              style={{ border: "1px dashed var(--wb-primary)", color: "var(--wb-primary)" }}>
        <span className="material-symbols-outlined text-[14px]" aria-hidden="true">edit</span>Enter Parimatch's prices
      </button>
    );
  }

  return (
    <form id={formId} className="flex flex-col gap-2 rounded-xl p-2.5" style={{ border: "1px dashed var(--wb-primary)" }}
          onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <p className="text-[11px]" style={{ color: "var(--wb-muted)" }}>
        Type {market.market} exactly as Parimatch shows it now. The feed does not carry Parimatch, and BetDoc never copies or guesses its prices; prices over ten minutes old are refused.
      </p>
      <div className="grid grid-cols-3 gap-1.5">
        {parsed.map(({ s, raw, valid }) => (
          <label key={s.leg_id} className="flex min-w-0 flex-col gap-0.5 text-[10px]" style={{ color: "var(--wb-muted)" }}>
            <span className="truncate">{s.label}</span>
            <input inputMode="decimal" value={raw} aria-invalid={!valid} placeholder="e.g. 2.10"
                   onChange={(e) => setValues((v) => ({ ...v, [s.selection]: e.target.value }))}
                   className="w-full rounded-lg bg-transparent px-2 py-1.5 font-mono text-sm"
                   style={{ border: `1px solid ${valid ? "color-mix(in srgb, var(--wb-primary) 40%, transparent)" : "#F43F5E"}`, color: "inherit" }} />
          </label>
        ))}
      </div>
      {error && <p role="alert" className="text-[11px] text-rose-400">{error}</p>}
      <div className="flex items-center gap-2">
        <button type="submit" disabled={!ready} className="rounded-full px-3 py-1 text-[11px] font-bold disabled:opacity-40"
                style={{ background: "var(--wb-primary)", color: "var(--wb-on-primary)" }}>
          {saving ? "Saving…" : "Save Parimatch's prices"}
        </button>
        <button type="button" onClick={() => { setOpen(false); setError(null); }} className="text-[11px] underline" style={{ color: "var(--wb-muted)" }}>Cancel</button>
        <span className="text-[10px]" style={{ color: "var(--wb-muted)" }}>{filled.length < 2 ? "at least two selections" : `${MIN_ODDS}–${MAX_ODDS}`}</span>
      </div>
    </form>
  );
};
