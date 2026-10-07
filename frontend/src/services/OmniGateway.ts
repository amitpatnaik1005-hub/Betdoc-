/**
 * Omni-Gateway: the single WebSocket layer for every live BetDoc channel.
 *
 * One `OmniSocket` per backend channel (`OmniSocket.channel("/ws/events")`), all on the page's own
 * origin and authenticated with the signed-in user's JWT (`?token=`), which the backend checks before
 * the handshake completes. Ref-counted: the first subscriber opens the socket, the last one closes it.
 * Reconnects with jittered exponential backoff and pauses while the tab is hidden or offline.
 *
 * Provider credentials never reach the browser: third-party feeds are ingested server-side by the
 * Omni workers and arrive here over `/omni/ws/stream`.
 */
import { readToken, wsUrl } from "../api/client";

// ---------------------------------------------------------------- config
const readEnv = (key: string): string | undefined => {
  const value: unknown = (import.meta.env as Record<string, unknown>)[key];
  return typeof value === "string" && value.trim() !== "" ? value.trim() : undefined;
};

const readNumber = (key: string, fallback: number): number => {
  const parsed = Number(readEnv(key));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
};

const isBrowser = (): boolean => typeof window !== "undefined" && typeof window.location !== "undefined";

interface WsConfig {
  url: string;
  topicField: string;
  heartbeatMs: number;
  heartbeatPayload: string | undefined;
  maxReconnectAttempts: number;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
}

const channelConfig = (path: string, topicField: string): WsConfig => ({
  url: wsUrl(path),
  topicField,
  // Server-side relays answer "ping"; keeps proxies from reaping idle sockets.
  heartbeatMs: readNumber("VITE_OMNI_WS_HEARTBEAT_MS", 25_000),
  heartbeatPayload: readEnv("VITE_OMNI_WS_HEARTBEAT_PAYLOAD") ?? "ping",
  // Never give up on first-party channels; backoff is capped instead.
  maxReconnectAttempts: readNumber("VITE_OMNI_WS_MAX_RECONNECT_ATTEMPTS", Number.POSITIVE_INFINITY),
  reconnectBaseMs: readNumber("VITE_OMNI_WS_RECONNECT_BASE_MS", 1_000),
  reconnectMaxMs: readNumber("VITE_OMNI_WS_RECONNECT_MAX_MS", 30_000),
});

const backoffDelay = (attempt: number, baseMs: number, maxMs: number): number => {
  const ceiling = Math.min(maxMs, baseMs * 2 ** attempt);
  return ceiling / 2 + Math.random() * (ceiling / 2);
};

// ---------------------------------------------------------------- WebSocket layer
export type OmniSocketStatus = "idle" | "connecting" | "open" | "reconnecting" | "paused" | "closed" | "error";
export type OmniMessageHandler = (data: unknown, event: MessageEvent) => void;
export type OmniStatusListener = (status: OmniSocketStatus) => void;

export const OMNI_WILDCARD_TOPIC = "*";

export interface OmniSocketOptions {
  /** Provider-specific subscribe frame; called on (re)connect and when a new topic is added. */
  buildSubscribeMessage?: (topics: readonly string[]) => unknown;
  /** Provider-specific unsubscribe frame. */
  buildUnsubscribeMessage?: (topics: readonly string[]) => unknown;
  /** Close while the tab is hidden (mobile battery protection). */
  pauseWhenHidden?: boolean;
}

export class OmniSocket {
  private static readonly channels = new Map<string, OmniSocket>();

  private socket: WebSocket | null = null;
  private status: OmniSocketStatus = "idle";
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lifecycleBound = false;
  private readonly handlers = new Map<string, Set<OmniMessageHandler>>();
  private readonly statusListeners = new Set<OmniStatusListener>();
  private readonly config: WsConfig;
  private readonly options: OmniSocketOptions;

  private constructor(config: WsConfig, options: OmniSocketOptions) {
    this.config = config;
    this.options = options;
  }

  /** Shared socket for an API WebSocket path (e.g. `/ws/events`), created on first use. */
  static channel(path: string, options: OmniSocketOptions & { topicField?: string } = {}): OmniSocket {
    let socket = OmniSocket.channels.get(path);
    if (!socket) {
      const { topicField = "type", ...rest } = options;
      socket = new OmniSocket(channelConfig(path, topicField), { pauseWhenHidden: true, ...rest });
      OmniSocket.channels.set(path, socket);
    }
    return socket;
  }

  /** Drop every channel (sign-out): sockets close and nothing reconnects with the old token. */
  static closeAll(): void {
    for (const socket of OmniSocket.channels.values()) {
      socket.handlers.clear();
      socket.disconnect();
    }
    OmniSocket.channels.clear();
  }

  getStatus(): OmniSocketStatus {
    return this.status;
  }

  /** Ref-counted: the first subscriber opens the socket, the last unsubscribe closes it. */
  subscribe(topic: string, handler: OmniMessageHandler): () => void {
    const isNewTopic = !this.handlers.has(topic);
    const set = this.handlers.get(topic) ?? new Set<OmniMessageHandler>();
    set.add(handler);
    this.handlers.set(topic, set);

    if (!this.socket && this.status !== "paused") this.connect();
    else if (isNewTopic && topic !== OMNI_WILDCARD_TOPIC) this.sendSubscribe([topic]);

    return () => {
      const current = this.handlers.get(topic);
      if (!current) return;
      current.delete(handler);
      if (current.size === 0) {
        this.handlers.delete(topic);
        if (topic !== OMNI_WILDCARD_TOPIC) this.sendFrame(this.options.buildUnsubscribeMessage?.([topic]));
      }
      if (this.handlers.size === 0) this.disconnect();
    };
  }

