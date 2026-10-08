import { useEffect, useState, type FormEvent, type JSX } from "react";
import { motion } from "framer-motion";
import { MOCK_AUTH, useAuthStore } from "../store/useAuthStore";
import { BetdocLogo } from "../ui/brand";
import { Button, SPRING, Segmented, inputClass } from "../ui/kit";

// Mirrors backend UserCreate: 3-64 chars of a-z 0-9 _ . - (lower-cased), password 8-72 bytes.
const USERNAME_RE = /^[a-z0-9_.-]{3,64}$/;

type Mode = "signin" | "register";

export default function LoginForm(): JSX.Element {
  const login = useAuthStore((s) => s.login);
  const register = useAuthStore((s) => s.register);
  const error = useAuthStore((s) => s.error);
  const isLoading = useAuthStore((s) => s.isLoading);

  const [mode, setMode] = useState<Mode>("signin");
  const [username, setUsername] = useState<string>("");
  const [password, setPassword] = useState<string>("");

  // Follow the persisted theme so the gate matches the desk.
  useEffect(() => {
    const stored = window.localStorage.getItem("betdoc:theme");
    const dark = stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", dark);
  }, []);

  const normalized = username.trim().toLowerCase();
  const usernameOk = mode === "signin" ? normalized.length > 0 : USERNAME_RE.test(normalized);
  const passwordOk = mode === "signin" ? password.length > 0 : password.length >= 8 && new TextEncoder().encode(password).length <= 72;
  const canSubmit = usernameOk && passwordOk && !isLoading;

  const handleSubmit = async (e: FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    if (!canSubmit) return;
    if (mode === "signin") await login(username, password);
    else await register(normalized, password);
    setPassword("");
  };

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-[#F8F6F0] px-4 text-stone-900 dark:bg-stone-950 dark:text-stone-100">
      <motion.form
        onSubmit={handleSubmit}
        noValidate
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={SPRING}
        className="relative w-full max-w-sm space-y-6 rounded-[2rem] bg-white p-8 shadow-soft-lg sm:p-10 dark:bg-stone-900 dark:shadow-none dark:ring-1 dark:ring-inset dark:ring-white/[0.04]"
      >
        <div className="space-y-4">
          <BetdocLogo variant="full" />
          <div>
            <h1 className="font-display text-2xl font-bold tracking-[-0.02em] text-stone-900 dark:text-stone-100">
              {mode === "signin" ? "Welcome back" : "Open your desk"}
            </h1>
            <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
              {mode === "signin" ? "Sign in to your betting desk." : "A minute to set up, then you're in."}
            </p>
          </div>
        </div>

        <Segmented
          label="Account"
          value={mode}
          onChange={setMode}
          options={[
            { value: "signin", label: "Sign in" },
            { value: "register", label: "Create account" },
          ]}
        />

        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-stone-500 dark:text-stone-400">Username</span>
          <input
            name="username"
            type="text"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="operator"
            className={inputClass}
            disabled={isLoading}
          />
          {mode === "register" && normalized.length > 0 && !usernameOk && (
            <span className="text-[11px] text-amber-600 dark:text-amber-400">3-64 characters: letters, digits, _ . -</span>
          )}
        </label>

        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-stone-500 dark:text-stone-400">Password</span>
          <input
            name="password"
            type="password"
            autoComplete={mode === "signin" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••••"
            className={inputClass}
            disabled={isLoading}
          />
          {mode === "register" && <span className="text-[11px] text-stone-400">At least 8 characters.</span>}
        </label>

        {error && (
          <p role="alert" className="rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:bg-rose-400/10 dark:text-rose-300">
            {error}
          </p>
        )}

        <Button type="submit" variant="primary" disabled={!canSubmit} busy={isLoading} className="w-full py-3">
          {isLoading ? "Signing in…" : mode === "signin" ? "Sign in" : "Create account & sign in"}
        </Button>

        {MOCK_AUTH && (
          <p className="text-center text-[11px] text-amber-600 dark:text-amber-400">Mock auth is on (VITE_MOCK_AUTH): live sections need the backend.</p>
        )}
      </motion.form>
    </div>
  );
}
