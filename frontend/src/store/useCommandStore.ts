import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';

// ---------------------------------------------------------------------------
// TYPES
// ---------------------------------------------------------------------------
export type ConnectionStatus =
  | 'DISCONNECTED'
  | 'CONNECTING'
  | 'CONNECTED'
  | 'SYNCING';

export interface ExchangeBalance {
  exchangeId: string;
  name: string;
  currency: string;
  available: number;
  exposure: number;
}

export interface UserSession {
  userId: string;
  role: 'ADMIN' | 'QUANT' | 'VIEWER';
  lastLogin: string;
}

export interface CommandState {
  // --- STATE ---
  session: UserSession | null;
  globalStatus: ConnectionStatus;
  exchangeBalances: ExchangeBalance[];
  isAutoExecutionArmed: boolean;

  // --- ACTIONS ---
  setSession: (session: UserSession | null) => void;
  setGlobalStatus: (status: ConnectionStatus) => void;
  updateBalance: (
    exchangeId: string,
    available: number,
    exposure: number
  ) => void;
  toggleAutoExecution: () => void;
  triggerKillSwitch: () => void;
}

// ---------------------------------------------------------------------------
// INITIAL EXCHANGE BALANCES
// ---------------------------------------------------------------------------
const INITIAL_BALANCES: ExchangeBalance[] = [
  {
    exchangeId: 'ex-001',
    name: 'Pinnacle Primary',
    currency: 'INR',
    available: 154000,
    exposure: 12000,
  },
  {
    exchangeId: 'ex-002',
    name: 'Betfair Exchange',
    currency: 'INR',
    available: 42000,
    exposure: 0,
  },
  {
    exchangeId: 'ex-003',
    name: 'Bookmaker.eu',
    currency: 'INR',
    available: 8900,
    exposure: 450,
  },
];

// ---------------------------------------------------------------------------
// STORE — Zustand v4 curried middleware signature
// ---------------------------------------------------------------------------
export const useCommandStore = create<CommandState>()(
  persist(
    (set) => ({
      // -----------------------------------------------------------------------
      // INITIAL STATE
      // -----------------------------------------------------------------------
      session: null,
      globalStatus: 'DISCONNECTED',
      exchangeBalances: INITIAL_BALANCES,
      isAutoExecutionArmed: false,

      // -----------------------------------------------------------------------
      // ACTIONS
      // -----------------------------------------------------------------------

      /**
       * Set or clear the active user session.
       */
      setSession: (session) =>
        set({ session }),

      /**
       * Update the global WebSocket connection status.
       */
      setGlobalStatus: (status) =>
        set({ globalStatus: status }),

      /**
       * Immutably update the available balance and exposure
       * for a single exchange by its ID.
       */
      updateBalance: (exchangeId, available, exposure) =>
        set((state) => ({
          exchangeBalances: state.exchangeBalances.map((ex) =>
            ex.exchangeId === exchangeId
              ? { ...ex, available, exposure }
              : ex
          ),
        })),

      /**
       * Flip the auto-execution armed flag.
       */
      toggleAutoExecution: () =>
        set((state) => ({
          isAutoExecutionArmed: !state.isAutoExecutionArmed,
        })),

      /**
       * KILL SWITCH — halts all execution and zeroes every balance.
       * Does NOT empty the exchangeBalances array; preserves exchange
       * metadata while setting available and exposure to 0.
       */
      triggerKillSwitch: () =>
        set((state) => ({
          globalStatus: 'DISCONNECTED',
          isAutoExecutionArmed: false,
          exchangeBalances: state.exchangeBalances.map((ex) => ({
            ...ex,
            available: 0,
            exposure: 0,
          })),
        })),
    }),
    {
      name: 'betdoc-command-storage',
      storage: createJSONStorage(() => localStorage),

      /**
       * Only persist session and isAutoExecutionArmed.
       * Runtime state (balances, connection status) is always
       * re-fetched fresh on mount — never stale from localStorage.
       */
      partialize: (state) => ({
        session: state.session,
        isAutoExecutionArmed: state.isAutoExecutionArmed,
      }),
    }
  )
);

// ---------------------------------------------------------------------------
// EXTERNAL SELECTOR HOOKS
// ---------------------------------------------------------------------------

/**
 * Returns the sum of available funds across all connected exchanges.
 */
export const useTotalAvailable = () =>
  useCommandStore((state) =>
    state.exchangeBalances.reduce((sum, ex) => sum + ex.available, 0)
  );

/**
 * Returns the total capital currently locked in active bets
 * across all exchanges.
 */
export const useTotalExposure = () =>
  useCommandStore((state) =>
    state.exchangeBalances.reduce((sum, ex) => sum + ex.exposure, 0)
  );
