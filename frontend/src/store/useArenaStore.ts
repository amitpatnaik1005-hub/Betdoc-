/**
 * The Arena's opportunity queue: Aryabhata's +EV lines from `/ws/signals` (via OmniGateway).
 *
 * - Dedupe: one card per fixture + selection. A newer signal for it updates that card in place
 *   (odds, stake, EV) and restarts its countdown; it never adds a second card.
 * - TTL: a card lives until the server's expires_at, converted to this browser's clock through the
 *   frame's server_time, so a skewed clock can't shorten or stretch the 15s.
 * - Order and capacity: newest opportunity first; an update keeps a card where it is. The Arena
 *   shows the first MAX_VISIBLE, so a sixth new line slides the oldest out of view, while a line
 *   that is merely re-confirmed never reshuffles the queue. Up to MAX_TRACKED are kept (memory bound;
 *   past it the one closest to expiry goes).
 * - Withdrawals: when a line closes before its TTL, the server says so and the card leaves at once.
 */
import { create } from "zustand";
import { OMNI_WILDCARD_TOPIC, OmniSocket, type OmniSocketStatus } from "../services/OmniGateway";

const SIGNALS_PATH = "/ws/signals";
export const MAX_VISIBLE = 5;
export const MAX_TRACKED = 50;
const DEFAULT_TTL_MS = 15_000;

export type StakeBinding = "kelly" | "pct_cap" | "max_bet" | "halted" | "no_bankroll" | "no_edge";

/** Mirrors backend `TradeSignal` (app/schemas/aryabhata.py). */
export interface TradeSignal {
  signal_id: string;
  fixture_id: string;
  market_id: string;
  selection: string;
  odds: number;
  true_prob: number;
  ev_percent: number;
  kelly_stake_inr: number;
  bookmaker_id: string;
  timestamp: string;
  expires_at: string;
  market_type: string;
  home_team: string;
  away_team: string;
  sport_key: string | null;
  commence_time: string | null;
  source: string;
  devig_method: "shin" | "mpo" | "multiplicative";
  overround: number;
  books: number;
  stake_fraction: number;
  stake_binding: StakeBinding;
}

export interface ArenaSignal extends TradeSignal {
  key: string;
  /** Local-clock expiry and lifetime: what the countdown ring and the pruner use. */
  expiresAt: number;
  ttlMs: number;
  /** Bumps on every in-place update, so the card can restart its ring and flash the change. */
  revision: number;
  firstSeenAt: number;
}

export interface RiskConfig {
  kelly_multiplier: number;
  max_stake_pct: number;
  max_bet_size: number | null;
  halted: boolean;
}

interface ArenaState {
  signals: ArenaSignal[]; // newest opportunity first
  status: OmniSocketStatus;
  risk: RiskConfig | null;
  bankroll: number | null;
  lastFrameAt: number | null;
  /** Handle one `/ws/signals` frame. Exposed for tests; the socket calls it. */
  ingest: (frame: unknown, now?: number) => void;
  /** Drop every card whose TTL has run out. */
  prune: (now?: number) => void;
  /** Open (or share) the signals socket. Returns the unsubscribe. */
  connect: () => () => void;
}

// ---------------------------------------------------------------- parsing
const isRecord = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const text = (v: unknown): v is string => typeof v === "string" && v.length > 0;

export const signalKey = (s: Pick<TradeSignal, "fixture_id" | "selection">): string => `${s.fixture_id}|${s.selection}`;

/** A frame's signal, or null when it fails the contract (never trust the wire blindly). */
export function parseSignal(raw: unknown): TradeSignal | null {
  if (!isRecord(raw)) return null;
  const s = raw as Partial<TradeSignal>;
  if (!text(s.signal_id) || !text(s.fixture_id) || !text(s.selection) || !text(s.bookmaker_id)) return null;
  if (!finite(s.odds) || s.odds <= 1 || !finite(s.true_prob) || s.true_prob <= 0 || s.true_prob >= 1) return null;
  if (!finite(s.ev_percent) || !finite(s.kelly_stake_inr) || s.kelly_stake_inr < 0) return null;
  if (!text(s.timestamp) || !text(s.expires_at)) return null;
  return {
    ...(s as TradeSignal),
    home_team: text(s.home_team) ? s.home_team : "Home",
    away_team: text(s.away_team) ? s.away_team : "Away",
    books: finite(s.books) ? s.books : 0,
    overround: finite(s.overround) ? s.overround : 0,
    stake_fraction: finite(s.stake_fraction) ? s.stake_fraction : 0,
  };
}

function lifetimeMs(signal: TradeSignal): number {
  const ttl = Date.parse(signal.expires_at) - Date.parse(signal.timestamp);
  return Number.isFinite(ttl) && ttl > 0 ? Math.min(ttl, DEFAULT_TTL_MS) : DEFAULT_TTL_MS;
}

