import { create } from "zustand";

export type ToastTone = "success" | "error" | "info" | "warning";

export interface Toast {
  id: number;
  tone: ToastTone;
  title: string;
  detail?: string;
}

interface ToastState {
  toasts: Toast[];
  push: (toast: Omit<Toast, "id">) => void;
  dismiss: (id: number) => void;
}

const MAX_VISIBLE = 4;
const LIFETIME_MS: Record<ToastTone, number> = { success: 3500, info: 3500, warning: 6000, error: 7000 };
let nextId = 1;

export const useToastStore = create<ToastState>()((set, get) => ({
  toasts: [],
  push: (toast) => {
    const id = nextId++;
    set((s) => ({ toasts: [...s.toasts, { ...toast, id }].slice(-MAX_VISIBLE) }));
    window.setTimeout(() => get().dismiss(id), LIFETIME_MS[toast.tone]);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

export const toast = {
  success: (title: string, detail?: string) => useToastStore.getState().push({ tone: "success", title, detail }),
  error: (title: string, detail?: string) => useToastStore.getState().push({ tone: "error", title, detail }),
  info: (title: string, detail?: string) => useToastStore.getState().push({ tone: "info", title, detail }),
  warning: (title: string, detail?: string) => useToastStore.getState().push({ tone: "warning", title, detail }),
};
