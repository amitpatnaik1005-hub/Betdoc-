import { create } from "zustand";
import { wsUrl } from "../api/client";

const LIVE_ODDS_PATH = "/ws/live-odds";
// The server closes sockets idle for 90s; ping well inside that window.
const PING_INTERVAL_MS = 30_000;
const BASE_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const JITTER_MS = 500;
// Close codes meaning "your credentials are bad". Retrying these forever is pointless.
const AUTH_FAILURE_CODES: ReadonlySet<number> = new Set([1008, 4001, 4401, 4403]);

export interface MarketTick {
  matchId: string;
  homeTeam: string;
  awayTeam: string;
  marketType: string;
  selection: string;
  odds: number;
  trueProbability: number;
  isSuspended: boolean;
}

export interface MatchState {
  matchId: string;
  homeTeam: string;
  awayTeam: string;
  selections: Record<string, MarketTick>;
}

interface MarketState {
  matches: Record<string, MatchState>;
  isConnected: boolean;
  isReconnecting: boolean;
  connectionError: string | null;
  connect: (token: string) => void;
  disconnect: () => void;
}

// --------------------------------------------------------------------------- //
// Module-level singleton state (never captured stale by closures)
// --------------------------------------------------------------------------- //
let activeSocket: WebSocket | null = null;
let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
let reconnectAttempts: number = 0;
let isIntentionallyDisconnected: boolean = false;
let currentToken: string = "";
let pingTimer: ReturnType<typeof setInterval> | null = null;

function stopPing(): void {
  if (pingTimer !== null) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}
// Tick coalescing buffer: last-write-wins per (match, selection), flushed once per
// animation frame. A burst of 500 messages in 16ms produces ONE store update.
// Bounded by market count, so it can't grow while a background tab pauses rAF.
let pendingTicks: Map<string, MarketTick> = new Map();
let flushHandle: number | null = null;

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //
function isMarketTick(value: unknown): value is MarketTick {
  if (value === null || typeof value !== "object") return false;
  const t = value as Record<string, unknown>;
  return (
    typeof t.matchId === "string" &&
    t.matchId.length > 0 &&
    typeof t.homeTeam === "string" &&
    typeof t.awayTeam === "string" &&
    typeof t.marketType === "string" &&
    typeof t.selection === "string" &&
    t.selection.length > 0 &&
    typeof t.odds === "number" &&
    Number.isFinite(t.odds) &&
    typeof t.trueProbability === "number" &&
    Number.isFinite(t.trueProbability) &&
    typeof t.isSuspended === "boolean"
  );
}

function ticksEqual(a: MarketTick, b: MarketTick): boolean {
  return (
    a.odds === b.odds &&
    a.trueProbability === b.trueProbability &&
    a.isSuspended === b.isSuspended &&
    a.marketType === b.marketType &&
    a.homeTeam === b.homeTeam &&
    a.awayTeam === b.awayTeam
  );
}

function clearReconnectTimer(): void {
  if (reconnectTimeout !== null) {
    clearTimeout(reconnectTimeout);
    reconnectTimeout = null;
  }
}

function cancelPendingFlush(): void {
  if (flushHandle !== null) {
    cancelAnimationFrame(flushHandle);
    flushHandle = null;
  }
  pendingTicks = new Map();
}

function flushTicks(): void {
  flushHandle = null;
  if (pendingTicks.size === 0) return;

  const batch: Map<string, MarketTick> = pendingTicks;
  pendingTicks = new Map();

  useMarketStore.setState((state) => {
    let nextMatches: Record<string, MatchState> | null = null;

    for (const tick of batch.values()) {
      // Read from the in-progress copy so several selections of the same match
      // in one batch accumulate correctly.
      const source: Record<string, MatchState> = nextMatches ?? state.matches;
      const existingMatch: MatchState | undefined = source[tick.matchId];
      const existingTick: MarketTick | undefined = existingMatch?.selections[tick.selection];

      // Identical tick: keep old references so no subscriber re-renders.
      if (existingTick !== undefined && ticksEqual(existingTick, tick)) continue;

      if (nextMatches === null) nextMatches = { ...state.matches };

      const existingSelections: Record<string, MarketTick> = existingMatch?.selections ?? {};
      nextMatches[tick.matchId] = {
        ...(existingMatch ?? { matchId: tick.matchId }),
        matchId: tick.matchId,
        homeTeam: tick.homeTeam,
        awayTeam: tick.awayTeam,
        selections: { ...existingSelections, [tick.selection]: tick },
      };
    }

    // Returning the same state object is a no-op in Zustand (Object.is check).
    return nextMatches === null ? state : { matches: nextMatches };
  });
}

