/**
 * The Bet-Slip's six delivery channels (Group 106, point b.12):
 * 1 the coupon text to the clipboard; 2 the print sheet (or Save as PDF, vector); 3 the editable Word ticket; 4 the
 * high-DPI PNG poster; 5 WhatsApp (a deep link) and Telegram; 6 SMS and email. Telegram, SMS and email go through the
 * Sentinel's own channels to the administrators' chats, phones and inboxes: an administrator's action, and a channel
 * that is not set up says so. Without one, SMS and email open the phone's or computer's own app with the text in.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { type ReactNode, useState } from "react";
import { ApiError } from "../../api/client";
import { downloadDocx, posterPng, saveBlob, sendTicket, type Ticket, useOrnamentChannels, type Via } from "../../lib/slip_ornament";
import { formatDateTime } from "../../lib/format";
import { invalidate } from "../../lib/resource";
import { toast } from "../../store/useToastStore";
import { Panel, Pill } from "../../ui/kit";

const refusal = (err: unknown): string => (err instanceof ApiError ? err.message : "No answer from the server");

const Tile = ({ n, icon, title, detail, onClick, busy, disabled, tone = "stone", children }: {
  n: number; icon: string; title: string; detail: string; onClick?: () => void; busy?: boolean; disabled?: boolean; tone?: "stone" | "emerald" | "sky"; children?: ReactNode;
}) => {
  const ring = { stone: "border-stone-200 hover:border-[#C89B3C] dark:border-white/[0.08]", emerald: "border-emerald-200 hover:border-emerald-400 dark:border-emerald-900/50",
                 sky: "border-sky-200 hover:border-sky-400 dark:border-sky-900/50" }[tone];
  return (
    <div className={`flex flex-col gap-1.5 rounded-xl border bg-white/70 p-3 transition dark:bg-white/[0.03] ${ring}`}>
      <button type="button" onClick={onClick} disabled={disabled || busy} className="flex items-start gap-2.5 text-left disabled:cursor-not-allowed disabled:opacity-50">
        <span className="material-symbols-outlined mt-0.5 text-[22px] text-stone-600 dark:text-stone-300" aria-hidden="true">{busy ? "hourglass_top" : icon}</span>
        <span className="min-w-0">
          <span className="block text-xs font-bold text-stone-900 dark:text-stone-100"><span className="font-mono text-[10px] text-stone-400">{n}</span> {title}</span>
          <span className="block text-[11px] text-stone-500 dark:text-stone-400">{detail}</span>
        </span>
      </button>
      {children}
    </div>
  );
};

export const ExportChannelsToolbar = ({ ticket }: { ticket: Ticket }) => {
  const channels = useOrnamentChannels();
  const [busy, setBusy] = useState<string | null>(null);
  const ready = (via: Via) => channels.data?.channels[via];

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };
  const copy = () => run("copy", async () => {
    try {
      await navigator.clipboard.writeText(ticket.coupon_text);
      toast.success("Coupon copied", `${ticket.legs.length} leg${ticket.legs.length === 1 ? "" : "s"} with ${ticket.book_label}'s own codes`);
    } catch {
      toast.error("The clipboard refused", "Select the text from the Word or print copy instead");
    }
  });
  const word = () => run("docx", async () => {
    try {
      await downloadDocx(ticket);
    } catch (err) {
      toast.error("No Word ticket", refusal(err));
    }
  });
  const poster = () => run("png", async () => {
    try {
      saveBlob(await posterPng(ticket, 2), `betdoc-bet-slip-${ticket.ticket_no}.png`);
    } catch (err) {
      toast.error("No poster", err instanceof Error ? err.message : "The poster could not be drawn");
    }
  });
  const send = (via: Via) => run(via, async () => {
    try {
      const r = await sendTicket(ticket.report_id, via);
      (r.ok ? toast.success : toast.error)(r.ok ? `Sent by ${r.label}` : `${r.label} did not deliver`, r.ok ? `${r.deliveries} delivery(ies) in ${r.latency_ms} ms` : (r.error ?? undefined));
      invalidate(`bet-slip:ticket:${ticket.report_id}`);
    } catch (err) {
      toast.error(`Not sent`, refusal(err));
    }
  });

  const admin = channels.data?.admin ?? false;
  const sentinel = (via: Via, label: string) => {
    const c = ready(via);
    if (!admin) return null;
    return (
      <button type="button" disabled={!c?.ready || busy !== null} onClick={() => void send(via)} title={c?.ready ? undefined : c?.problem ?? undefined}
              className="inline-flex items-center gap-1 self-start rounded-full bg-stone-900 px-2.5 py-1 text-[11px] font-bold text-white disabled:opacity-40 dark:bg-stone-100 dark:text-stone-900">
        <span className="material-symbols-outlined text-[13px]" aria-hidden="true">{busy === via ? "hourglass_top" : "send"}</span>
        {c?.ready ? `Send by ${label}` : `${label}: ${c?.problem ?? "checking"}`}
      </button>
    );
  };

  return (
    <Panel title="Six-channel delivery" icon="share" subtitle="nothing is placed from here" className="print:hidden">
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
        <Tile n={1} icon="content_copy" title="Copy the coupon" detail="Plain text with the book's own codes" onClick={() => void copy()} busy={busy === "copy"} />
        <Tile n={2} icon="print" title="Print or save as PDF" detail="The ticket alone, on white, vector" onClick={() => window.print()} />
        <Tile n={3} icon="description" title="Word document" detail="Editable, with the Consultant's report" onClick={() => void word()} busy={busy === "docx"} />
        <Tile n={4} icon="image" title="PNG poster" detail="2160 px wide, for sharing or keeping" onClick={() => void poster()} busy={busy === "png"} />
        <Tile n={5} icon="chat" title="WhatsApp" detail="Opens WhatsApp with the coupon in" tone="emerald" onClick={() => window.open(ticket.whatsapp_url, "_blank", "noopener,noreferrer")}>
          {sentinel("telegram", "Telegram")}
        </Tile>
        <Tile n={6} icon="mail" title="SMS or email" detail="Opens your own app with the text in" tone="sky" onClick={() => { window.location.href = ticket.mailto_url; }}>
          <a href={ticket.sms_url} className="self-start text-[11px] font-semibold text-sky-700 underline dark:text-sky-300">Open as SMS</a>
          {sentinel("sms", "SMS")}
          {sentinel("email", "Email")}
        </Tile>
      </div>
      {!admin && channels.data?.message && <p className="mt-2 text-[11px] text-stone-500 dark:text-stone-400">{channels.data.message}</p>}
      {ticket.deliveries.length > 0 && (
        <ul className="mt-3 flex flex-col gap-1 border-t border-stone-200 pt-3 text-[11px] dark:border-white/[0.08]" aria-label="Deliveries">
          {ticket.deliveries.map((d) => (
            <li key={`${d.channel}-${d.at}`} className="flex flex-wrap items-center gap-2">
              <Pill tone={d.ok ? "good" : "critical"} icon={d.ok ? "check_circle" : "error"}>{d.channel.toLowerCase()}</Pill>
              <span className="text-stone-500 dark:text-stone-400">{formatDateTime(d.at)}{d.ok ? ` · ${d.deliveries} delivered` : ` · ${d.error ?? "failed"}`}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
};