  onStatus(listener: OmniStatusListener): () => void {
    this.statusListeners.add(listener);
    listener(this.status);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  publish(payload: unknown): boolean {
    return this.sendFrame(payload);
  }

  connect(): void {
    this.bindLifecycle();
    this.attempts = 0;
    this.open();
  }

  disconnect(): void {
    this.clearReconnect();
    this.stopHeartbeat();
    this.teardownSocket();
    this.unbindLifecycle();
    this.setStatus("closed");
  }

  // -------------------------------------------------------------- internals
  private open(): void {
    this.clearReconnect();
    if (this.socket) return;
    if (this.options.pauseWhenHidden && typeof document !== "undefined" && document.visibilityState === "hidden") {
      this.setStatus("paused");
      return;
    }
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      this.setStatus("paused");
      return;
    }

    const token = readToken();
    if (!token) {
      this.setStatus("closed");
      return;
    }
    const url = new URL(this.config.url);
    url.searchParams.set("token", token);

    this.setStatus(this.attempts > 0 ? "reconnecting" : "connecting");
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (error) {
      console.error("OmniSocket construction failed", error instanceof Error ? error.message : error);
      this.scheduleReconnect();
      return;
    }
    this.socket = ws;

    ws.onopen = () => {
      this.attempts = 0;
      this.setStatus("open");
      const topics = [...this.handlers.keys()].filter((t) => t !== OMNI_WILDCARD_TOPIC);
      if (topics.length > 0) this.sendSubscribe(topics);
      this.startHeartbeat();
    };
    ws.onmessage = (event: MessageEvent) => this.dispatch(event);
    ws.onerror = () => {
      // The browser hides error details; onclose follows and drives reconnection.
      this.setStatus("error");
    };
    ws.onclose = (event: CloseEvent) => {
      if (this.socket === ws) this.socket = null;
      this.stopHeartbeat();
      // 1008: the server rejected our credentials; retrying with the same token is pointless.
      if (this.handlers.size === 0 || event.code === 1008) {
        this.setStatus(event.code === 1008 ? "error" : "closed");
        return;
      }
      this.scheduleReconnect();
    };
  }

  private dispatch(event: MessageEvent): void {
    let data: unknown = event.data;
    if (typeof event.data === "string") {
      try {
        data = JSON.parse(event.data) as unknown;
      } catch {
        data = event.data;
      }
    }
    const field = this.config.topicField;
    const topic =
      data !== null && typeof data === "object" && field in data ? String((data as Record<string, unknown>)[field]) : undefined;

    const targets = [...(topic ? (this.handlers.get(topic) ?? []) : []), ...(this.handlers.get(OMNI_WILDCARD_TOPIC) ?? [])];
    for (const handler of targets) {
      try {
        handler(data, event);
      } catch (error) {
        console.error(`OmniSocket handler failed for topic "${topic ?? OMNI_WILDCARD_TOPIC}"`, error);
      }
    }
  }

  private sendSubscribe(topics: readonly string[]): void {
    this.sendFrame(this.options.buildSubscribeMessage?.(topics));
  }

  private sendFrame(payload: unknown): boolean {
    if (payload === undefined || !this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(typeof payload === "string" ? payload : JSON.stringify(payload));
    return true;
  }

  private scheduleReconnect(): void {
    if (this.handlers.size === 0) return;
    if (this.attempts >= this.config.maxReconnectAttempts) {
      this.setStatus("error");
      return;
    }
    const delay = backoffDelay(this.attempts, this.config.reconnectBaseMs, this.config.reconnectMaxMs);
    this.attempts += 1;
    this.setStatus("reconnecting");
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    const { heartbeatMs, heartbeatPayload } = this.config;
    if (heartbeatMs <= 0 || !heartbeatPayload) return;
    this.heartbeatTimer = setInterval(() => this.sendFrame(heartbeatPayload), heartbeatMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private teardownSocket(): void {
    const s = this.socket;
    this.socket = null;
    if (!s) return;
    s.onmessage = null;
    s.onerror = null;
    s.onclose = null;
    if (s.readyState === WebSocket.CONNECTING) {
      s.onopen = () => s.close(1000, "client teardown");
    } else {
      s.onopen = null;
      if (s.readyState === WebSocket.OPEN) s.close(1000, "client teardown");
    }
  }

  private setStatus(next: OmniSocketStatus): void {
    if (this.status === next) return;
    this.status = next;
    for (const listener of this.statusListeners) listener(next);
  }

  private readonly onVisibilityChange = (): void => {
    if (!this.options.pauseWhenHidden) return;
    if (document.visibilityState === "hidden") {
      this.clearReconnect();
      this.stopHeartbeat();
      this.teardownSocket();
      this.setStatus("paused");
    } else if (this.handlers.size > 0) {
      this.attempts = 0;
      this.open();
    }
  };

  private readonly onOnline = (): void => {
    if (this.handlers.size === 0) return;
    this.attempts = 0;
    this.open();
  };

  private readonly onOffline = (): void => {
    this.clearReconnect();
    this.stopHeartbeat();
    this.teardownSocket();
    this.setStatus("paused");
  };

  private bindLifecycle(): void {
    if (this.lifecycleBound || !isBrowser()) return;
    document.addEventListener("visibilitychange", this.onVisibilityChange);
    window.addEventListener("online", this.onOnline);
    window.addEventListener("offline", this.onOffline);
    this.lifecycleBound = true;
  }

  private unbindLifecycle(): void {
    if (!this.lifecycleBound) return;
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
    window.removeEventListener("online", this.onOnline);
    window.removeEventListener("offline", this.onOffline);
    this.lifecycleBound = false;
  }
}