/** Time left on the server's clock, clamped to (0, lifetime]. Unknown server time: a full lifetime. */
function remainingMs(signal: TradeSignal, serverNow: number | null, lifetime: number): number {
  if (serverNow === null) return lifetime;
  const left = Date.parse(signal.expires_at) - serverNow;
  return Number.isFinite(left) ? Math.max(0, Math.min(left, lifetime)) : lifetime;
}

function parseRisk(raw: unknown): RiskConfig | null {
  if (!isRecord(raw) || !finite(raw.max_stake_pct) || !finite(raw.kelly_multiplier) || typeof raw.halted !== "boolean") return null;
  return {
    kelly_multiplier: raw.kelly_multiplier,
    max_stake_pct: raw.max_stake_pct,
    max_bet_size: finite(raw.max_bet_size) ? raw.max_bet_size : null,
    halted: raw.halted,
  };
}

// ---------------------------------------------------------------- queue maths (pure)
/** Upsert signals into the queue: update in place by key, else insert newest-first, then cap. */
export function upsertSignals(queue: readonly ArenaSignal[], incoming: readonly TradeSignal[], now: number, serverNow: number | null = null): ArenaSignal[] {
  const next = [...queue];
  for (const signal of incoming) {
    const key = signalKey(signal);
    const ttlMs = lifetimeMs(signal);
    const expiresAt = now + remainingMs(signal, serverNow, ttlMs);
    const index = next.findIndex((s) => s.key === key);
    if (index >= 0) {
      const current = next[index];
      next[index] = { ...signal, key, ttlMs, expiresAt, revision: current.revision + 1, firstSeenAt: current.firstSeenAt };
    } else {
      next.unshift({ ...signal, key, ttlMs, expiresAt, revision: 0, firstSeenAt: now });
    }
  }
  const fresh = new Set(incoming.map(signalKey));
  while (next.length > MAX_TRACKED) {
    // Push out the card closest to expiry, never one that just arrived in this frame
    let victim = -1;
    next.forEach((s, i) => {
      if (fresh.has(s.key) && next.some((o) => !fresh.has(o.key))) return;
      if (victim < 0 || s.expiresAt < next[victim].expiresAt) victim = i;
    });
    next.splice(victim, 1);
  }
  return next;
}

export const pruneExpired = (queue: readonly ArenaSignal[], now: number): ArenaSignal[] => queue.filter((s) => s.expiresAt > now);

// ---------------------------------------------------------------- store
let unsubscribeSocket: (() => void) | null = null;
let unsubscribeStatus: (() => void) | null = null;
let subscribers = 0;

export const useArenaStore = create<ArenaState>()((set, get) => ({
  signals: [],
  status: "idle",
  risk: null,
  bankroll: null,
  lastFrameAt: null,

  ingest: (frame, now = Date.now()) => {
    if (!isRecord(frame) || typeof frame.type !== "string") return;
    const serverTime = text(frame.server_time) ? Date.parse(frame.server_time) : Number.NaN;
    const serverNow = Number.isFinite(serverTime) ? serverTime : null;
    if (frame.type === "snapshot") {
      const incoming = (Array.isArray(frame.signals) ? frame.signals : []).map(parseSignal).filter((s): s is TradeSignal => s !== null);
      const keep = new Set(incoming.map(signalKey));
      // A snapshot is the whole truth: cards it doesn't mention are gone; the rest update in place
      const base = get().signals.filter((s) => keep.has(s.key));
      set({
        signals: pruneExpired(upsertSignals(base, [...incoming].reverse(), now, serverNow), now),
        risk: parseRisk(frame.risk) ?? get().risk,
        bankroll: finite(frame.bankroll) ? frame.bankroll : null,
        lastFrameAt: now,
      });
      return;
    }
    if (frame.type === "signals") {
      const withdrawn = new Set((Array.isArray(frame.withdrawn) ? frame.withdrawn : []).filter(text));
      const incoming = (Array.isArray(frame.signals) ? frame.signals : []).map(parseSignal).filter((s): s is TradeSignal => s !== null);
      const kept = get().signals.filter((s) => !withdrawn.has(s.key));
      set({ signals: pruneExpired(upsertSignals(kept, incoming, now, serverNow), now), lastFrameAt: now });
    }
  },

  prune: (now = Date.now()) => {
    const current = get().signals;
    const next = pruneExpired(current, now);
    if (next.length !== current.length) set({ signals: next });
  },

  connect: () => {
    subscribers += 1;
    if (subscribers === 1) {
      const socket = OmniSocket.channel(SIGNALS_PATH);
      unsubscribeStatus = socket.onStatus((status) => set({ status }));
      unsubscribeSocket = socket.subscribe(OMNI_WILDCARD_TOPIC, (data) => get().ingest(data));
    }
    let released = false;
    return () => {
      if (released) return;
      released = true;
      subscribers -= 1;
      if (subscribers === 0) {
        unsubscribeSocket?.();
        unsubscribeStatus?.();
        unsubscribeSocket = null;
        unsubscribeStatus = null;
        set({ signals: [], status: "idle" });
      }
    };
  },
}));
