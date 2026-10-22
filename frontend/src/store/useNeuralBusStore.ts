/**
 * The neural event bus, live (Group 108, point b.9): the frames of `/ws/neural-bus` folded into the chain the pipeline
 * bar shows and a short trace. The socket forwards the Wire's own chains (system) and the viewer's, never another
 * user's. Developed for Amit Ashok Kumar Patnaik.
 */
import { create } from "zustand";
import type { ChainStep, ChainTrigger, NeuralChain, NeuralFrame } from "../lib/neural_bus";
import { invalidate } from "../lib/resource";
import { OMNI_WILDCARD_TOPIC, OmniSocket, type OmniSocketStatus } from "../services/OmniGateway";

const TRACE_LIMIT = 40;

export interface LiveChain {
  chain_id: string;
  trigger: ChainTrigger;
  mine: boolean;
  started_at: string;
  steps: Record<number, ChainStep>;
  done: boolean;
  outcome: string | null;
}

export interface TraceLine {
  key: string;
  chain_id: string;
  at: string;
  mine: boolean;
  fixture: string | null;
  step: ChainStep;
}

interface NeuralBusState {
  status: OmniSocketStatus;
  transport: "redis" | "in-process" | null;
  active: LiveChain | null;
  trace: TraceLine[];
  drawerOpen: boolean;
  setDrawerOpen: (open: boolean) => void;
  toggleDrawer: () => void;
  ingest: (frame: NeuralFrame) => void;
  /** A chain this browser started: applied whole, so the bar is right even if the socket missed its frames. */
  recordChain: (chain: NeuralChain) => void;
  /** Open `/ws/neural-bus`; returns the teardown. */
  connect: () => () => void;
}

const fixtureOf = (t: ChainTrigger): string | null => (t.fixture ? `${t.fixture.home} v ${t.fixture.away}` : null);

export const useNeuralBusStore = create<NeuralBusState>((set, get) => ({
  status: "idle",
  transport: null,
  active: null,
  trace: [],
  drawerOpen: false,
  setDrawerOpen: (open) => set({ drawerOpen: open }),
  toggleDrawer: () => set((s) => ({ drawerOpen: !s.drawerOpen })),

  ingest: (frame) => {
    if (frame.type === "connection_ack") {
      set({ transport: frame.transport });
      return;
    }
    if (frame.type === "pong") return;
    const { active } = get();
    if (frame.type === "chain_started") {
      // a chain of yours in flight keeps the bar; the Wire's own chains still reach the trace
      if (active && !active.done && active.mine && !frame.mine) return;
      set({ active: { chain_id: frame.chain_id, trigger: frame.trigger, mine: frame.mine, started_at: frame.started_at, steps: {}, done: false, outcome: null } });
      return;
    }
    if (frame.type === "step") {
      if (frame.step.status !== "RUNNING") {
        const line: TraceLine = { key: `${frame.chain_id}:${frame.step.number}`, chain_id: frame.chain_id, at: frame.step.finished_at ?? frame.step.started_at,
                                  mine: frame.mine, fixture: fixtureOf(frame.trigger), step: frame.step };
        set((s) => ({ trace: [line, ...s.trace.filter((l) => l.key !== line.key)].slice(0, TRACE_LIMIT) }));
      }
      if (active?.chain_id === frame.chain_id) set({ active: { ...active, steps: { ...active.steps, [frame.step.number]: frame.step } } });
      return;
    }
    if (frame.type === "chain") {
      invalidate("neural-bus", "the-wire:board");  // the recent chains, and the board's best legs the chain just re-read
      if (active?.chain_id === frame.chain_id) {
        set({ active: { ...active, steps: Object.fromEntries(frame.chain.steps.map((s) => [s.number, s])), done: true, outcome: frame.chain.outcome } });
      }
    }
  },

  recordChain: (chain) => {
    const fixture = fixtureOf(chain.trigger);
    const lines: TraceLine[] = [...chain.steps].reverse().map((step) => ({ key: `${chain.chain_id}:${step.number}`, chain_id: chain.chain_id,
                                                                            at: step.finished_at ?? step.started_at, mine: true, fixture, step }));
    set((s) => ({
      active: { chain_id: chain.chain_id, trigger: chain.trigger, mine: true, started_at: chain.started_at,
                steps: Object.fromEntries(chain.steps.map((st) => [st.number, st])), done: true, outcome: chain.outcome },
      trace: [...lines, ...s.trace.filter((l) => l.chain_id !== chain.chain_id)].slice(0, TRACE_LIMIT),
    }));
  },

  connect: () => {
    const socket = OmniSocket.channel("/ws/neural-bus", { topicField: "type" });
    const offStatus = socket.onStatus((status) => set({ status }));
    const offFrames = socket.subscribe(OMNI_WILDCARD_TOPIC, (data) => {
      if (data && typeof data === "object" && "type" in data) get().ingest(data as NeuralFrame);
    });
    return () => {
      offFrames();
      offStatus();
    };
  },
}));
