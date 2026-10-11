/**
 * Universal 9-Sport Selection Bar with Model Indicators (Group 79)
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
 *
 * One chip per sport adapter; the active sport's model family and its sport-calibrated Kelly multiplier (FA-4)
 * sit in the header strip. A sport the desk has deactivated is shown dimmed.
 */
import { ALL_SPORTS, sportInfo, useSportStore } from '../../store/useSportStore';

interface Props {
  /** Sports the desk has switched off (sport_configurations.is_active = false). */
  inactive?: ReadonlySet<string>;
}

export const UniversalSportSelector = ({ inactive }: Props) => {
  const activeSport = useSportStore((s) => s.activeSport);
  const setActiveSport = useSportStore((s) => s.setActiveSport);
  const developerCredit = useSportStore((s) => s.developerCredit);
  const current = sportInfo(activeSport);

  return (
    <div className="flex w-full flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold uppercase tracking-[0.12em] text-stone-400">Sport fleet</span>
          <span className="rounded-full bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] px-2.5 py-0.5 font-medium text-[var(--accent-text)]">
            Model: {current.modelType}
          </span>
          <span className="rounded-full bg-stone-100 px-2.5 py-0.5 font-mono text-stone-600 dark:bg-white/[0.05] dark:text-stone-300" title="FA-4 sport-calibrated fractional Kelly">
            {current.kellyMultiplier.toFixed(2)} Kelly
          </span>
        </div>
        <span className="text-stone-400">
          Architect: <span className="font-medium text-stone-600 dark:text-stone-300">{developerCredit}</span>
        </span>
      </div>

      <div role="tablist" aria-label="Sport" className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-hide">
        {ALL_SPORTS.map((s) => {
          const isActive = activeSport === s.key;
          const off = inactive?.has(s.key) ?? false;
          return (
            <button
              key={s.key}
              type="button"
              role="tab"
              aria-selected={isActive}
              title={`${s.name}: ${s.modelType} · ${s.primaryLeague}${off ? ' (deactivated)' : ''}`}
              onClick={() => setActiveSport(s.key)}
              className={`flex shrink-0 items-center gap-2 whitespace-nowrap rounded-2xl px-3.5 py-2 text-xs font-semibold transition-[background-color,transform,box-shadow] duration-300 active:scale-95 ${
                isActive
                  ? 'bg-stone-900 text-white shadow-soft dark:bg-[#E3BE63] dark:text-stone-950'
                  : 'bg-white text-stone-600 hover:bg-stone-100 dark:bg-stone-900 dark:text-stone-300 dark:hover:bg-white/[0.06]'
              } ${off ? 'opacity-50' : ''}`}
            >
              <span className="material-symbols-outlined text-[18px]" aria-hidden>{s.icon}</span>
              <span>{s.name}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
};