function enqueueTicks(ticks: MarketTick[]): void {
  for (const tick of ticks) {
    pendingTicks.set(`${tick.matchId}\u0000${tick.selection}`, tick);
  }
  if (flushHandle === null) {
    flushHandle = requestAnimationFrame(flushTicks);
  }
}

function teardownSocket(): void {
  stopPing();
  const ws: WebSocket | null = activeSocket;
  activeSocket = null;
  if (ws === null) return;
  // Detach first so the old socket's async onclose can't trigger a reconnect loop.
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
  if (ws.readyState === WebSocket.CONNECTING) {
    // Closing mid-handshake logs a browser error; finish the handshake, then close.
    ws.onopen = (): void => ws.close(1000, "client teardown");
  } else {
    ws.onopen = null;
    if (ws.readyState === WebSocket.OPEN) ws.close(1000, "client teardown");
  }
}

function scheduleReconnect(): void {
  clearReconnectTimer();
  const backoff: number = Math.min(BASE_BACKOFF_MS * 2 ** reconnectAttempts, MAX_BACKOFF_MS);
  const delay: number = backoff + Math.random() * JITTER_MS;
  reconnectAttempts = Math.min(reconnectAttempts + 1, 30); // cap exponent growth

  useMarketStore.setState({ isConnected: false, isReconnecting: true });

  reconnectTimeout = setTimeout(() => {
    reconnectTimeout = null;
    if (!isIntentionallyDisconnected && currentToken) openSocket();
  }, delay);
}

function openSocket(): void {
  clearReconnectTimer();
  teardownSocket();

  let ws: WebSocket;
  try {
    ws = new WebSocket(`${wsUrl(LIVE_ODDS_PATH)}?token=${encodeURIComponent(currentToken)}`);
  } catch {
    scheduleReconnect();
    return;
  }
  activeSocket = ws;

  ws.onopen = (): void => {
    if (ws !== activeSocket) return;
    reconnectAttempts = 0;
    useMarketStore.setState({ isConnected: true, isReconnecting: false, connectionError: null });
    stopPing();
    pingTimer = setInterval(() => {
      if (ws === activeSocket && ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, PING_INTERVAL_MS);
  };

  ws.onmessage = (event: MessageEvent): void => {
    if (ws !== activeSocket || typeof event.data !== "string" || event.data === "pong") return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(event.data);
    } catch {
      return; // malformed frame: drop, never crash the feed
    }
    const items: unknown[] = Array.isArray(parsed) ? parsed : [parsed];
    const ticks: MarketTick[] = items.filter(isMarketTick);
    if (ticks.length > 0) enqueueTicks(ticks);
  };

  ws.onerror = (): void => {
    // Browsers always fire onclose after onerror; reconnect logic lives there.
  };

  ws.onclose = (event: CloseEvent): void => {
    if (ws !== activeSocket) return;
    activeSocket = null;
    stopPing();

    if (isIntentionallyDisconnected) {
      useMarketStore.setState({ isConnected: false, isReconnecting: false });
      return;
    }
    if (AUTH_FAILURE_CODES.has(event.code)) {
      useMarketStore.setState({
        isConnected: false,
        isReconnecting: false,
        connectionError: "Live feed rejected credentials",
      });
      return;
    }
    scheduleReconnect();
  };
}

// --------------------------------------------------------------------------- //
// Store
// --------------------------------------------------------------------------- //
export const useMarketStore = create<MarketState>()(() => ({
  matches: {},
  isConnected: false,
  isReconnecting: false,
  connectionError: null,

  connect: (token: string): void => {
    // Already live (or dialling) on this token: a repeat call is a no-op.
    if (token && token === currentToken && !isIntentionallyDisconnected && activeSocket !== null) return;
    isIntentionallyDisconnected = false;
    currentToken = token;
    reconnectAttempts = 0;
    clearReconnectTimer();
    if (!token) return;
    openSocket();
  },

  disconnect: (): void => {
    isIntentionallyDisconnected = true;
    clearReconnectTimer();
    cancelPendingFlush();
    teardownSocket();
    currentToken = "";
    reconnectAttempts = 0;
    useMarketStore.setState({
      matches: {},
      isConnected: false,
      isReconnecting: false,
      connectionError: null,
    });
  },
}));
