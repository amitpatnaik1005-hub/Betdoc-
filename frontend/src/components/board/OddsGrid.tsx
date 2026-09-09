import React, { useMemo, useCallback, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { EdgeBar } from "./EdgeBar";
import { useBetStore } from "../../store/useBetStore";

// Using the strict types defined in the prompt
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

// Temporary mock data since backend WebSocket isn't live yet
const mockData: OddsTick[] = Array.from({ length: 1000 }).map((_, i) => {
  const edge = (Math.random() * 0.15) - 0.05; // -5% to +10% edge
  const implied = 0.5 + (Math.random() * 0.1 - 0.05);
  return {
    market_id: `m${i}`, 
    team_home: ["Chiefs", "Eagles", "Bills", "Ravens", "49ers"][i % 5], 
    team_away: ["Packers", "Dolphins", "Cowboys", "Bengals", "Lions"][(i+1) % 5], 
    market_type: ["moneyline", "spread", "total"][i % 3] as any, 
    sportsbook_odds: Math.floor(Math.random() * 200) - 150, 
    implied_probability: implied, 
    model_win_chance: implied + edge, 
    edge_percentage: edge 
  };
});

export const OddsGrid = () => {
  const parentRef = useRef<HTMLDivElement>(null);
  // Zustand actions
  const setScoutContext = useBetStore(state => state.setScoutContext);

  // Aggressive Memoization as commanded
  const sortedData = useMemo(() => {
    return [...mockData].sort((a, b) => b.edge_percentage - a.edge_percentage);
  }, []);

  const rowVirtualizer = useVirtualizer({
    count: sortedData.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 130, // Estimated height of a card
    overscan: 5,
  });

  const handleRowClick = useCallback((market_id: string) => {
    setScoutContext(market_id);
  }, [setScoutContext]);

  return (
    <div 
      ref={parentRef} 
      className="h-[calc(100vh-140px)] overflow-auto rounded-xl border-2 border-onyx bg-white shadow-solid scrollbar-hide"
    >
      <div
        className="w-full relative bg-slate-50"
        style={{ height: `${rowVirtualizer.getTotalSize()}px` }}
      >
        {rowVirtualizer.getVirtualItems().map((virtualRow) => {
          const item = sortedData[virtualRow.index];
          const isEdge = item.edge_percentage > 0;

          return (
            <div
              key={virtualRow.key}
              className="absolute top-0 left-0 w-full px-6 py-4 border-b-2 border-onyx/10 bg-white hover:bg-slate-50 transition-colors cursor-pointer"
              style={{
                height: `${virtualRow.size}px`,
                transform: `translateY(${virtualRow.start}px)`,
              }}
              onClick={() => handleRowClick(item.market_id)}
            >
              <div className="flex justify-between items-start">
                <div>
                  <h3 className="font-display font-bold text-onyx text-lg">
                    {item.team_away} <span className="text-slate-400 font-normal">@</span> {item.team_home}
                  </h3>
                  <div className="flex gap-2 mt-2">
                    <span className="px-2 py-0.5 bg-slate-100 border border-slate-200 text-slate-600 rounded text-xs font-bold uppercase tracking-wider shadow-sm">
                      {item.market_type}
                    </span>
                    {isEdge && (
                      <span className="px-2 py-0.5 bg-primary/10 border border-primary/20 text-primary rounded text-xs font-bold uppercase tracking-wider flex items-center shadow-sm">
                        🔥 EDGE FOUND
                      </span>
                    )}
                  </div>
                </div>
                
                <div className="text-right">
                  <div className="font-mono text-2xl font-black text-onyx">
                    {item.sportsbook_odds > 0 ? `+${item.sportsbook_odds}` : item.sportsbook_odds}
                  </div>
                  <button className="mt-2 px-6 py-1.5 bg-accent text-onyx text-sm font-bold uppercase tracking-wider border-2 border-onyx rounded-md shadow-solid hover:translate-y-px active:translate-y-1 active:shadow-none transition-all">
                    Bet
                  </button>
                </div>
              </div>

              {/* The Edge Bar Visualization Component */}
              <div className="mt-3 w-2/3">
                <EdgeBar modelWinChance={item.model_win_chance} impliedProb={item.implied_probability} />
              </div>
              
            </div>
          );
        })}
      </div>
    </div>
  );
};
