// Relative by default so the Vite proxy (dev) or nginx (prod) fronts the API: one origin, no CORS.
const BASE_URL: string = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/+$/, "");
const REQUEST_TIMEOUT_MS = 15_000;
const TOKEN_KEY = "token";

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler = () => {
  localStorage.removeItem(TOKEN_KEY);
  window.location.reload();
};

/** The auth store registers a logout here, so an expired session returns to the login screen in place. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  onUnauthorized = handler;
}

export function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export class ApiError extends Error {
  readonly status: number;
  /** Machine-readable refusal code when the API sends one (`detail.reason`), e.g. BLOCKED_BY_DRAWDOWN. */
  readonly reason: string | null;
  /** The structured refusal body (`detail`), when the API sent one: e.g. `{ reason, message, problems }`. */
  readonly detail: Record<string, unknown> | null;

  constructor(message: string, status: number, reason: string | null = null, detail: Record<string, unknown> | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
    this.detail = detail;
  }
}

interface FastApiValidationItem {
  loc?: Array<string | number>;
  msg?: string;
  type?: string;
}

type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

function isAbortError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === "AbortError") ||
    (err instanceof Error && err.name === "AbortError")
  );
}

function toTransportError(err: unknown): ApiError {
  if (isAbortError(err)) return new ApiError("Network timeout", 0);
  return new ApiError("Network error: unable to reach server", 0);
}

function extractErrorMessage(payload: unknown, fallback: string): string {
  if (payload === null || typeof payload !== "object" || !("detail" in payload)) {
    return fallback;
  }

  const detail: unknown = (payload as { detail: unknown }).detail;

  if (typeof detail === "string" && detail.trim().length > 0) {
    return detail;
  }

  // Structured refusals: { reason, message, ... }
  if (detail !== null && typeof detail === "object" && !Array.isArray(detail)) {
    const message: unknown = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim().length > 0) return message;
  }

  if (Array.isArray(detail)) {
    const messages: string[] = detail
      .map((item: unknown): string | null => {
        if (item === null || typeof item !== "object") return null;
        const { loc, msg } = item as FastApiValidationItem;
        if (typeof msg !== "string") return null;
        const field: string = Array.isArray(loc)
          ? loc.filter((part) => part !== "body").join(".")
          : "";
        return field ? `${field}: ${msg}` : msg;
      })
      .filter((m): m is string => m !== null);

    if (messages.length > 0) return messages.join("; ");
  }

  return fallback;
}

function extractReason(payload: unknown): string | null {
  if (payload === null || typeof payload !== "object" || !("detail" in payload)) return null;
  const detail: unknown = (payload as { detail: unknown }).detail;
  if (detail === null || typeof detail !== "object" || Array.isArray(detail)) return null;
  const reason: unknown = (detail as { reason?: unknown }).reason;
  return typeof reason === "string" ? reason : null;
}

function extractDetail(payload: unknown): Record<string, unknown> | null {
  if (payload === null || typeof payload !== "object" || !("detail" in payload)) return null;
  const detail: unknown = (payload as { detail: unknown }).detail;
  return detail !== null && typeof detail === "object" && !Array.isArray(detail) ? (detail as Record<string, unknown>) : null;
}

async function request<T>(
  method: HttpMethod,
  endpoint: string,
  body?: unknown,
  isFormData: boolean = false,
): Promise<T> {
  const controller = new AbortController();
  const timeoutId: number = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  const token: string | null = readToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;

  const init: RequestInit = { method, headers, signal: controller.signal };
  if (method !== "GET" && body !== undefined) {
    if (!isFormData) headers["Content-Type"] = "application/json";
    init.body = isFormData ? (body as BodyInit) : JSON.stringify(body);
  }

  try {
    let response: Response;
    try {
      response = await fetch(`${BASE_URL}${endpoint}`, init);
    } catch (err: unknown) {
      throw toTransportError(err);
    }

    // Only a 401 on an authenticated request means "session expired".
    if (response.status === 401 && token) {
      onUnauthorized();
      throw new ApiError("Session expired. Please sign in again.", 401);
    }

    let text: string;
    try {
      text = await response.text();
    } catch (err: unknown) {
      throw toTransportError(err);
    }

    let payload: unknown = null;
    let parsed: boolean = false;
    if (text.length > 0) {
      try {
        payload = JSON.parse(text);
        parsed = true;
      } catch {
        payload = null;
      }
    }

    if (!response.ok) {
      throw new ApiError(
        extractErrorMessage(payload, `Request failed (${response.status} ${response.statusText})`),
        response.status,
        extractReason(payload),
        extractDetail(payload),
      );
    }

    if (text.length > 0 && !parsed) {
      throw new ApiError("Invalid JSON response from server", response.status);
    }

    return payload as T;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

/** `/vault/overview` + `{ currency: "INR", start: undefined }` -> `/vault/overview?currency=INR` */
export function withQuery(endpoint: string, query?: Query): string {
  if (!query) return endpoint;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${endpoint}?${qs}` : endpoint;
}

export const apiClient = {
  async get<T = unknown>(endpoint: string, query?: Query): Promise<T> {
    return request<T>("GET", withQuery(endpoint, query));
  },

  async post<T = unknown>(
    endpoint: string,
    body?: unknown,
    isFormData: boolean = false,
  ): Promise<T> {
    return request<T>("POST", endpoint, body, isFormData);
  },

  async put<T = unknown>(endpoint: string, body?: unknown): Promise<T> {
    return request<T>("PUT", endpoint, body);
  },

  async patch<T = unknown>(endpoint: string, body?: unknown): Promise<T> {
    return request<T>("PATCH", endpoint, body);
  },

  async delete<T = unknown>(endpoint: string): Promise<T> {
    return request<T>("DELETE", endpoint);
  },
};

/** Absolute ws(s):// URL for an API path, on the page's own origin (proxied like REST). */
export function wsUrl(path: string): string {
  const base = new URL(BASE_URL, window.location.origin);
  base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
  return `${base.toString().replace(/\/+$/, "")}${path}`;
}
