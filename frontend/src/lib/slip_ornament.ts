/**
 * The Bet-Slip ornament (Group 106, ASHVATTHAMA, points b.7 and b.12): the collectible ticket of an approved, sealed
 * Consultant verdict, and its six delivery channels. Every figure comes from `/slip-ornament`, which issues a ticket
 * only from a gold-grade verdict whose seal still holds. The poster (channel 4) is drawn here as SVG from the same
 * figures and rasterised at high DPI; nothing on it is computed in the browser. Developed for Amit Ashok Kumar Patnaik.
 */
import { apiClient } from "../api/client";
import type { Tier } from "./backtest_and_parlay";
import { code39 } from "./barcode";
import { useResource } from "./resource";

export interface TicketLeg {
  number: number;
  leg_id: string;
  sport: string | null;
  league: string | null;
  fixture: string;
  kickoff: string | null;
  market: string;
  code: string;
  price: string | null;
  fair_probability: number | null;
  implied_probability: number | null;
  ev: number | null;
}

export interface TicketDelivery {
  channel: string;
  ok: boolean;
  deliveries: number;
  error: string | null;
  at: string;
}

export interface Ticket {
  ticket_no: string;
  report_id: string;
  signed_at: string;
  signed_at_local: string;
  age_minutes: number;
  fresh: boolean;
  book: string | null;
  book_label: string;
  badge: { foreground: string; background: string };
  verdict: { status: string; title: string; summary: string };
  slip_score: number;
  tier: Tier;
  kind: string;
  slip_title: string | null;
  legs: TicketLeg[];
  odds: string | null;
  stake_inr: string;
  stake_usd: string | null;
  inr_per_usd: string | null;
  payout_inr: string | null;
  net_winnings_inr: string | null;
  tds_inr: string | null;
  take_home_inr: string | null;
  tds_rate_pct: number | null;
  stop_loss_pct: number;
  stop_loss_floor_inr: string;
  joint_probability: number | null;
  joint_ev: number | null;
  seal: string;
  signed_by: string;
  architect: string;
  watermark: string;
  booking_code: string | null;
  booking_recorded_at: string | null;
  coupon_text: string;
  sms_text: string;
  whatsapp_url: string;
  mailto_url: string;
  sms_url: string;
  deliveries: TicketDelivery[];
  /** Group 114: the real-money ticket this slip was placed as, once the placement is confirmed */
  placement: TicketPlacement | null;
}

export interface TicketPlacement {
  ticket_reference: string;
  status: string;
  bookmaker: string;
  booking_code: string | null;
  placed_stake: string;
  placed_odds: string | null;
  placed_at: string | null;
  stop_loss_pct: number;
  stop_loss_floor_inr: string;
  monitor_active: boolean;
  current_win_prob: number | null;
  fair_value_inr: string | null;
  gross_return: string;
  settlement_payout: string;
  net_profit: string;
  tds_deducted: string;
  settled_at: string | null;
}

export interface TicketRow {
  report_id: string;
  issuable: boolean;
  ticket_no?: string;
  signed_at?: string;
  signed_at_local?: string;
  fresh?: boolean;
  book_label?: string;
  legs?: string[];
  odds?: string | null;
  stake_inr?: string;
  slip_score: number;
  tier: Tier;
  booking_code?: string | null;
  reason?: string;
  message?: string;
}

export type Via = "telegram" | "sms" | "email";

export interface ChannelReadiness {
  admin: boolean;
  channels: Partial<Record<Via, { channel: string; label: string; ready: boolean; problem: string | null }>>;
  message?: string;
}

export interface SendResult {
  channel: string;
  label: string;
  ok: boolean;
  deliveries: number;
  error: string | null;
  latency_ms: number;
}

export const useTickets = () => useResource("bet-slip:tickets", () => apiClient.get<{ tickets: TicketRow[] }>("/slip-ornament/tickets"), { intervalMs: 120_000 });
export const useTicket = (reportId: string | null) =>
  useResource(reportId ? `bet-slip:ticket:${reportId}` : null, () => apiClient.get<Ticket>(`/slip-ornament/${reportId}`), { intervalMs: 60_000 });
export const useOrnamentChannels = () => useResource("bet-slip:channels", () => apiClient.get<ChannelReadiness>("/slip-ornament/channels"), { intervalMs: 300_000 });

export const recordBooking = (reportId: string, code: string | null) => apiClient.put<Ticket>(`/slip-ornament/${reportId}/booking-code`, { booking_code: code });
export const sendTicket = (reportId: string, via: Via) => apiClient.post<SendResult>(`/slip-ornament/${reportId}/send-${via}`);

/** Hand a file to the browser's own download. */
export const saveBlob = (blob: Blob, filename: string) => {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
};

