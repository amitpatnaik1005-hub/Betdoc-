import { useEffect, useState, type FormEvent, type JSX } from "react";
import { motion } from "framer-motion";
import { MOCK_AUTH, useAuthStore } from "../store/useAuthStore";
import { BetdocLogo } from "../ui/brand";

const INPUT_CLASS =
  "w-full rounded-xl bg-white px-3.5 py-2.5 text-sm text-slate-900 ring-1 ring-inset ring-slate-900/10 placeholder:text-slate-400 " +
  "focus:outline-none focus:ring-2 focus:ring-[#C89B3C] disabled:opacity-60 " +
  "dark:bg-white/[0.04] dark:text-slate-100 dark:ring-white/10";

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
    <div className="flex min-h-screen w-full items-center justify-center bg-[#F8F6F0] px-4 text-slate-900 dark:bg-[#121110] dark:text-slate-100">
      <div aria-hidden="true" className="pointer-events-none fixed inset-0 bg-[radial-gradient(600px_circle_at_70%_20%,rgba(200,155,60,0.18),transparent)]" />
      <motion.form
        onSubmit={handleSubmit}
        noValidate
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 300, damping: 30 }}
        className="relative w-full max-w-sm space-y-5 rounded-3xl bg-white p-7 shadow-xl ring-1 ring-slate-900/[0.06] dark:bg-[#161514] dark:ring-white/[0.08]"
      >
        <div className="space-y-3">
          <BetdocLogo variant="full" />
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
            {mode === "signin" ? "Execution desk access" : "Open a desk account"}
          </p>
        </div>

        <div className="grid grid-cols-2 gap-1 rounded-xl bg-slate-100 p-1 dark:bg-white/[0.05]" role="tablist">
          {(["signin", "register"] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              onClick={() => setMode(m)}
              className={`rounded-lg py-1.5 text-xs font-semibold transition-colors ${
                mode === m ? "bg-white text-slate-900 shadow-sm dark:bg-white/10 dark:text-white" : "text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"
              }`}
            >
              {m === "signin" ? "Sign in" : "Create account"}
            </button>
          ))}
        </div>

        <label className="block space-y-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">Username</span>
          <input
            name="username"
            type="text"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="operator"
            className={INPUT_CLASS}
            disabled={isLoading}
          />
          {mode === "register" && normalized.length > 0 && !usernameOk && (
            <span className="text-[11px] text-amber-600 dark:text-amber-400">3-64 characters: letters, digits, _ . -</span>
          )}
        </label>

        <label className="block space-y-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">Password</span>
          <input
            name="password"
            type="password"
            autoComplete={mode === "signin" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••••"
            className={INPUT_CLASS}
            disabled={isLoading}
          />
          {mode === "register" && <span className="text-[11px] text-slate-400">At least 8 characters.</span>}
        </label>

        {error && (
          <p role="alert" className="rounded-xl bg-rose-50 px-3 py-2 text-sm text-rose-700 ring-1 ring-inset ring-rose-600/20 dark:bg-rose-500/10 dark:text-rose-300">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={!canSubmit}
          className="w-full rounded-xl bg-gradient-to-tr from-[#9F7A2A] via-[#C89B3C] to-[#E3BE63] py-2.5 text-sm font-bold tracking-wide text-white shadow-[0_8px_28px_-8px_rgba(200,155,60,0.55)] transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isLoading ? "Authenticating…" : mode === "signin" ? "Sign in" : "Create account & sign in"}
        </button>

        {MOCK_AUTH && (
          <p className="text-center text-[11px] text-amber-600 dark:text-amber-400">Mock auth is on (VITE_MOCK_AUTH): live sections need the backend.</p>
        )}
      </motion.form>
    </div>
  );
}
