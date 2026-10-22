/**
 * The Pocket Twin (Group 109, point b.10), on the Command Center: install BetDoc on the phone's home screen, link a
 * Telegram chat to this account (a one-time code, sent as /link CODE or through the bot's deep link), and see which
 * phone alerts go where. The bot is the Sentinel's Telegram channel and only answers its configured chats.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { invalidate } from "../../lib/resource";
import { issueLinkCode, type LinkCode, PHONE_ALERT_LABEL, unlinkChat, usePocketStatus } from "../../lib/pocket_twin";
import { isIOS, isStandalone, useInstallPrompt } from "../../lib/pwa";
import { toast } from "../../store/useToastStore";
import { Async, Button, ConfirmButton, Panel } from "../../ui/kit";

const CHANNEL_LABEL: Record<string, string> = { TELEGRAM: "Telegram", TWILIO: "SMS", DISCORD: "Discord", PAGERDUTY: "PagerDuty", EMAIL: "Email", BROWSER: "the browser siren" };
const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");

const Ready = ({ ready, label, problem }: { ready: boolean; label: string; problem: string | null }) => (
  <span className="flex items-center gap-1.5 text-xs" title={problem ?? undefined}>
    <span className={`material-symbols-outlined text-[16px] ${ready ? "text-emerald-600 dark:text-emerald-400" : "text-stone-400"}`} aria-hidden="true">
      {ready ? "check_circle" : "radio_button_unchecked"}
    </span>
    <span className="font-semibold text-stone-700 dark:text-stone-200">{label}</span>
    <span className="text-stone-500 dark:text-stone-400">{ready ? "ready" : problem ?? "not set up"}</span>
  </span>
);

const Countdown = ({ until, onExpire }: { until: number; onExpire: () => void }) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => {
      const t = Date.now();
      setNow(t);
      if (t >= until) onExpire();
    }, 1000);
    return () => window.clearInterval(id);
  }, [until, onExpire]);
  const left = Math.max(0, Math.round((until - now) / 1000));
  return <span className="font-mono">{left > 0 ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "expired"}</span>;
};

export const PocketTwinPanel = ({ className }: { className?: string }) => {
  const status = usePocketStatus();
  const install = useInstallPrompt();
  const [code, setCode] = useState<(LinkCode & { until: number }) | null>(null);
  const [busy, setBusy] = useState(false);
  const installed = isStandalone();
  const clearCode = useCallback(() => setCode(null), []);

  const link = async () => {
    setBusy(true);
    try {
      const issued = await issueLinkCode();
      setCode({ ...issued, until: Date.now() + issued.expires_in_seconds * 1000 });
    } catch (err) {
      toast.error("No link code", refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const copy = (text: string) => void navigator.clipboard?.writeText(text).then(() => toast.success("Copied", text), () => undefined);
  const unlink = async (chatId: string) => {
    try {
      await unlinkChat(chatId);
      invalidate("pocket-twin");
      toast.info("Chat unlinked", "It no longer reads this account.");
    } catch (err) {
      toast.error("Not unlinked", refusal(err));
    }
  };

  return (
    <Panel title="Pocket Twin" icon="smartphone" className={className} updatedAt={status.updatedAt} subtitle="the desk on your phone: the app, Telegram, phone alerts">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <section aria-label="Install the app" className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
          <h3 className="flex items-center gap-1.5 text-sm font-bold text-stone-900 dark:text-stone-100">
            <span className="material-symbols-outlined text-[18px] text-[#C89B3C]" aria-hidden="true">install_mobile</span>The app
          </h3>
          {installed ? (
            <p className="text-xs text-stone-600 dark:text-stone-300">You are in the installed app. The thumb bar at the bottom reaches the Oracle, Score Prediction, the Consultant, your slip and The Vault.</p>
          ) : install.available ? (
            <>
              <p className="text-xs text-stone-600 dark:text-stone-300">Install BetDoc on this device: it opens full screen from the home screen, with the thumb bar on a phone.</p>
              <Button size="sm" variant="primary" icon="download" className="self-start"
                      onClick={() => void install.install().then((outcome) => outcome === "accepted" && toast.success("Installing BetDoc", "Find it on your home screen."))}>
                Install BetDoc
              </Button>
            </>
          ) : (
            <p className="text-xs text-stone-600 dark:text-stone-300">
              {isIOS() ? "On an iPhone or iPad: Safari's Share button, then Add to Home Screen." : "Open this page in Chrome or Edge on the phone, then the browser menu's Install app (or Add to Home Screen)."}
              {" "}It opens full screen, with the thumb bar along the bottom.
            </p>
          )}
          <p className="text-[11px] text-stone-500 dark:text-stone-400">Live prices, stakes and balances are always read fresh: the app caches its own screens, never your data.</p>
        </section>

        <Async resource={status}>
          {(s) => (
            <>
              <section aria-label="Telegram" className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
                <h3 className="flex items-center gap-1.5 text-sm font-bold text-stone-900 dark:text-stone-100">
                  <span className="material-symbols-outlined text-[18px] text-[#C89B3C]" aria-hidden="true">send</span>Telegram desk
                </h3>
                <Ready ready={s.telegram.ready} label="Bot" problem={s.telegram.problem} />
                {s.telegram.linked_chats.length > 0 ? (
                  <ul className="flex flex-col gap-1">
                    {s.telegram.linked_chats.map((c) => (
                      <li key={c.chat_id} className="flex items-center justify-between gap-2 text-xs">
                        <span className="font-mono text-stone-700 dark:text-stone-200">chat {c.chat_id}</span>
                        <ConfirmButton size="sm" variant="ghost" icon="link_off" confirmLabel="Unlink?" onConfirm={() => void unlink(c.chat_id)}>Unlink</ConfirmButton>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs text-stone-500 dark:text-stone-400">No chat is linked to this account yet.</p>
                )}
                {code ? (
                  <div className="flex flex-col gap-1.5 rounded-xl bg-white p-3 ring-1 ring-stone-200 dark:bg-stone-900 dark:ring-white/10">
                    <span className="text-[11px] text-stone-500 dark:text-stone-400">Send this to the bot from the Sentinel's chat (expires in <Countdown until={code.until} onExpire={clearCode} />):</span>
                    <button type="button" onClick={() => copy(code.command)} className="text-left font-mono text-lg font-bold tracking-wider text-stone-900 dark:text-stone-100"
                            aria-label={`Copy ${code.command}`}>
                      {code.command} <span className="material-symbols-outlined align-[-3px] text-[16px] text-stone-400" aria-hidden="true">content_copy</span>
                    </button>
                    {code.deep_link && (
                      <a href={code.deep_link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 self-start text-xs font-semibold text-[var(--accent-text)] hover:underline">
                        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">open_in_new</span>Open the bot and link in one tap
                      </a>
                    )}
                  </div>
                ) : (
                  <Button size="sm" icon="link" busy={busy} className="self-start" onClick={() => void link()}>Link Telegram</Button>
                )}
                <p className="text-[11px] leading-relaxed text-stone-500 dark:text-stone-400">
                  {s.telegram.commands.filter((c) => !["/start", "/help", "/link", "/unlink", "/halt", "/resume"].includes(c)).join(" · ")}. Nothing is placed or moved from a chat.
                </p>
              </section>

              <section aria-label="Phone alerts" className="flex flex-col gap-2 rounded-2xl bg-stone-50 p-4 dark:bg-white/[0.03]">
                <h3 className="flex items-center gap-1.5 text-sm font-bold text-stone-900 dark:text-stone-100">
                  <span className="material-symbols-outlined text-[18px] text-[#C89B3C]" aria-hidden="true">notifications_active</span>Phone alerts
                </h3>
                <div className="flex flex-col gap-1">
                  <Ready {...s.phone_alerts.channels.telegram} label="Telegram" />
                  <Ready {...s.phone_alerts.channels.sms} label="SMS" />
                </div>
                <ul className="flex flex-col gap-1 text-xs text-stone-600 dark:text-stone-300">
                  {s.phone_alerts.kinds.map((k) => <li key={k} className="flex items-start gap-1.5"><span aria-hidden="true">•</span>{PHONE_ALERT_LABEL[k] ?? k}</li>)}
                </ul>
                <p className="text-[11px] text-stone-500 dark:text-stone-400">
                  Routed to {s.phone_alerts.routes_to.length ? s.phone_alerts.routes_to.map((c) => CHANNEL_LABEL[c] ?? c.toLowerCase()).join(", ") : "no channel"}{" "}
                  by the Sentinel's matrix (Control Panel); a channel that is not set up is skipped, never reported sent.
                </p>
              </section>
            </>
          )}
        </Async>
      </div>
    </Panel>
  );
};
