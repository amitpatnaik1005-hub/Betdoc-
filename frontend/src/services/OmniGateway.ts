/**
 * Dual-layer Omni-Gateway: REST (Axios singleton) + WebSocket (pub/sub manager).
 *
 * SECURITY: every VITE_* value is compiled into the public JS bundle. Keys placed in
 * VITE_OMNI_REST_KEY / VITE_OMNI_WS_KEY are readable by anyone. Prefer pointing the URLs
 * at a backend proxy that holds the real keys server-side, and leave the key variables empty.
 */
import axios from "axios";
import type { AxiosError, AxiosInstance, AxiosRequestConfig, InternalAxiosRequestConfig } from "axios";

declare module "axios" {
  interface InternalAxiosRequestConfig {
    omniRetryCount?: number;
  }
}

// ---------------------------------------------------------------- config
type RestAuthMode = "header" | "bearer" | "query";
type WsAuthMode = "query" | "message" | "protocol";

const DEFAULTS = {
  restTimeoutMs: 15_000,
  restMaxRetries: 2,
  restRetryBaseMs: 500,
  restRetryMaxMs: 8_000,
  restAuthMode: "header" as RestAuthMode,
  restAuthParam: "X-API-Key",
  wsAuthMode: "query" as WsAuthMode,
  wsAuthParam: "apiKey",
  wsTopicField: "topic",
  wsMaxReconnectAttempts: 12,
  wsReconnectBaseMs: 1_000,
  wsReconnectMaxMs: 30_000,
} as const;

export class OmniConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OmniConfigError";
  }
}

const readEnv = (key: string): string | undefined => {
  const value: unknown = (import.meta.env as Record<string, unknown>)[key];
  return typeof value === "string" && value.trim() !== "" ? value.trim() : undefined;
};

const readNumber = (key: string, fallback: number): number => {
  const parsed = Number(readEnv(key));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
};

const readEnum = <T extends string>(key: string, allowed: readonly T[], fallback: T): T => {
  const value = readEnv(key);
  return value !== undefined && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
};

const isBrowser = (): boolean => typeof window !== "undefined" && typeof window.location !== "undefined";
const isSecurePage = (): boolean => isBrowser() && window.location.protocol === "https:";

const resolveHttpUrl = (raw: string): string => {
  const url = isBrowser() ? new URL(raw, window.location.origin) : new URL(raw);
  if (isSecurePage() && url.protocol === "http:") url.protocol = "https:";
  return url.toString().replace(/\/+$/, "");
};

const resolveWsUrl = (raw: string): URL => {
  const url = isBrowser() ? new URL(raw, window.location.origin) : new URL(raw);
  if (url.protocol === "https:" || url.protocol === "wss:") url.protocol = "wss:";
  else if (url.protocol === "http:" || url.protocol === "ws:") url.protocol = isSecurePage() ? "wss:" : "ws:";
  else throw new OmniConfigError(`Unsupported WebSocket protocol: ${url.protocol}`);
  return url;
};

interface RestConfig {
  baseURL: string;
  apiKey: string | undefined;
  authMode: RestAuthMode;
  authParam: string;
  timeoutMs: number;
  maxRetries: number;
  retryBaseMs: number;
  retryMaxMs: number;
}

const loadRestConfig = (): RestConfig => {
  const raw = readEnv("VITE_OMNI_REST_URL");
  if (!raw) throw new OmniConfigError("VITE_OMNI_REST_URL is not configured.");
  return {
    baseURL: resolveHttpUrl(raw),
    apiKey: readEnv("VITE_OMNI_REST_KEY"),
    authMode: readEnum("VITE_OMNI_REST_AUTH_MODE", ["header", "bearer", "query"], DEFAULTS.restAuthMode),
    authParam: readEnv("VITE_OMNI_REST_AUTH_PARAM") ?? DEFAULTS.restAuthParam,
    timeoutMs: readNumber("VITE_OMNI_REST_TIMEOUT_MS", DEFAULTS.restTimeoutMs),
    maxRetries: readNumber("VITE_OMNI_REST_MAX_RETRIES", DEFAULTS.restMaxRetries),
    retryBaseMs: readNumber("VITE_OMNI_REST_RETRY_BASE_MS", DEFAULTS.restRetryBaseMs),
    retryMaxMs: readNumber("VITE_OMNI_REST_RETRY_MAX_MS", DEFAULTS.restRetryMaxMs),
  };
};

