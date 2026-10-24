/**
 * The sports the board can show, switched on one by one (Group 127). The list is The Odds API's own index of what is in
 * season (read for free), each sport with what it costs a day: every polled sport spends credits, more as its kickoffs
 * near, so a sport is polled only once switched on here. The environment's sports (ODDS_SPORT_KEYS) are always polled.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useState } from "react";
import { runMutation } from "../../lib/resource";
import { type CatalogSport, switchSport, useSportCatalog } from "../../lib/vault";
import { Async, Toggle } from "../../ui/kit";

const cost = (s: CatalogSport) => `≈${s.credits_per_day.far}–${s.credits_per_day.near} credits/day`;

export const SportSwitchboard = () => {
  const catalog = useSportCatalog();
  const [busy, setBusy] = useState<string | null>(null);

  const flip = async (s: CatalogSport, polling: boolean) => {
    setBusy(s.key);
    await runMutation(() => switchSport(s.key, polling), {
      invalidate: ["vault:sports", "vault:status"],
      success: polling ? `${s.title} is polled now (${cost(s)})` : `${s.title} is no longer polled`,
      errorTitle: polling ? `${s.title} was not switched on` : `${s.title} was not switched off`,
    });
    setBusy(null);
  };

  return (
    <details className="group rounded-2xl border border-stone-200/70 p-3 dark:border-white/[0.08]">
      <summary className="flex cursor-pointer items-center justify-between gap-2 text-sm font-semibold">
        <span>Sports on the board</span>
        <span className="text-xs font-normal text-stone-500 dark:text-stone-400">
          {catalog.data ? `${catalog.data.sports.filter((s) => s.polling).length} polled · ${catalog.data.sports.filter((s) => !s.polling).length} more in season` : "switch a sport on"}
        </span>
      </summary>
      <Async resource={catalog} skeletonRows={3}>
        {(c) => {
          const families = [...new Set(c.sports.map((s) => s.category ?? "Other"))];
          return (
            <div className="mt-3 flex flex-col gap-3">
              <p className="text-[11px] text-stone-500 dark:text-stone-400">
                {c.index_read ? "In season on The Odds API now." : `The in-season list could not be read (${c.reason}): only the polled sports are shown.`} Every polled sport spends credits,
                fewer while its kickoffs are days away and more as they near. Switch on only what you will bet.
              </p>
              {families.map((family) => (
                <section key={family} className="flex flex-col gap-1.5">
                  <h4 className="text-[11px] font-bold uppercase tracking-wider text-stone-500 dark:text-stone-400">{family}</h4>
                  <ul className="flex flex-col gap-1">
                    {c.sports.filter((s) => (s.category ?? "Other") === family).map((s) => (
                      <li key={s.key} className="flex items-center justify-between gap-3 rounded-xl px-2 py-1.5 text-xs hover:bg-stone-50 dark:hover:bg-white/[0.03]">
                        <span className="min-w-0">
                          <span className="block truncate font-medium">
                            {s.title}
                            {s.suggested && !s.polling && <span className="ml-1.5 rounded bg-amber-100 px-1 py-0.5 text-[9px] font-bold uppercase text-amber-800 dark:bg-amber-500/15 dark:text-amber-300">suggested</span>}
                            {s.in_season === false && <span className="ml-1.5 text-[10px] text-stone-400">out of season</span>}
                          </span>
                          <span className="block truncate font-mono text-[10px] text-stone-400">{s.key} · {cost(s)}</span>
                        </span>
                        {s.source === "environment" ? (
                          <span className="shrink-0 text-[10px] text-stone-400" title="Set in ODDS_SPORT_KEYS">always on</span>
                        ) : (
                          <Toggle checked={s.polling} disabled={busy !== null || (!s.polling && s.in_season === false)} label={`Poll ${s.title}`}
                                  onChange={(next) => void flip(s, next)} />
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          );
        }}
      </Async>
    </details>
  );
};
