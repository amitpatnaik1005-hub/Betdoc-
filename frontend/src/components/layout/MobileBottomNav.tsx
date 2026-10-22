/**
 * The Pocket Twin's thumb bar (Group 109, point b.10): five one-thumb targets along the bottom of a phone (under 768px;
 * wider screens keep the sidebar). The Oracle's match board, Score Prediction (The Lab), BetDoc Consultant, the
 * Oracle's slip on the manual workbench with its leg count, and The Vault. It sits above the home indicator
 * (safe-area inset) and under any full-screen sheet. Developed for Amit Ashok Kumar Patnaik.
 */
import { useLocation, useNavigate } from "react-router-dom";
import { useParlaySlipStore } from "../../store/useParlaySlipStore";

interface Tab {
  key: string;
  label: string;
  icon: string;
  to: string;
  active: (path: string, params: URLSearchParams) => boolean;
}

const TABS: Tab[] = [
  { key: "oracle", label: "Oracle", icon: "auto_awesome", to: "/oracle?tab=board", active: (p, q) => p === "/oracle" && q.get("tab") !== "builder" },
  { key: "score", label: "Score", icon: "scoreboard", to: "/lab#score-prediction", active: (p) => p === "/lab" },
  { key: "consultant", label: "Consultant", icon: "stethoscope", to: "/consultant", active: (p) => p === "/consultant" },
  { key: "slip", label: "Slip", icon: "confirmation_number", to: "/oracle?tab=builder&mode=manual", active: (p, q) => p === "/oracle" && q.get("tab") === "builder" },
  { key: "vault", label: "Vault", icon: "account_balance", to: "/vault", active: (p) => p === "/vault" },
];

export const MobileBottomNav = () => {
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const params = new URLSearchParams(search);
  const legs = useParlaySlipStore((s) => s.legs.length);

  return (
    <nav aria-label="Pocket Twin"
         className="fixed inset-x-0 bottom-0 z-40 border-t border-stone-200/80 bg-white/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-xl backdrop-saturate-150 md:hidden dark:border-white/10 dark:bg-stone-950/90">
      <ul className="mx-auto grid h-16 max-w-md grid-cols-5">
        {TABS.map((tab) => {
          const on = tab.active(pathname, params);
          return (
            <li key={tab.key} className="flex">
              <button type="button" onClick={() => navigate(tab.to)} aria-current={on ? "page" : undefined}
                      aria-label={tab.key === "slip" && legs ? `Slip, ${legs} leg${legs === 1 ? "" : "s"}` : tab.label}
                      className={`relative flex min-w-0 flex-1 flex-col items-center justify-center gap-0.5 transition-colors active:scale-95 ${
                        on ? "text-[#C89B3C] dark:text-[#E3BE63]" : "text-stone-500 dark:text-stone-400"}`}>
                <span className="relative">
                  <span className="material-symbols-outlined text-[24px]" aria-hidden="true" style={on ? { fontVariationSettings: "'FILL' 1" } : undefined}>{tab.icon}</span>
                  {tab.key === "slip" && legs > 0 && (
                    <span aria-hidden="true" className="absolute -right-2.5 -top-1 grid min-w-[18px] place-items-center rounded-full bg-[#C89B3C] px-1 font-mono text-[10px] font-bold leading-[18px] text-stone-950">
                      {legs}
                    </span>
                  )}
                </span>
                <span className="truncate text-[10px] font-semibold">{tab.label}</span>
                {on && <span aria-hidden="true" className="absolute top-0 h-0.5 w-8 rounded-full bg-[#C89B3C]" />}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
};