interface WsConfig {
  url: string;
  apiKey: string | undefined;
  authMode: WsAuthMode;
  authParam: string;
  topicField: string;
  heartbeatMs: number;
  heartbeatPayload: string | undefined;
  maxReconnectAttempts: number;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
}

const loadWsConfig = (): WsConfig => {
  const raw = readEnv("VITE_OMNI_WS_URL");
  if (!raw) throw new OmniConfigError("VITE_OMNI_WS_URL is not configured.");
  return {
    url: resolveWsUrl(raw).toString(),
    apiKey: readEnv("VITE_OMNI_WS_KEY"),
    authMode: readEnum("VITE_OMNI_WS_AUTH_MODE", ["query", "message", "protocol"], DEFAULTS.wsAuthMode),
    authParam: readEnv("VITE_OMNI_WS_AUTH_PARAM") ?? DEFAULTS.wsAuthParam,
    topicField: readEnv("VITE_OMNI_WS_TOPIC_FIELD") ?? DEFAULTS.wsTopicField,
    heartbeatMs: readNumber("VITE_OMNI_WS_HEARTBEAT_MS", 0),
    heartbeatPayload: readEnv("VITE_OMNI_WS_HEARTBEAT_PAYLOAD"),
    maxReconnectAttempts: readNumber("VITE_OMNI_WS_MAX_RECONNECT_ATTEMPTS", DEFAULTS.wsMaxReconnectAttempts),
    reconnectBaseMs: readNumber("VITE_OMNI_WS_RECONNECT_BASE_MS", DEFAULTS.wsReconnectBaseMs),
    reconnectMaxMs: readNumber("VITE_OMNI_WS_RECONNECT_MAX_MS", DEFAULTS.wsReconnectMaxMs),
  };
};

const backoffDelay = (attempt: number, baseMs: number, maxMs: number): number => {
  const ceiling = Math.min(maxMs, baseMs * 2 ** attempt);
  return ceiling / 2 + Math.random() * (ceiling / 2);
};

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- REST layer
export class OmniGatewayError extends Error {
  readonly status: number | null;
  readonly code: string | null;
  readonly path: string | null;
  readonly body: unknown;

  constructor(message: string, status: number | null, code: string | null, path: string | null, body: unknown) {
    super(message);
    this.name = "OmniGatewayError";
    this.status = status;
    this.code = code;
    this.path = path;
    this.body = body;
  }

  /** Never copies request config/headers/params, so credentials cannot leak into logs. */
  static fromAxios(error: AxiosError): OmniGatewayError {
    const status = error.response?.status ?? null;
    const path = error.config?.url ?? null;
    const message = status ? `Omni REST ${status} on ${path ?? "request"}` : `Omni REST network error (${error.code ?? "unknown"})`;
    return new OmniGatewayError(message, status, error.code ?? null, path, error.response?.data);
  }
}

const IDEMPOTENT_METHODS = new Set(["get", "head", "options", "put", "delete"]);

const retryAfterMs = (error: AxiosError): number | null => {
  const header: unknown = error.response?.headers?.["retry-after"];
  if (typeof header !== "string") return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(seconds * 1000, 0);
  const date = Date.parse(header);
  return Number.isFinite(date) ? Math.max(date - Date.now(), 0) : null;
};

export class OmniRest {
  private static instance: OmniRest | null = null;
  readonly http: AxiosInstance;

  private constructor(private readonly config: RestConfig) {
    this.http = axios.create({
      baseURL: config.baseURL,
      timeout: config.timeoutMs,
      headers: { Accept: "application/json" },
    });
    this.http.interceptors.request.use((request) => this.injectAuth(request));
    this.http.interceptors.response.use(undefined, (error: unknown) => this.handleError(error));
  }

  /** Lazily created: an unconfigured gateway never crashes app start-up. */
  static getInstance(): OmniRest {
    OmniRest.instance ??= new OmniRest(loadRestConfig());
    return OmniRest.instance;
  }

