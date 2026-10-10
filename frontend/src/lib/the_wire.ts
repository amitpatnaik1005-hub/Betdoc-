/**
 * The Wire (backend `app/api/v1/the_wire.py`, `wire_ws.py`; Group 39, made the fortress's feed in Group 78).
 * Developed for Amit Ashok Kumar Patnaik.
 *
 * Every figure here comes from a feed (RSS, newsapi.org, ESPN, Open-Meteo) or an operator; nothing is filled in.
 * A fixture with no known venue shows no weather; an unrated absence counts in nothing until it is rated.
 */
import { useEffect, useRef, useState } from "react";
import { apiClient, readToken, wsUrl } from "../api/client";
import { useResource } from "./resource";

export type Impact = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";

export interface NewsItem {
  id: string;
  source: string;
  title: string;
  summary: string;
  url: string;
  published_at: string;
  sentiment_score?: number | null;
  tactical_impact?: Impact | null;
  source_credibility?: number | null;
  fixture_ids?: string[];
  associated_steam_move_id?: string | null;
}

export interface WeatherReport {
  match_id: string;
  temperature_c?: number | null;
  condition: string;
  wind_speed_kmh?: number | null;
  wind_cardinal?: string | null;
  humidity_pct?: number | null;
  precipitation_mmh?: number | null;
  pitch_impact_score?: number | null;
  is_indoor_dome?: boolean | null;
  roof_may_close?: boolean | null;
  tactical_advisory?: string | null;
  venue_name?: string | null;
  kickoff_at?: string | null;
  fetched_at?: string | null;
}

export interface MatchScore {
  match_id: string;
  home_team: string;
  away_team: string;
  home_score: number;
  away_score: number;
  status: string;
  clock?: string | null;
  detail?: string | null;
  source?: string | null;
}

export interface SteamCatalystAlert {
  article_id: string;
  headline: string;
  match_id: string;
  selection: string;
  probability_before: number;
  probability_after: number;
  shift_pct: number;
  latency_seconds: number;
  tactical_impact: string;
  source_credibility: number;
  published_at: string;
}

export interface WireDashboardData {
  news: NewsItem[];
  scores: MatchScore[];
  weather: Record<string, WeatherReport>;
  catalysts: SteamCatalystAlert[];
  developer_credit?: string | null;
}

export interface WireFixture {
  fixture_id: string;
  home: string;
  away: string;
  sport_key: string | null;
  kickoff: string;
  espn: { event_id: string; league: string; status: string; home_score: number; away_score: number; clock: string | null; detail: string | null } | null;
  weather: WeatherReport | null;
  absences: number;
  referee: string | null;
  intel_sections: string[];
}

export interface AbsenceRow {
  id: string;
  side: "HOME" | "AWAY";
  team: string;
  player: string;
  position: string | null;
  status: "OUT" | "SUSPENDED" | "DOUBTFUL" | "QUESTIONABLE";
  nature: string | null;
  return_date: string | null;
  rating: number | null;
  replacement_quality: number | null;
  source: string;
  cost: number | null;
  fortress_impact: number | null;
  position_weight: number;
}

export interface LineupView {
  fixture_id: string;
  home: string;
  away: string;
  absences: AbsenceRow[];
  delta: { HOME: number; AWAY: number };
  unrated: string[];
  probabilities: Record<string, number> | null;
  adjusted: Record<string, number> | null;
  rating_scale: number;
}

export interface RefereeProfileRow {
  referee: string;
  league: string;
  matches: number;
  avg_yellow_cards: number;
  avg_red_cards: number;
  cards_per_game: number;
  penalties_per_90: number;
  home_bias_ratio: number;
  over_totals_pct: number;
  baseline_matches: number;
  fortress_ready: boolean;
}

export const IMPACT_TONE: Record<Impact, "critical" | "warning" | "info" | "neutral"> = { CRITICAL: "critical", HIGH: "warning", MEDIUM: "info", LOW: "neutral" };

