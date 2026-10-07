import { useState, type FormEvent, type JSX } from "react";
import { useAuthStore } from "../store/useAuthStore";

// NOTE: `[Webkit-box-shadow:...]` compiles to the invalid CSS property
// "Webkit-box-shadow" (the capitalised form is JS style-object syntax only),
// which browsers silently drop. These variants emit valid CSS.
const AUTOFILL_FIX =
  "[&:-webkit-autofill]:shadow-[inset_0_0_0_30px_#111827] " +
  "[&:-webkit-autofill]:[-webkit-text-fill-color:#d1d5db] " +
  "[&:-webkit-autofill]:[caret-color:#d1d5db]";

const INPUT_CLASS =
  "w-full rounded px-3 py-2 text-sm bg-gray-900 border border-gray-700 text-gray-100 " +
  "placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 " +
  "focus:border-emerald-500 " +
  AUTOFILL_FIX;

export default function LoginForm(): JSX.Element {
  const login = useAuthStore((s) => s.login);
  const error = useAuthStore((s) => s.error);
  const isLoading = useAuthStore((s) => s.isLoading);

  const [username, setUsername] = useState<string>("");
  const [password, setPassword] = useState<string>("");

  const isBlank: boolean = username.trim().length === 0 || password.length === 0;

  const handleSubmit = async (e: FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    if (isBlank || isLoading) return;
    await login(username, password);
    setPassword("");
  };

  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-gray-900 text-gray-100">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm bg-gray-800 border border-gray-700 rounded-lg p-6 space-y-4 shadow-xl"
        noValidate
      >
        <div className="space-y-1">
          <h1 className="text-lg font-semibold tracking-wide">BETDOC</h1>
          <p className="text-xs uppercase tracking-widest text-gray-400">Execution Desk Access</p>
        </div>

        <div className="space-y-1">
          <label htmlFor="username" className="block text-xs uppercase tracking-wide text-gray-400">
            Username
          </label>
          <input
            id="username"
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
        </div>

        <div className="space-y-1">
          <label htmlFor="password" className="block text-xs uppercase tracking-wide text-gray-400">
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••••"
            className={INPUT_CLASS}
            disabled={isLoading}
          />
        </div>

        {error && (
          <p role="alert" className="text-sm text-red-400">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={isBlank || isLoading}
          className="w-full rounded py-2 text-sm font-semibold bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-700 disabled:text-gray-500 disabled:cursor-not-allowed transition-colors"
        >
          {isLoading ? "AUTHENTICATING..." : "SIGN IN"}
        </button>
      </form>
    </div>
  );
}
