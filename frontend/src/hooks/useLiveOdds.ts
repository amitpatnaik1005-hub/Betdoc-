import { useEffect } from 'react';
import { useBetStore } from '../store/useBetStore';
import type { OddsMessage, OddsTick } from '../store/useBetStore';

const SOCKET_URL = 'ws://localhost:8000/api/v1/stream/odds';
const MAX_RETRIES = 5;
const INITIAL_RETRY_DELAY_MS = 1_000;
const BATCH_INTERVAL_MS = 16;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isProbability(value: unknown): value is number {
  return isFiniteNumber(value) && value >= 0 && value <= 1;
}

function isOddsTick(value: unknown): value is OddsTick {
  if (!isRecord(value)) return false;

  return (
    typeof value.market_id === 'string' &&
    value.market_id.trim().length > 0 &&
    typeof value.team_home === 'string' &&
    typeof value.team_away === 'string' &&
    (value.market_type === 'spread' ||
      value.market_type === 'moneyline' ||
      value.market_type === 'total') &&
    isFiniteNumber(value.sportsbook_odds) &&
    isProbability(value.implied_probability) &&
    isProbability(value.model_win_chance) &&
    isFiniteNumber(value.edge_percentage)
  );
}

function parseMessage(raw: unknown): OddsMessage | null {
  if (typeof raw !== 'string') return null;

  try {
    const parsed: unknown = JSON.parse(raw);

    if (
      !isRecord(parsed) ||
      (parsed.type !== 'TICK' && parsed.type !== 'SUSPEND') ||
      !isOddsTick(parsed.data)
    ) {
      return null;
    }

    return { type: parsed.type, data: parsed.data };
  } catch {
    return null;
  }
}

function startConnection(): () => void {
  let disposed = false;
  let retries = 0;
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let batchTimer: ReturnType<typeof setTimeout> | null = null;

  const pending = new Map<string, OddsMessage>();

  const setConnected = (connected: boolean): void => {
    useBetStore.getState().setSocketConnected(connected);
  };

  const flush = (): void => {
    if (batchTimer !== null) {
      clearTimeout(batchTimer);
      batchTimer = null;
    }

    if (disposed || pending.size === 0) return;

    const batch = Array.from(pending.values());
    pending.clear();
    useBetStore.getState().updateTicks(batch);
  };

  const detach = (connection: WebSocket): void => {
    connection.onopen = null;
    connection.onmessage = null;
    connection.onerror = null;
    connection.onclose = null;
  };

  const close = (connection: WebSocket): void => {
    if (
      connection.readyState === WebSocket.CONNECTING ||
      connection.readyState === WebSocket.OPEN
    ) {
      try {
        connection.close();
      } catch {
        // Cleanup must not interrupt React unmounting or reconnection.
      }
    }
  };

  function scheduleReconnect(): void {
    if (disposed || retryTimer !== null || retries >= MAX_RETRIES) return;

    const delay = INITIAL_RETRY_DELAY_MS * 2 ** retries;
    retries += 1;

    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  }

  function disconnect(connection: WebSocket): void {
    if (disposed || socket !== connection) return;

    detach(connection);
    socket = null;
    close(connection);
    flush();
    setConnected(false);
    scheduleReconnect();
  }

  function connect(): void {
    if (disposed) return;

    let connection: WebSocket;

    try {
      connection = new WebSocket(SOCKET_URL);
    } catch {
      setConnected(false);
      scheduleReconnect();
      return;
    }

    socket = connection;

    connection.onopen = (): void => {
      if (disposed || socket !== connection) return;

      // The retry budget applies to this entire subscription lifetime.
      setConnected(true);
    };

    connection.onmessage = (event: MessageEvent<unknown>): void => {
      if (disposed || socket !== connection) return;

      const message = parseMessage(event.data);
      if (!message) return;

      // Wire order wins: SUSPEND disables a row; a later TICK resumes it.
      pending.set(message.data.market_id, message);

      if (batchTimer === null) {
        batchTimer = setTimeout(flush, BATCH_INTERVAL_MS);
      }
    };

    connection.onerror = (): void => disconnect(connection);
    connection.onclose = (): void => disconnect(connection);
  }

  setConnected(false);

  if (typeof WebSocket !== 'undefined') {
    connect();
  }

  return (): void => {
    disposed = true;

    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }

    if (batchTimer !== null) {
      clearTimeout(batchTimer);
      batchTimer = null;
    }

    pending.clear();

    const connection = socket;
    socket = null;

    if (connection) {
      detach(connection);
      close(connection);
    }

    setConnected(false);
  };
}

let subscribers = 0;
let stopConnection: (() => void) | null = null;

function subscribe(): () => void {
  subscribers += 1;

  if (subscribers === 1) {
    stopConnection = startConnection();
  }

  let released = false;

  return (): void => {
    if (released) return;
    released = true;
    subscribers -= 1;

    if (subscribers === 0) {
      stopConnection?.();
      stopConnection = null;
    }
  };
}

export function useLiveOdds(): void {
  useEffect(subscribe, []);
}
