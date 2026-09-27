const BASE_URL = "http://localhost:8000/api/v1";
const REQUEST_TIMEOUT_MS = 10_000;
const TOKEN_KEY = "token";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

interface FastApiValidationItem {
  loc?: Array<string | number>;
  msg?: string;
  type?: string;
}

type HttpMethod = "GET" | "POST";

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

async function request<T>(
  method: HttpMethod,
  endpoint: string,
  body?: unknown,
  isFormData: boolean = false,
): Promise<T> {
  const controller = new AbortController();
  const timeoutId: number = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  const token: string | null = localStorage.getItem(TOKEN_KEY);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;

  const init: RequestInit = { method, headers, signal: controller.signal };
  if (method !== "GET") {
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
      localStorage.removeItem(TOKEN_KEY);
      window.location.reload();
      throw new ApiError("Session Expired", 401);
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

export const apiClient = {
  async get<T = unknown>(endpoint: string): Promise<T> {
    return request<T>("GET", endpoint);
  },

  async post<T = unknown>(
    endpoint: string,
    body: unknown,
    isFormData: boolean = false,
  ): Promise<T> {
    return request<T>("POST", endpoint, body, isFormData);
  },
};