export const useWireDashboard = (matchIds: string) =>
  useResource(`the-wire:dashboard:${matchIds}`, () => apiClient.get<WireDashboardData>("/the-wire/dashboard", { match_ids: matchIds }), { intervalMs: 60_000 });
export const useWireFixtures = () => useResource("the-wire:fixtures", () => apiClient.get<{ fixtures: WireFixture[] }>("/the-wire/fixtures"), { intervalMs: 120_000 });
export const useLineup = (fixtureId: string | null) =>
  useResource(fixtureId ? `the-wire:absences:${fixtureId}` : null, () => apiClient.get<LineupView>(`/the-wire/absences/${fixtureId}`), { intervalMs: 120_000 });
export const useReferees = () =>
  useResource("the-wire:referees", () => apiClient.get<{ profiles: RefereeProfileRow[]; records_per_league: Record<string, number>; min_matches: number }>("/the-wire/referees"), { intervalMs: 600_000 });

export const rateAbsence = (id: string, rating: number, replacement_quality?: number) =>
  apiClient.patch<{ injury_section: { written: boolean; reason?: string; unrated?: string[] } }>(`/the-wire/absences/${id}`, replacement_quality === undefined ? { rating } : { rating, replacement_quality });
export const assignReferee = (fixture_id: string, referee_name: string) =>
  apiClient.post<{ referee_section_written: boolean }>("/the-wire/referees/assign", { fixture_id, referee_name });
export const scanWire = (parts = "news,espn,weather") => apiClient.post<Record<string, unknown>>(`/the-wire/scan?parts=${encodeURIComponent(parts)}`);

export type WireFrame =
  | ({ type: "news" } & NewsItem)
  | ({ type: "catalyst" } & SteamCatalystAlert)
  | ({ type: "scores" } & MatchScore)
  | ({ type: "weather" } & WeatherReport)
  | { type: "connection_ack"; developer: string; status: string }
  | { type: "pong" };

/** `/ws/the-wire`: the greeting, the recent backlog, then live frames. Reconnects with backoff, pings every 30 s. */
export function useWireStream(onFrame?: (frame: WireFrame) => void): { frames: WireFrame[]; connected: boolean; developer: string | null } {
  const [frames, setFrames] = useState<WireFrame[]>([]);
  const [connected, setConnected] = useState(false);
  const [developer, setDeveloper] = useState<string | null>(null);
  const retry = useRef(0);
  const handler = useRef(onFrame);
  useEffect(() => {
    handler.current = onFrame;
  }, [onFrame]);
  useEffect(() => {
    let socket: WebSocket | null = null;
    let ping: ReturnType<typeof setInterval> | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let closed = false;
    const open = () => {
      const token = readToken();
      if (!token || closed) return;
      socket = new WebSocket(`${wsUrl("/ws/the-wire")}?token=${encodeURIComponent(token)}`);
      socket.onopen = () => {
        retry.current = 0;
        setConnected(true);
        ping = setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 30_000);
      };
      socket.onmessage = (event) => {
        try {
          const frame = JSON.parse(String(event.data)) as WireFrame;
          if (frame.type === "pong") return;
          if (frame.type === "connection_ack") {
            setDeveloper(frame.developer);
            return;
          }
          setFrames((prev) => [frame, ...prev].slice(0, 100));
          handler.current?.(frame);
        } catch {
          /* a malformed frame is dropped; the dashboard's next poll carries the same record */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (ping) clearInterval(ping);
        if (closed) return;
        retry.current = Math.min(retry.current + 1, 6);
        timer = setTimeout(open, 1000 * 2 ** retry.current);
      };
    };
    open();
    return () => {
      closed = true;
      if (ping) clearInterval(ping);
      if (timer) clearTimeout(timer);
      socket?.close();
    };
  }, []);
  return { frames, connected, developer };
}
