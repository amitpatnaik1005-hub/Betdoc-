/**
 * Realtime wiring for a signed-in session.
 *
 * Backend writes publish to Redis -> `/ws/events` -> here -> `invalidate(...)` -> panels refetch.
 * SECTION_FANOUT encodes how sections depend on each other (an order touches the Arena, the Vault,
 * capital/risk and the dashboard), so one action anywhere updates every open view.
 */
import { invalidate } from "../lib/resource";
import { useMarketStore } from "../store/useMarketStore";
import { type BusEvent, useSystemStore } from "../store/useSystemStore";
import { toast } from "../store/useToastStore";
import { OMNI_WILDCARD_TOPIC, OmniSocket } from "./OmniGateway";

const MONEY_SECTIONS = ["arena", "vault", "ledger", "capital", "dashboard", "telemetry", "the-vault"];

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
};

function handleBusEvent(data: unknown): void {
  if (data === null || typeof data !== "object" || typeof (data as BusEvent).type !== "string") return;
  const event = data as BusEvent;
  if (event.type === "pong") return;
  useSystemStore.getState().pushEvent(event);

  if (event.type === "commanders") {
    invalidate("commanders");
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
