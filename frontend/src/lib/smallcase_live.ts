/**
 * The live smallcases' push (`/ws/smallcases`): the viewer's own baskets, sized by KUMBHA, sent whenever a fresh read of
 * the market changes them. A pushed board replaces the `smallcase:board` resource exactly as a fetch would, so every
 * panel reading it (the ticker, the inspector, the Vault) updates at once; while the socket is down the board keeps its
 * 30-second poll. One socket per tab, opened by the first panel that needs it and closed with the last.
 * Developed for Amit Ashok Kumar Patnaik.
 */
import { useEffect, useSyncExternalStore } from "react";
import { OMNI_WILDCARD_TOPIC, OmniSocket, type OmniSocketStatus } from "../services/OmniGateway";
import { MOCK_AUTH } from "../store/useAuthStore";
import { publish } from "./resource";
import type { Board } from "./smallcase";

export const BOARD_KEY = "smallcase:board";

interface Feed {
  status: OmniSocketStatus;
  pushes: number;  // boards pushed so far: a change means the market moved
  pushedAt: number | null;
}

let feed: Feed = { status: "idle", pushes: 0, pushedAt: null };
const listeners = new Set<() => void>();
const set = (next: Partial<Feed>) => {
  feed = { ...feed, ...next };
  listeners.forEach((fn) => fn());
};
const subscribe = (fn: () => void) => {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
};

let users = 0;
let stop: (() => void) | null = null;

function open(): () => void {
  const socket = OmniSocket.channel("/ws/smallcases", { topicField: "type" });
  const offStatus = socket.onStatus((status) => set({ status }));
  const offFrames = socket.subscribe(OMNI_WILDCARD_TOPIC, (data) => {
    if (!data || typeof data !== "object" || (data as { type?: string }).type !== "board") return;
    publish(BOARD_KEY, (data as { board: Board }).board);
    set({ pushes: feed.pushes + 1, pushedAt: Date.now() });
  });
  return () => {
    offFrames();
    offStatus();
    set({ status: "idle" });
  };
}

/** Keep the push open while the calling panel is mounted; the socket's status. */
export function useSmallcaseFeed(): OmniSocketStatus {
  useEffect(() => {
    if (MOCK_AUTH) return;
    users += 1;
    if (users === 1) stop = open();
    return () => {
      users -= 1;
      if (users === 0 && stop) {
        stop();
        stop = null;
      }
    };
  }, []);
  return useSyncExternalStore(subscribe, () => feed.status, () => "idle");
}

/** How many boards have been pushed (and when the last was): a slip kept live re-prices when it changes. */
export const useMarketPulse = (): { pushes: number; pushedAt: number | null } => {
  const pushes = useSyncExternalStore(subscribe, () => feed.pushes, () => 0);
  const pushedAt = useSyncExternalStore(subscribe, () => feed.pushedAt, () => null);
  return { pushes, pushedAt };
};
