/**
 * Realtime wiring for a signed-in session.
 *
 * Backend writes publish to Redis -> `/ws/events` -> here -> `invalidate(...)` -> panels refetch.
 * SECTION_FANOUT encodes how sections depend on each other (an order touches the Arena, the Vault,
 * capital/risk and the dashboard), so one action anywhere updates every open view.
 */
import { invalidate } from "../lib/resource";
import { type RealtimeCommanderPayload, useCommanderStore } from "../store/useCommanderStore";
import { useMarketStore } from "../store/useMarketStore";
import { type BusEvent, useSystemStore } from "../store/useSystemStore";
import { toast } from "../store/useToastStore";
import { OMNI_WILDCARD_TOPIC, OmniSocket } from "./OmniGateway";

const MONEY_SECTIONS = ["arena", "vault", "ledger", "capital", "dashboard", "telemetry", "the-vault"];
// Group 114: what a real-money write moves: the vault, the Bet-Slip and the Oracle's P&L
const REAL_MONEY_FANOUT = [...MONEY_SECTIONS, "real_money", "bet-slip", "oracle"];

/** Backend section (first path segment after /api/v1/) -> resource prefixes to refetch. */
const SECTION_FANOUT: Record<string, string[]> = {
  execution: [...MONEY_SECTIONS, "commanders"],
  arena: [...MONEY_SECTIONS, "commanders"],
  admin: [...MONEY_SECTIONS, "commanders"],
  ledger: MONEY_SECTIONS,
  capital: ["capital", "dashboard", "vault", "core"],
  "control-panel": ["control-panel", "system", "dashboard", "core", "commanders"],
  telemetry: ["telemetry", "oracle", "arena", "vault", "dashboard"],
  hive: ["hive", "commanders", "dashboard"],
  rnd: ["rnd", "hive", "commanders"],
  lab: ["lab", "commanders"],
  core: ["core", "archive", "commanders"],
  exchanges: ["exchanges", "control-panel"],
  signals: ["signals", "arena"],
  // Fleet Command writes under /api/v1/omni/fleet; CFO executions, settlements and risk settings under /api/v1/omni
  omni: ["fleet", "cfo"],
  // Group 114: the real-money desk
  "real-money": REAL_MONEY_FANOUT,
};

function handleBusEvent(data: unknown): void {
  if (data === null || typeof data !== "object" || typeof (data as BusEvent).type !== "string") return;
  const event = data as BusEvent;
  if (event.type === "pong") return;
  useSystemStore.getState().pushEvent(event);

  if (event.type === "commanders") {
    // Paint the pushed statuses at once; the refetch then brings the full heartbeats
    useCommanderStore.getState().hydrateFromRealtime(event as unknown as RealtimeCommanderPayload);
    invalidate("commanders");
    return;
  }
  if (event.type === "settlement") {
    // Group 113: a sweep settled bets from final scores: every bankroll, open position and P&L view refetches
    invalidate(...MONEY_SECTIONS, "real_money", "oracle", "bet-slip", "commanders");
    const settled = Number((event as BusEvent & { settled?: number }).settled ?? 0);
    if (settled > 0) toast.info(`${settled} bet${settled === 1 ? "" : "s"} settled from final scores`, "Bankroll, open positions and P&L are updated.");
    return;
  }
  if (event.type === "real_money") {
    // Group 114: the desk's own event (placement, cashout, reconcile): every other open view refetches
    invalidate(...REAL_MONEY_FANOUT);
    return;
  }
  if (event.type === "fleet") {
    // A worker finished a run, a source changed state, or a quorum sweep settled
    invalidate("fleet");
    return;
  }
  if (event.type === "mutation" && event.section) {
    invalidate(...(SECTION_FANOUT[event.section] ?? [event.section]));
    if (event.path?.endsWith("/control-panel/emergency-stop")) {
      useSystemStore.getState().setHalted(true);
      toast.warning("Emergency stop engaged", "Trading is halted across every section.");
    }
  }
}

/** Connect every session-wide channel. Returns the teardown (sign-out / unmount). */
export function startRealtime(token: string): () => void {
  const bus = OmniSocket.channel("/ws/events");
  const offStatus = bus.onStatus((status) => useSystemStore.getState().setBusStatus(status));
  const offEvents = bus.subscribe(OMNI_WILDCARD_TOPIC, handleBusEvent);
  useMarketStore.getState().connect(token);

  return () => {
    offEvents();
    offStatus();
    useMarketStore.getState().disconnect();
    OmniSocket.closeAll();
    useSystemStore.getState().reset();
  };
}

/** Subscribe a component to a section channel (`/hive/board/live`, `/core/live`, `/omni/ws/stream`). */
export function subscribeChannel(path: string, handler: (data: unknown) => void, topicField = "type"): () => void {
  return OmniSocket.channel(path, { topicField }).subscribe(OMNI_WILDCARD_TOPIC, (data) => handler(data));
}
