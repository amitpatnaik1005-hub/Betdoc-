/**
 * The Pocket Twin as an installed app (Group 109, point b.10): the service worker (production builds only: in
 * development Vite serves modules the worker must not cache) and the browser's install prompt, kept from the moment it
 * fires so a button can offer it later. iOS has no prompt: there it is Share → Add to Home Screen.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useSyncExternalStore } from "react";

interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

let deferred: InstallPromptEvent | null = null;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((fn) => fn());

export const isStandalone = (): boolean =>
  typeof window !== "undefined" && (window.matchMedia?.("(display-mode: standalone)").matches || (navigator as Navigator & { standalone?: boolean }).standalone === true);

export const isIOS = (): boolean => typeof navigator !== "undefined" && /iphone|ipad|ipod/i.test(navigator.userAgent);

export function startPwa(): void {
  if (typeof window === "undefined") return;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();  // keep it for the Install button instead of the browser's mini bar
    deferred = event as InstallPromptEvent;
    notify();
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    notify();
  });
  if (import.meta.env.PROD && "serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        /* without the worker the app still runs; it is only not available offline */
      });
    });
  }
}

/** Whether the browser offered an install prompt (Chrome and Edge on Android and desktop). */
export const useInstallPrompt = (): { available: boolean; install: () => Promise<"accepted" | "dismissed" | "unavailable"> } => {
  const available = useSyncExternalStore((fn) => {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }, () => deferred !== null, () => false);
  return {
    available,
    install: async () => {
      if (!deferred) return "unavailable";
      const prompt = deferred;
      deferred = null;
      notify();
      await prompt.prompt();
      return (await prompt.userChoice).outcome;
    },
  };
};