  static isConfigured(): boolean {
    return readEnv("VITE_OMNI_REST_URL") !== undefined;
  }

  get<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
    return this.http.get<T>(path, config).then((r) => r.data);
  }

  post<T>(path: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return this.http.post<T>(path, body, config).then((r) => r.data);
  }

  put<T>(path: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return this.http.put<T>(path, body, config).then((r) => r.data);
  }

  patch<T>(path: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
    return this.http.patch<T>(path, body, config).then((r) => r.data);
  }

  delete<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
    return this.http.delete<T>(path, config).then((r) => r.data);
  }

  private injectAuth(request: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
    const { apiKey, authMode, authParam } = this.config;
    if (!apiKey) return request;
    switch (authMode) {
      case "bearer":
        request.headers.set("Authorization", `Bearer ${apiKey}`);
        break;
      case "header":
        request.headers.set(authParam, apiKey);
        break;
      case "query": {
        const existing = (request.params ?? {}) as Record<string, unknown>;
        request.params = { ...existing, [authParam]: apiKey };
        break;
      }
    }
    return request;
  }

  private async handleError(error: unknown): Promise<never> {
    if (!axios.isAxiosError(error)) throw error;
    if (axios.isCancel(error) || error.code === "ERR_CANCELED") throw error;
    const request = error.config;
    const status = error.response?.status;
    const retriable =
      request !== undefined &&
      IDEMPOTENT_METHODS.has((request.method ?? "get").toLowerCase()) &&
      (status === undefined || status === 429 || status >= 500);
    const attempt = request?.omniRetryCount ?? 0;

    if (retriable && request && attempt < this.config.maxRetries) {
      request.omniRetryCount = attempt + 1;
      await sleep(retryAfterMs(error) ?? backoffDelay(attempt, this.config.retryBaseMs, this.config.retryMaxMs));
      return this.http.request(request);
    }
    throw OmniGatewayError.fromAxios(error);
  }
}

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
  private static instance: OmniSocket | null = null;

  private socket: WebSocket | null = null;
  private status: OmniSocketStatus = "idle";
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lifecycleBound = false;
  private readonly handlers = new Map<string, Set<OmniMessageHandler>>();
  private readonly statusListeners = new Set<OmniStatusListener>();

  private constructor(
    private readonly config: WsConfig,
    private readonly options: OmniSocketOptions,
  ) {}

  static getInstance(options: OmniSocketOptions = {}): OmniSocket {
    OmniSocket.instance ??= new OmniSocket(loadWsConfig(), { pauseWhenHidden: true, ...options });
    return OmniSocket.instance;
  }

  static isConfigured(): boolean {
    return readEnv("VITE_OMNI_WS_URL") !== undefined;
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

    const { apiKey, authMode, authParam } = this.config;
    const url = new URL(this.config.url);
    if (apiKey && authMode === "query") url.searchParams.set(authParam, apiKey);
    const protocols = apiKey && authMode === "protocol" ? [apiKey] : undefined;

    this.setStatus(this.attempts > 0 ? "reconnecting" : "connecting");
    let ws: WebSocket;
    try {
      ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url);
    } catch (error) {
      console.error("OmniSocket construction failed", error instanceof Error ? error.message : error);
      this.scheduleReconnect();
      return;
    }
    this.socket = ws;

    ws.onopen = () => {
      this.attempts = 0;
      this.setStatus("open");
      if (apiKey && authMode === "message") this.sendFrame({ [authParam]: apiKey });
      const topics = [...this.handlers.keys()].filter((t) => t !== OMNI_WILDCARD_TOPIC);
      if (topics.length > 0) this.sendSubscribe(topics);
      this.startHeartbeat();
    };
    ws.onmessage = (event: MessageEvent) => this.dispatch(event);
    ws.onerror = () => {
      // The browser hides error details; onclose follows and drives reconnection.
      this.setStatus("error");
    };
    ws.onclose = () => {
      if (this.socket === ws) this.socket = null;
      this.stopHeartbeat();
      if (this.handlers.size === 0) {
        this.setStatus("closed");
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
