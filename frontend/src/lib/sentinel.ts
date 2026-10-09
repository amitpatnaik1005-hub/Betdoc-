/** The Sentinel: alerting, liveness and remote command (backend `app/api/v1/sentinel.py`, Group 68). */
import { apiClient } from "../api/client";
import { subscribeChannel } from "../services/realtime";
import { useResource } from "./resource";

export const SEVERITIES = ["FATAL", "CRITICAL", "WARNING", "INFO"] as const;
export type Severity = (typeof SEVERITIES)[number];
export const CHANNELS = ["TELEGRAM", "DISCORD", "TWILIO", "PAGERDUTY"] as const;
export type ChannelName = (typeof CHANNELS)[number];
export const BROWSER = "BROWSER";

export interface Delivery {
  channel: string;
  status: "SENT" | "FAILED" | "BATCHED" | "DIGEST" | "SKIPPED";
  error: string | null;
  latency_ms: number | null;
  digest_id: string | null;
  attempted_at: string | null;
}

export interface SentinelAlert {
  id: string;
  kind: string;
  severity: Severity;
  title: string;
  body: string;
  source: string;
  dedupe_key: string | null;
  resolves?: boolean;
  detail: Record<string, unknown>;
  occurred_at: string;
  deliveries?: Delivery[];
}

export interface HealthCheck {
  name: string;
  kind: "postgres" | "redis" | "bookmaker";
  ok: boolean;
  latency_ms: number | null;
  detail: string;
  checked_at: string;
}

export interface Liveness {
  name: string;
  status: "ALIVE" | "SILENT" | "UNKNOWN";
  last_beat_at: string | null;
  age_seconds: number | null;
  runner: string | null;
  timeout_seconds: number;
  heartbeat_seconds: number;
}

export interface DebounceState {
  holding: number;
  last_sent: string | null;
  window_closes: string | null;
  sent: number;
  digests: number;
  batched: number;
}

export interface HypeResult {
  tier: "BUSSIN" | "PRIMED" | "VOLATILE" | "QUIET";
  forecast: {
    day: string;
    timezone: string;
    fixtures_today: number;
    live_edges: number;
    total_ev_pct: string;
    mean_ev_pct: string;
    steam_moves: number;
    sports: Record<string, number>;
    best: { fixture: string; selection: string; market: string; bookmaker: string; odds: string; ev_percent: string } | null;
  };
  sent: boolean;
  dry_run: boolean;
  line_id?: string;
  title?: string;
  body?: string;
  already_sent_today?: boolean;
  at?: string;
}

export interface SentinelStatus {
  enabled: boolean;
  generated_at: string;
  redis: boolean;
  liveness?: Liveness;
  health?: HealthCheck[];
  debounce?: Record<string, DebounceState>;
  stats?: Record<string, string>;
  stream_length?: number;
  kill_switch?: boolean | null;
  hype?: HypeResult | null;
  routing: Record<string, string[]>;
  channels: { channel: ChannelName; enabled: boolean; ready: boolean; problem: string | null; last_success_at: string | null; last_error: string | null }[];
  settings: {
    debounce_seconds: number;
    health_interval_seconds: number;
    liveness_timeout_seconds: number;
    heartbeat_seconds: number;
    whale_stake_inr: string;
    margin_utilisation: string;
    hype_at: string;
    flash_min_probability: number;
  };
}

export interface ChannelView {
  channel: ChannelName;
  enabled: boolean;
  ready: boolean;
  problem: string | null;
  credentials_hint: string | null;
  credentials_set: Record<string, boolean>;
  credential_fields: string[];
  config: Record<string, unknown>;
  config_fields: Record<string, "list" | "bool" | "str">;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
}

export interface RoutingView {
  matrix: Record<string, string[]>;
  rows: string[];
  columns: string[];
  browser: string;
}

export interface CommandRow {
  id: number;
  command: string;
  sender: string | null;
  chat_id: number | null;
  authorised: boolean;
  outcome: string;
  received_at: string | null;
}

export type LiveFrame =
  | { type: "alert"; alert: SentinelAlert }
  | { type: "digest"; channel: string; count: number; digest_id: string; at: string }
  | { type: "debounce"; channels: Record<string, DebounceState>; at: string }
  | { type: "health"; checks: HealthCheck[]; at: string };

export const useSentinelStatus = () => useResource("sentinel:status", () => apiClient.get<SentinelStatus>("/sentinel/status"), { intervalMs: 5_000 });
export const useSentinelAlerts = (severity: Severity | "") =>
  useResource(`sentinel:alerts:${severity}`, () => apiClient.get<SentinelAlert[]>("/sentinel/alerts", { limit: 150, ...(severity ? { severity } : {}) }), { intervalMs: 15_000 });
