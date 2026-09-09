import { create } from 'zustand';
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
  setActiveModel: (model: ModelName) => void;
  selectMarket: (market: BoardMarket) => void;
  closeBetslip: () => void;
  appendMessage: (message: ScoutMessage) => void;
  reserve: (payload: LedgerPlacement, stakePaise: number) => void;
  reconcile: (snapshot: WalletSnapshot) => void;
}
export const useBetStore = create<BetState>()(persist((set, get) => ({
  activeModel: 'System AI', bankroll: null, revision: -1, walletReady: false,
  oracleHistory: [], contextMarketId: null, selectedMarket: null,
  pending: null, lastReceipt: null,
  setActiveModel: (activeModel) => set({ activeModel, selectedMarket: null }),
  selectMarket: (selectedMarket) => set({ selectedMarket, contextMarketId: selectedMarket.market_id }),
  closeBetslip: () => set({ selectedMarket: null }),
  appendMessage: (message) => set((state) => ({
    oracleHistory: state.oracleHistory.some((item) => item.id === message.id)
      ? state.oracleHistory : [...state.oracleHistory, message],
  })),
  reserve: (payload, stakePaise) => {
    const state = get();
    if (state.pending || !state.walletReady || state.bankroll === null
      || !Number.isSafeInteger(stakePaise) || stakePaise <= 0 || stakePaise > state.bankroll) {
      throw new Error('Bankroll unavailable, insufficient, or a placement is still unresolved.');
    }
    set({ pending: { payload, stakePaise }, bankroll: state.bankroll - stakePaise, lastReceipt: null });
  },
  reconcile: (snapshot) => set((state) => {
    if (snapshot.revision < state.revision) return state;
    const terminal = state.pending !== null
      && snapshot.receipt?.idempotency_key === state.pending.payload.idempotency_key;
    const pending = terminal ? null : state.pending;
    return {
      bankroll: Math.max(0, snapshot.balance_paise - (pending?.stakePaise ?? 0)),
      revision: snapshot.revision, walletReady: true, pending,
      lastReceipt: terminal ? snapshot.receipt : state.lastReceipt,
    };
  }),
}), {
  name: 'betdoc-paper-session-v1',
  storage: createJSONStorage(() => sessionStorage),
  partialize: (state) => ({
    activeModel: state.activeModel, oracleHistory: state.oracleHistory,
    contextMarketId: state.contextMarketId, pending: state.pending,
  }),
}));

export function formatMoney(paise: number): string {
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' }).format(paise / 100);
}
