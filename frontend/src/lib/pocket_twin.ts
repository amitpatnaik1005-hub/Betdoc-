/**
 * The Pocket Twin (backend `/mobile`, Group 109, point b.10): the Telegram desk's state, one-time link codes, and
 * which phone alerts go where. The bot is the Sentinel's Telegram channel (its token stays in the vault); the alerts
 * are the Sentinel's phone row. Developed for Amit Ashok Kumar Patnaik.
 */
import { apiClient } from "../api/client";
import { useResource } from "./resource";

export interface ChannelReadiness {
  channel: string;
  label: string;
  ready: boolean;
  problem: string | null;
}

export interface PocketStatus {
  telegram: ChannelReadiness & { bot: string | null; commands: string[]; linked_chats: { chat_id: string; linked_at: string | null }[] };
  phone_alerts: { kinds: string[]; routes_to: string[]; channels: Record<"telegram" | "sms", ChannelReadiness> };
  app_link: string | null;
  developer_credit: string;
}

export interface LinkCode {
  code: string;
  command: string;
  expires_in_seconds: number;
  deep_link: string | null;
  issued_at: string;
}

export const usePocketStatus = () => useResource("pocket-twin:status", () => apiClient.get<PocketStatus>("/mobile/status"), { intervalMs: 60_000 });
export const issueLinkCode = () => apiClient.post<LinkCode>("/mobile/telegram/link-code");
export const unlinkChat = (chatId: string) => apiClient.delete<void>(`/mobile/telegram/links/${encodeURIComponent(chatId)}`);

/** What each phone alert is, in plain words (the Sentinel's alert kinds). */
export const PHONE_ALERT_LABEL: Record<string, string> = {
  TWIN_PULLOUT: "Stop-loss shield: cash out or hedge a placed bet",
  CHEF_DIAMOND: "Chef's Table: a DIAMOND parlay before kickoff",
  TWIN_SLIP_VETTED: "A slip cleared the twin's fortress",
  TWIN_SETTLED: "A placed bet settled",
  MARKET_HYPE: "The 08:00 market forecast",
};