export const useSentinelChannels = (enabled: boolean) => useResource(enabled ? "sentinel:channels" : null, () => apiClient.get<ChannelView[]>("/sentinel/channels"));
export const useSentinelRouting = () => useResource("sentinel:routing", () => apiClient.get<RoutingView>("/sentinel/routing"));
export const useSentinelCommands = (enabled: boolean) => useResource(enabled ? "sentinel:commands" : null, () => apiClient.get<CommandRow[]>("/sentinel/commands", { limit: 30 }), { intervalMs: 15_000 });

export const subscribeSentinel = (handler: (frame: LiveFrame) => void): (() => void) =>
  subscribeChannel("/ws/sentinel", (data) => {
    if (data && typeof data === "object" && "type" in data) handler(data as LiveFrame);
  });

export const CHANNEL_LABEL: Record<string, string> = { TELEGRAM: "Telegram", DISCORD: "Discord", TWILIO: "SMS / voice", PAGERDUTY: "PagerDuty", BROWSER: "Browser siren" };
export const CHANNEL_ICON: Record<string, string> = { TELEGRAM: "send", DISCORD: "forum", TWILIO: "sms", PAGERDUTY: "notification_important", BROWSER: "campaign" };
export const ROW_LABEL: Record<string, string> = { FATAL: "Fatal", CRITICAL: "Critical", WARNING: "Warning", INFO: "Info", HYPE: "08:00 hype" };

// ---------------------------------------------------------------- the HTML5 siren
const ARM_KEY = "betdoc.sentinel.sirens";

/**
 * A two-tone wail made with WebAudio: no audio file to load. Browsers only let a page make sound after
 * a user gesture, so arming (a click) creates and resumes the AudioContext; after that a FATAL frame
 * can sound it at any time. One siren per tab; it stops on acknowledge or after `maxSeconds`.
 */
class Siren {
  private ctx: AudioContext | null = null;
  private stopAt: number | null = null;
  private nodes: { osc: OscillatorNode; gain: GainNode } | null = null;
  private timer: number | null = null;
  private listeners = new Set<(sounding: boolean) => void>();

  get armed(): boolean {
    return this.ctx !== null && this.ctx.state === "running";
  }

  get sounding(): boolean {
    return this.nodes !== null;
  }

  wanted(): boolean {
    try {
      return window.localStorage.getItem(ARM_KEY) === "1";
    } catch {
      return false;
    }
  }

  async arm(): Promise<boolean> {
    try {
      const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Ctor) return false;
      this.ctx ??= new Ctor();
      if (this.ctx.state !== "running") await this.ctx.resume();
      try {
        window.localStorage.setItem(ARM_KEY, "1");
      } catch {
        /* a private window: armed for this page only */
      }
      return this.ctx.state === "running";
    } catch {
      return false;
    }
  }

  disarm(): void {
    this.stop();
    try {
      window.localStorage.removeItem(ARM_KEY);
    } catch {
      /* nothing stored */
    }
    void this.ctx?.close();
    this.ctx = null;
  }

  onChange(listener: (sounding: boolean) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const listener of this.listeners) listener(this.sounding);
  }

  /** Wail for up to `maxSeconds` (re-sounding extends it). Returns false when not armed. */
  sound(maxSeconds = 20): boolean {
    const ctx = this.ctx;
    if (!ctx || ctx.state !== "running") return false;
    this.stopAt = ctx.currentTime + maxSeconds;
    if (this.nodes) return true;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sawtooth";
    gain.gain.setValueAtTime(0, ctx.currentTime);
    gain.gain.linearRampToValueAtTime(0.07, ctx.currentTime + 0.05);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    this.nodes = { osc, gain };
    const sweep = () => {
      if (!this.nodes || this.stopAt === null) return;
      const now = ctx.currentTime;
      if (now >= this.stopAt) {
        this.stop();
        return;
      }
      const f = this.nodes.osc.frequency;
      f.cancelScheduledValues(now);
      f.setValueAtTime(620, now);
      f.linearRampToValueAtTime(1240, now + 0.45);
      f.linearRampToValueAtTime(620, now + 0.9);
    };
    sweep();
    this.timer = window.setInterval(sweep, 900);
    this.emit();
    return true;
  }

  stop(): void {
    if (this.timer !== null) window.clearInterval(this.timer);
    this.timer = null;
    if (this.nodes && this.ctx) {
      const { osc, gain } = this.nodes;
      const now = this.ctx.currentTime;
      gain.gain.cancelScheduledValues(now);
      gain.gain.setValueAtTime(gain.gain.value, now);
      gain.gain.linearRampToValueAtTime(0, now + 0.08);
      osc.stop(now + 0.1);
    }
    this.nodes = null;
    this.stopAt = null;
    this.emit();
  }
}

export const siren = new Siren();
