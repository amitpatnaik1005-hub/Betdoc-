/**
 * Universal Sport Filter & State Management Store (Group 79)
 * Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet
 *
 * The desk's sport, league and market filter, kept across reloads. The nine sports mirror the backend's
 * adapter registry (`app/domain/sports/registry.py`); the model names are the adapters' model families.
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type SportKey =
  | 'soccer' | 'cricket' | 'basketball' | 'tennis' | 'baseball' | 'american_football' | 'ice_hockey' | 'combat' | 'esports';

export interface SportInfo {
  key: SportKey;
  name: string;
  /** Material Symbols glyph. */
  icon: string;
  modelType: string;
  primaryLeague: string;
  kellyMultiplier: number;
}

export const ALL_SPORTS: readonly SportInfo[] = [
  { key: 'soccer', name: 'Football', icon: 'sports_soccer', modelType: 'Dixon-Coles Poisson', primaryLeague: 'Premier League', kellyMultiplier: 0.35 },
  { key: 'cricket', name: 'Cricket', icon: 'sports_cricket', modelType: 'DLS & Dew Markov', primaryLeague: 'IPL & Tests', kellyMultiplier: 0.25 },
  { key: 'basketball', name: 'Basketball', icon: 'sports_basketball', modelType: 'Oliver Four Factors', primaryLeague: 'NBA', kellyMultiplier: 0.4 },
  { key: 'tennis', name: 'Tennis', icon: 'sports_tennis', modelType: 'Markov Serve/Return', primaryLeague: 'ATP Tour', kellyMultiplier: 0.3 },
  { key: 'baseball', name: 'Baseball', icon: 'sports_baseball', modelType: 'BaseRuns BsR & NRFI', primaryLeague: 'MLB', kellyMultiplier: 0.35 },
  { key: 'american_football', name: 'NFL', icon: 'sports_football', modelType: 'EPA Key Numbers', primaryLeague: 'NFL', kellyMultiplier: 0.5 },
  { key: 'ice_hockey', name: 'Ice Hockey', icon: 'sports_hockey', modelType: 'Corsi & Empty Net xG', primaryLeague: 'NHL', kellyMultiplier: 0.3 },
  { key: 'combat', name: 'MMA / UFC', icon: 'sports_mma', modelType: 'Strike Decay Matrix', primaryLeague: 'UFC Main Card', kellyMultiplier: 0.2 },
  { key: 'esports', name: 'Esports', icon: 'sports_esports', modelType: 'HLTV Veto Simulation', primaryLeague: 'CS2 Major', kellyMultiplier: 0.25 },
];

export const sportInfo = (key: string): SportInfo => ALL_SPORTS.find((s) => s.key === key) ?? ALL_SPORTS[0];

interface SportStoreState {
  activeSport: SportKey;
  activeLeague: string | null;
  activeMarketFilter: string;
  developerCredit: string;
  setActiveSport: (sportKey: SportKey) => void;
  setActiveLeague: (leagueKey: string | null) => void;
  setActiveMarketFilter: (market: string) => void;
}

export const useSportStore = create<SportStoreState>()(
  persist(
    (set) => ({
      activeSport: 'soccer',
      activeLeague: null,
      activeMarketFilter: 'all',
      developerCredit: 'Amit Ashok Kumar Patnaik',
      setActiveSport: (sportKey) => set({ activeSport: sportKey, activeLeague: null, activeMarketFilter: 'all' }),
      setActiveLeague: (leagueKey) => set({ activeLeague: leagueKey }),
      setActiveMarketFilter: (market) => set({ activeMarketFilter: market }),
    }),
    {
      name: 'betdoc-sport-storage',
      partialize: (s) => ({ activeSport: s.activeSport, activeLeague: s.activeLeague, activeMarketFilter: s.activeMarketFilter }),
    },
  ),
);