export const downloadDocx = async (ticket: Pick<Ticket, "report_id" | "ticket_no">) => {
  const { blob, filename } = await apiClient.download(`/slip-ornament/${ticket.report_id}/export-docx`);
  saveBlob(blob, filename ?? `betdoc-bet-slip-${ticket.ticket_no}.docx`);
};

export const inr = (value: string | number | null | undefined): string => {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 })}`;
};

// ---------------------------------------------------------------- channel 4: the poster
// Group 111: pure white paper, a gold foil strip, BetDoc's seal beside the book's badge, a Code 39 barcode of the ticket number
const POSTER = { width: 1080, paper: "#FFFFFF", gold: "#C89B3C", goldInk: "#8A6A1F", emerald: "#047857", ink: "#1C1917", muted: "#78716C", rule: "#E7E5E4", rose: "#BE123C" };
const SANS = "Inter, 'Segoe UI', Roboto, Arial, sans-serif";
const MONO = "'JetBrains Mono', Consolas, Menlo, monospace";

const xml = (value: unknown): string =>
  String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
const clip = (text: string, max: number): string => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

/** The ticket number's Code 39 bars, centred on cx and scaled to the width given (black on the white paper so it scans). */
const barcodeSvg = (value: string, cx: number, top: number, width: number, height: number, ink: string): string => {
  const code = code39(value);
  if (!code) return "";
  const k = width / code.width;
  const left = cx - width / 2;
  return code.bars.map((b) => `<rect x="${(left + b.x * k).toFixed(2)}" y="${top}" width="${(b.width * k).toFixed(2)}" height="${height}" fill="${ink}"/>`).join("");
};

/** The ticket as an SVG poster: the same layout and figures as the card, drawn for a 1080px-wide image. */
export const posterSvg = (t: Ticket): { svg: string; width: number; height: number } => {
  const P = POSTER;
  const W = P.width;
  const legTop = 330;
  const legHeight = 112;
  const moneyTop = legTop + t.legs.length * legHeight + 24;
  const barcodeTop = moneyTop + 232;
  const height = barcodeTop + 236;
  const text = (x: number, y: number, body: string, size: number, opts: { fill?: string; weight?: number; family?: string; anchor?: string; spacing?: number } = {}) =>
    `<text x="${x}" y="${y}" font-family="${xml(opts.family ?? SANS)}" font-size="${size}" font-weight="${opts.weight ?? 400}" fill="${opts.fill ?? P.ink}"` +
    `${opts.anchor ? ` text-anchor="${opts.anchor}"` : ""}${opts.spacing ? ` letter-spacing="${opts.spacing}"` : ""}>${xml(body)}</text>`;
  const legs = t.legs.map((leg, i) => {
    const y = legTop + i * legHeight;
    const where = [leg.sport, leg.league, leg.kickoff].filter(Boolean).join(" · ");
    return [
      i > 0 ? `<line x1="64" y1="${y}" x2="${W - 64}" y2="${y}" stroke="${P.rule}" stroke-width="2"/>` : "",
      `<circle cx="92" cy="${y + 44}" r="22" fill="#E7E5E4"/>`,
      text(92, y + 52, String(leg.number), 22, { weight: 700, family: MONO, anchor: "middle" }),
      text(132, y + 38, clip(leg.fixture, 40), 30, { weight: 800 }),
      text(132, y + 68, clip(where, 70), 20, { fill: P.muted }),
      text(132, y + 98, clip(`${leg.market}  [${leg.code}]`, 52), 24, { weight: 700, fill: P.emerald }),
      text(W - 72, y + 60, leg.price ?? "—", 40, { weight: 800, family: MONO, anchor: "end" }),
    ].join("");
  }).join("");
  const money = [
    ["KELLY STAKE", `${inr(t.stake_inr)}${t.stake_usd ? ` ($${t.stake_usd})` : ""}`, P.ink],
    ["GROSS RETURN", inr(t.payout_inr), P.ink],
    [`NET AFTER ${t.tds_rate_pct ?? 30}% TDS`, inr(t.take_home_inr), P.emerald],
    ["STOP-LOSS FLOOR", inr(t.stop_loss_floor_inr), P.rose],
  ].map(([label, value, colour], i) => {
    const x = 72 + i * ((W - 144) / 4);
    return text(x, moneyTop + 50, label, 16, { fill: P.muted, weight: 700, spacing: 2 }) + text(x, moneyTop + 92, value, 30, { weight: 800, family: MONO, fill: colour });
  }).join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${height}" viewBox="0 0 ${W} ${height}">
<rect x="0" y="0" width="${W}" height="${height}" fill="#F5F5F4"/>
<rect x="16" y="16" width="${W - 32}" height="${height - 32}" rx="36" fill="${P.paper}" stroke="${P.rule}" stroke-width="3"/>
<path d="M16 74 V52 a36 36 0 0 1 36 -36 H${W - 52} a36 36 0 0 1 36 36 V74 Z" fill="${P.gold}"/>
${text(64, 54, `BETDOC OFFICIAL BET-SLIP · ${t.ticket_no}`, 20, { fill: P.ink, weight: 800, family: MONO, spacing: 3 })}
${text(W - 64, 54, `${t.kind.replace(/_/g, " ").toUpperCase()} · ${t.legs.length} LEG${t.legs.length === 1 ? "" : "S"}`, 18, { fill: P.ink, weight: 700, family: MONO, anchor: "end", spacing: 2 })}
<line x1="18" y1="214" x2="${W - 18}" y2="214" stroke="#D6D3D1" stroke-width="3" stroke-dasharray="14 10"/>
<rect x="64" y="108" width="96" height="96" rx="24" fill="${P.ink}"/><rect x="66" y="110" width="92" height="92" rx="22" fill="none" stroke="${P.gold}" stroke-width="3" stroke-opacity="0.6"/>
${text(112, 168, "BD", 34, { weight: 900, anchor: "middle", fill: P.gold })}
${text(184, 128, "BETDOC QUANTITATIVE DESK", 20, { fill: P.ink, weight: 900, spacing: 1 })}
${text(184, 170, clip(t.verdict.title, 30), 36, { weight: 900, fill: P.emerald })}
${text(184, 202, clip(`Signed ${t.signed_at_local}`, 56), 20, { fill: P.muted })}
<rect x="${W - 300}" y="122" width="232" height="64" rx="16" fill="${xml(t.badge.background)}" stroke="${xml(t.badge.foreground)}" stroke-width="3"/>
${text(W - 184, 164, clip(t.book_label.toUpperCase(), 16), 26, { weight: 900, family: MONO, anchor: "middle", fill: t.badge.foreground, spacing: 2 })}
<rect x="18" y="236" width="${W - 36}" height="70" fill="#FAFAF9"/><line x1="18" y1="236" x2="${W - 18}" y2="236" stroke="${P.rule}" stroke-width="2"/>
<line x1="18" y1="306" x2="${W - 18}" y2="306" stroke="${P.rule}" stroke-width="2"/>
${text(64, 281, `SLIP SCORE ${Math.round(t.slip_score)}/100 ${t.tier}`, 26, { weight: 900, fill: P.goldInk })}
${text(W - 72, 281, `${t.kind.replace(/_/g, " ")}${t.odds ? ` @ ${t.odds}` : ""}`, 26, { weight: 800, family: MONO, anchor: "end" })}
${legs}
<rect x="18" y="${moneyTop}" width="${W - 36}" height="130" fill="#FAFAF9"/><line x1="18" y1="${moneyTop}" x2="${W - 18}" y2="${moneyTop}" stroke="${P.rule}" stroke-width="3"/>
${money}
${text(64, moneyTop + 170, `Booking code: ${t.booking_code ?? "not recorded yet"}`, 22, { weight: 700, family: MONO })}
${t.fresh ? "" : text(64, moneyTop + 202, `Prices as signed ${t.age_minutes} min ago: re-check them at ${t.book_label} before placing.`, 18, { fill: "#9A3412" })}
${barcodeSvg(t.ticket_no, W / 2, barcodeTop, 560, 72, P.ink)}
${text(W / 2, barcodeTop + 104, t.ticket_no.split("").join(" "), 20, { weight: 700, family: MONO, anchor: "middle" })}
<path d="M18 ${height - 104} H${W - 18} V${height - 52} a34 34 0 0 1 -34 34 H52 a34 34 0 0 1 -34 -34 Z" fill="${P.ink}"/>
${text(W / 2, height - 70, `SEAL ${t.seal.slice(0, 32)}…`, 17, { fill: "#E7E5E4", family: MONO, anchor: "middle", spacing: 1 })}
${text(W / 2, height - 40, `Official Quantitative Slip · Developed for ${t.architect || "Amit Ashok Kumar Patnaik"}`, 17, { fill: "#A8A29E", anchor: "middle" })}
</svg>`;
  return { svg, width: W, height };
};

/** Channel 4: the poster rasterised at `scale` (2 = a 2160px-wide PNG). */
export const posterPng = async (t: Ticket, scale = 2): Promise<Blob> => {
  const { svg, width, height } = posterSvg(t);
  const image = new Image();
  image.decoding = "async";
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("The poster could not be drawn"));
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  });
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const context = canvas.getContext("2d");
  if (!context) throw new Error("This browser cannot draw the poster");
  context.scale(scale, scale);
  context.drawImage(image, 0, 0, width, height);
  return new Promise<Blob>((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("No PNG was produced"))), "image/png"));
};
