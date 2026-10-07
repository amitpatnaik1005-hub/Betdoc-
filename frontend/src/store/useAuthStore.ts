import { create } from "zustand";
import { apiClient, readToken, setUnauthorizedHandler } from "../api/client";

const TOKEN_KEY = "token";
// Offline UI testing only: VITE_MOCK_AUTH=true skips the FastAPI login. Any real API call made with
// the mock token is answered 401, so the live sections need the backend and a real account.
export const MOCK_AUTH: boolean = import.meta.env.VITE_MOCK_AUTH === "true";

interface LoginResponse {
  access_token: string;
  token_type: string;
}

export interface CurrentUser {
  id: string;
  username: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

interface AuthState {
  token: string | null;
  user: CurrentUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  login: (u: string, p: string) => Promise<void>;
  register: (u: string, p: string) => Promise<void>;
  loadProfile: () => Promise<void>;
  logout: () => void;
}

const message = (err: unknown, fallback: string): string => (err instanceof Error ? err.message : fallback);

export const useAuthStore = create<AuthState>()((set, get) => {
  const initialToken: string | null = readToken();

  const startSession = (token: string): void => {
    localStorage.setItem(TOKEN_KEY, token);
    set({ token, isAuthenticated: true, error: null });
    void get().loadProfile();
  };

  return {
    token: initialToken,
    user: null,
    isAuthenticated: initialToken !== null,
    isLoading: false,
    error: null,

    login: async (u: string, p: string): Promise<void> => {
      set({ isLoading: true, error: null });
      try {
        if (MOCK_AUTH) {
          // MOCK LOGIN FOR TESTING: Backend not running
          await new Promise(resolve => setTimeout(resolve, 500));
          startSession("mock_token_for_testing");
          return;
        }

        const form = new URLSearchParams();
        form.set("username", u.trim());
        form.set("password", p); // never trim passwords

        const data = await apiClient.post<LoginResponse>("/auth/login", form, true);
        if (!data || typeof data.access_token !== "string" || data.access_token.length === 0) {
          throw new Error("Malformed login response");
        }
        startSession(data.access_token);
      } catch (err: unknown) {
        set({ token: null, isAuthenticated: false, error: message(err, "Login failed") });
      } finally {
        set({ isLoading: false });
      }
    },

    register: async (u: string, p: string): Promise<void> => {
      set({ isLoading: true, error: null });
      try {
        await apiClient.post("/auth/register", { username: u.trim(), password: p });
      } catch (err: unknown) {
        set({ isLoading: false, error: message(err, "Registration failed") });
        return;
      }
      // Registration also provisions the account's risk mandate server-side; sign straight in.
      await get().login(u, p);
    },

    loadProfile: async (): Promise<void> => {
      if (MOCK_AUTH || !get().token) return;
      try {
        set({ user: await apiClient.get<CurrentUser>("/auth/me") });
      } catch {
        // A 401 already logged us out via the unauthorized handler; anything else keeps the session.
      }
    },

    logout: (): void => {
      localStorage.removeItem(TOKEN_KEY);
      set({ token: null, user: null, isAuthenticated: false, isLoading: false, error: null });
    },
  };
});

// An expired or revoked token returns to the login screen in place (no reload loop).
setUnauthorizedHandler(() => {
  if (MOCK_AUTH) return;
  useAuthStore.getState().logout();
  useAuthStore.setState({ error: "Your session expired. Please sign in again." });
});

// Cross-tab sync: logging out (or in) in one tab updates every other tab.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (e: StorageEvent) => {
    if (e.key !== TOKEN_KEY && e.key !== null) return;
    const token: string | null = readToken();
    useAuthStore.setState({ token, isAuthenticated: token !== null, user: token ? useAuthStore.getState().user : null });
  });
}
