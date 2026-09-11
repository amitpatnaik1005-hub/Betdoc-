import { create, useStore } from 'zustand';
import { createStore } from 'zustand/vanilla';
import { createJSONStorage, persist } from 'zustand/middleware';

export type ModelName = 'System AI' | 'Custom Aggressive';

export interface OddsTick {
  market_id: string;
  team_home: string;
  team_away: string;
  market_type: 'spread' | 'moneyline' | 'total';
  sportsbook_odds: number;
  implied_probability: number;
  model_win_chance: number;
  edge_percentage: number;
}

export interface OddsMessage {
  type: 'TICK' | 'SUSPEND';
  data: OddsTick;
}

export interface LiveMarket extends OddsTick {
  suspended: boolean;
}

export interface LedgerPlacement {
  idempotency_key: string;
  market_id: string;
  stake: number;
  model_used: string;
}

export interface ScoutMessage {
  id: string;
  role: 'user' | 'scout';
  content: string;
  context_market_id?: string;
}

export interface BookLine {
  sportsbook: string;
  decimal_odds: number | null;
  quoted_at: string | null;
}

export interface BoardMarket extends OddsTick {
  selection_label: string;
  fixture_id: string;
  lines: BookLine[];
  quoted_at: string;
}

export interface Receipt {
  idempotency_key: string;
  status: 'recorded' | 'rejected';
  reason: string;
}

export interface WalletSnapshot {
  balance_paise: number;
  revision: number;
  currency: 'INR';
  mode: 'paper';
  receipt: Receipt | null;
}

export interface PendingPlacement {
  payload: LedgerPlacement;
  stakePaise: number;
}

interface LiveMarketIndex {
  marketIds: readonly string[];
  revision: number;
}

// Keep the mutable index private; subscribers receive immutable row snapshots.
// Live ticks never pass through persist or rewrite sessionStorage.
const liveMarkets = new Map<string, LiveMarket>();
const liveMarketIndex = createStore<LiveMarketIndex>()(() => ({
  marketIds: [],
  revision: 0,
}));

function equalMarket(a: LiveMarket, b: LiveMarket): boolean {
  return (
    a.market_id === b.market_id &&
    a.team_home === b.team_home &&
    a.team_away === b.team_away &&
    a.market_type === b.market_type &&
    a.sportsbook_odds === b.sportsbook_odds &&
    a.implied_probability === b.implied_probability &&
    a.model_win_chance === b.model_win_chance &&
    a.edge_percentage === b.edge_percentage &&
    a.suspended === b.suspended
  );
}

function patchTicks(messages: readonly OddsMessage[]): void {
  const newIds: string[] = [];
  let changed = false;

  for (const message of messages) {
    const tick = message.data;
    const previous = liveMarkets.get(tick.market_id);
    const next: LiveMarket = {
      market_id: tick.market_id,
      team_home: tick.team_home,
      team_away: tick.team_away,
      market_type: tick.market_type,
      sportsbook_odds: tick.sportsbook_odds,
      implied_probability: tick.implied_probability,
      model_win_chance: tick.model_win_chance,
      edge_percentage: tick.edge_percentage,
      suspended: message.type === 'SUSPEND',
    };

    if (previous && equalMarket(previous, next)) continue;

    if (!previous) newIds.push(tick.market_id);
    liveMarkets.set(tick.market_id, next);
    changed = true;
  }

  if (!changed) return;

  liveMarketIndex.setState((state) => ({
    marketIds:
      newIds.length === 0
        ? state.marketIds
        : [...state.marketIds, ...newIds],
    revision: state.revision + 1,
  }));
}

export function useOddsMarketIds(): readonly string[] {
  return useStore(liveMarketIndex, (state) => state.marketIds);
}

export function useOddsMarket(marketId: string): LiveMarket | undefined {
  return useStore(liveMarketIndex, () => liveMarkets.get(marketId));
}

interface BetState {
  activeModel: ModelName;
  bankroll: number | null;
  revision: number;
  walletReady: boolean;
  oracleHistory: ScoutMessage[];
  contextMarketId: string | null;
  selectedMarket: BoardMarket | null;
  pending: PendingPlacement | null;
  lastReceipt: Receipt | null;
  isSocketConnected: boolean;
  setActiveModel: (model: ModelName) => void;
  setScoutContext: (marketId: string) => void;
  selectMarket: (market: BoardMarket) => void;
  closeBetslip: () => void;
  appendMessage: (message: ScoutMessage) => void;
  reserve: (payload: LedgerPlacement, stakePaise: number) => void;
  reconcile: (snapshot: WalletSnapshot) => void;
  setSocketConnected: (connected: boolean) => void;
  updateTick: (tick: OddsTick, type?: OddsMessage['type']) => void;
  updateTicks: (messages: readonly OddsMessage[]) => void;
}

export const useBetStore = create<BetState>()(
  persist(
    (set, get) => ({
      activeModel: 'System AI',
      bankroll: null,
      revision: -1,
      walletReady: false,
      oracleHistory: [],
      contextMarketId: null,
      selectedMarket: null,
      pending: null,
      lastReceipt: null,
      isSocketConnected: false,

      setActiveModel: (activeModel) =>
        set({ activeModel, selectedMarket: null }),

      setScoutContext: (contextMarketId) => {
        if (get().contextMarketId !== contextMarketId) {
          set({ contextMarketId });
        }
      },

      selectMarket: (selectedMarket) =>
        set({
          selectedMarket,
          contextMarketId: selectedMarket.market_id,
        }),

      closeBetslip: () => set({ selectedMarket: null }),

      appendMessage: (message) =>
        set((state) => ({
          oracleHistory: state.oracleHistory.some(
            (item) => item.id === message.id,
          )
            ? state.oracleHistory
            : [...state.oracleHistory, message],
        })),

      reserve: (payload, stakePaise) => {
        const state = get();

        if (
          state.pending ||
          !state.walletReady ||
          state.bankroll === null ||
          !Number.isSafeInteger(stakePaise) ||
          stakePaise <= 0 ||
          stakePaise > state.bankroll
        ) {
          throw new Error(
            'Bankroll unavailable, insufficient, or a placement is still unresolved.',
          );
        }

        set({
          pending: { payload, stakePaise },
          bankroll: state.bankroll - stakePaise,
          lastReceipt: null,
        });
      },

      reconcile: (snapshot) =>
        set((state) => {
          if (snapshot.revision < state.revision) return state;

          const terminal =
            state.pending !== null &&
            snapshot.receipt?.idempotency_key ===
              state.pending.payload.idempotency_key;

          const pending = terminal ? null : state.pending;

          return {
            bankroll: Math.max(
              0,
              snapshot.balance_paise - (pending?.stakePaise ?? 0),
            ),
            revision: snapshot.revision,
            walletReady: true,
            pending,
            lastReceipt: terminal ? snapshot.receipt : state.lastReceipt,
          };
        }),

      setSocketConnected: (isSocketConnected) => {
        if (get().isSocketConnected !== isSocketConnected) {
          set({ isSocketConnected });
        }
      },

      updateTick: (data, type = 'TICK') => patchTicks([{ type, data }]),

      updateTicks: patchTicks,
    }),
    {
      name: 'betdoc-paper-session-v1',
      storage: createJSONStorage(() => sessionStorage),
      partialize: (state) => ({
        activeModel: state.activeModel,
        oracleHistory: state.oracleHistory,
        contextMarketId: state.contextMarketId,
        pending: state.pending,
      }),
    },
  ),
);

export function formatMoney(paise: number): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
  }).format(paise / 100);
}
