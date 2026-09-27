import { create } from "zustand";
import { apiClient } from "../api/client";

const TOKEN_KEY = "token";

interface LoginResponse {
  access_token: string;
  token_type: string;
}

interface AuthState {
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  login: (u: string, p: string) => Promise<void>;
  logout: () => void;
}

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage disabled (privacy mode, sandboxed iframe)
  }
}

export const useAuthStore = create<AuthState>()((set) => {
  const initialToken: string | null = readToken();

  return {
    token: initialToken,
    isAuthenticated: initialToken !== null,
    isLoading: false,
    error: null,

    login: async (u: string, p: string): Promise<void> => {
      set({ isLoading: true, error: null });
      try {
        const form = new URLSearchParams();
        form.set("username", u.trim());
        form.set("password", p); // never trim passwords

        const data = await apiClient.post<LoginResponse>("/auth/login", form, true);

        if (!data || typeof data.access_token !== "string" || data.access_token.length === 0) {
          throw new Error("Malformed login response");
        }

        localStorage.setItem(TOKEN_KEY, data.access_token);
        set({ token: data.access_token, isAuthenticated: true, error: null });
      } catch (err: unknown) {
        set({
          token: null,
          isAuthenticated: false,
          error: err instanceof Error ? err.message : "Login failed",
        });
      } finally {
        set({ isLoading: false });
      }
    },

    logout: (): void => {
      localStorage.removeItem(TOKEN_KEY);
      set({ token: null, isAuthenticated: false, isLoading: false, error: null });
    },
  };
});

// Cross-tab sync: logging out (or in) in one tab updates every other tab.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (e: StorageEvent) => {
    if (e.key !== TOKEN_KEY && e.key !== null) return;
    const token: string | null = readToken();
    useAuthStore.setState({ token, isAuthenticated: token !== null });
  });
}
