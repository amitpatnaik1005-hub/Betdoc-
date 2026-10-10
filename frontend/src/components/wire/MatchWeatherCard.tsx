/**
 * One fixture's venue weather and its friction factor Pi (backend `weather_impact.py`). Developed for Amit Ashok Kumar Patnaik.
 * Under 1 suppresses scoring, over 1 lifts it (altitude); indoors is exactly 1.
 */
import { formatAgo, formatTime } from "../../lib/format";
import { type WeatherReport } from "../../lib/the_wire";
import { Pill } from "../../ui/kit";

const fixed = (v: number | null | undefined, digits = 1, unit = ""): string => (v === null || v === undefined ? "—" : `${v.toFixed(digits)}${unit}`);

export const MatchWeatherCard = ({ weather, title }: { weather: WeatherReport; title: string }) => {
  const pi = weather.pitch_impact_score ?? 1;
  const indoor = Boolean(weather.is_indoor_dome);
  const tone = indoor ? "good" : pi < 0.9 ? "critical" : pi < 0.98 ? "warning" : pi > 1.02 ? "info" : "good";
  return (
    <div className="rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-stone-800 dark:text-stone-100">{title}</p>
          <p className="truncate text-[11px] text-stone-400">
            {weather.venue_name ?? (indoor ? "Indoor arena" : "Venue")} {weather.kickoff_at ? `· ${formatTime(weather.kickoff_at)}` : ""}
          </p>
        </div>
        <Pill tone={tone}>{indoor ? "Indoor · Π 1.00" : `Π ${pi.toFixed(3)}`}</Pill>
      </div>
      {!indoor && (
        <dl className="mt-3 grid grid-cols-4 gap-2 text-center font-mono text-xs">
          {[
            ["Temp", fixed(weather.temperature_c, 1, "°C")],
            ["Wind", `${fixed(weather.wind_speed_kmh, 0)} ${weather.wind_cardinal ?? ""}`],
            ["Rain", fixed(weather.precipitation_mmh, 1, " mm/h")],
            ["Humidity", fixed(weather.humidity_pct, 0, "%")],
          ].map(([label, value]) => (
            <div key={label} className="rounded-xl bg-white/70 px-1 py-2 dark:bg-white/[0.04]">
              <dt className="text-[10px] uppercase tracking-wide text-stone-400">{label}</dt>
              <dd className="mt-0.5 font-semibold text-stone-700 dark:text-stone-200">{value}</dd>
            </div>
          ))}
        </dl>
      )}
      <p className="mt-2 text-xs text-stone-500 dark:text-stone-400">
        <span className="font-semibold text-stone-600 dark:text-stone-300">{weather.condition}.</span> {weather.tactical_advisory}
        {weather.roof_may_close && <span className="ml-1 text-amber-600 dark:text-amber-300">The roof may close on the day.</span>}
      </p>
      {weather.fetched_at && <p className="mt-1 text-[10px] text-stone-400">{indoor ? "No forecast needed" : "Open-Meteo forecast for the match window"} · {formatAgo(weather.fetched_at)}</p>}
    </div>
  );
};
