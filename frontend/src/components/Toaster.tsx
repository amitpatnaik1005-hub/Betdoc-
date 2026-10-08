import { AnimatePresence, motion } from "framer-motion";
import { type ToastTone, useToastStore } from "../store/useToastStore";

const STYLE: Record<ToastTone, { icon: string; color: string }> = {
  success: { icon: "check_circle", color: "text-emerald-500" },
  error: { icon: "error", color: "text-rose-500" },
  warning: { icon: "warning", color: "text-amber-500" },
  info: { icon: "info", color: "text-sky-500" },
};

export const Toaster = () => {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);
  return (
    <div aria-live="polite" className="pointer-events-none fixed bottom-5 left-1/2 z-[60] flex w-[min(92vw,420px)] -translate-x-1/2 flex-col gap-2">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            layout
            initial={{ opacity: 0, y: 16, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.96 }}
            transition={{ type: "spring", stiffness: 420, damping: 32 }}
            role={t.tone === "error" ? "alert" : "status"}
            className="pointer-events-auto flex items-start gap-3 rounded-2xl bg-white/95 px-4 py-3 shadow-xl ring-1 ring-stone-900/10 backdrop-blur dark:bg-[#292524]/95 dark:ring-white/10"
          >
            <span className={`material-symbols-outlined mt-0.5 text-[20px] ${STYLE[t.tone].color}`}>{STYLE[t.tone].icon}</span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-stone-900 dark:text-stone-50">{t.title}</p>
              {t.detail && <p className="mt-0.5 break-words text-xs text-stone-500 dark:text-stone-400">{t.detail}</p>}
            </div>
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              className="rounded-xl p-0.5 text-stone-400 hover:text-stone-700 dark:hover:text-stone-200"
              aria-label="Dismiss notification"
            >
              <span className="material-symbols-outlined text-[18px]">close</span>
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
};
