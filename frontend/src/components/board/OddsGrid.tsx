import React, { memo, useCallback, useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { EdgeBar } from './EdgeBar';
import { useLiveOdds } from '../../hooks/useLiveOdds';
import {
  useBetStore,
  useOddsMarket,
  useOddsMarketIds,
} from '../../store/useBetStore';

export type { OddsTick } from '../../store/useBetStore';

interface OddsRowProps {
  marketId: string;
  size: number;
  start: number;
  onSelect: (marketId: string) => void;
}

const OddsRow = memo(function OddsRow({
  marketId,
  size,
  start,
  onSelect,
}: OddsRowProps) {
  const item = useOddsMarket(marketId);
  const isSocketConnected = useBetStore((state) => state.isSocketConnected);

  const handleClick = useCallback(() => {
    onSelect(marketId);
  }, [marketId, onSelect]);

  const handleBetClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();

      if (isSocketConnected && item && !item.suspended) {
        onSelect(marketId);
      }
    },
    [isSocketConnected, item, marketId, onSelect],
  );

  if (!item) return null;

  const isEdge = !item.suspended && item.edge_percentage > 0;
  const canBet = isSocketConnected && !item.suspended;

  return (
    <div
      className="absolute top-0 left-0 w-full px-6 py-4 border-b-2 border-onyx/10 bg-white hover:bg-slate-50 transition-colors cursor-pointer"
      style={{
        height: `${size}px`,
        transform: `translateY(${start}px)`,
      }}
      onClick={handleClick}
    >
      <div className="flex justify-between items-start">
        <div>
          <h3 className="font-display font-bold text-onyx text-lg">
            {item.team_away}{' '}
            <span className="text-slate-400 font-normal">@</span>{' '}
            {item.team_home}
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
            {item.suspended
              ? '—'
              : item.sportsbook_odds > 0
                ? `+${item.sportsbook_odds}`
                : item.sportsbook_odds}
          </div>
          <button
            type="button"
            className="mt-2 px-6 py-1.5 bg-accent text-onyx text-sm font-bold uppercase tracking-wider border-2 border-onyx rounded-md shadow-solid hover:translate-y-px active:translate-y-1 active:shadow-none transition-all"
            disabled={!canBet}
            title={
              item.suspended
                ? 'Market suspended'
                : !isSocketConnected
                  ? 'Live odds disconnected'
                  : 'Select market'
            }
            onClick={handleBetClick}
          >
            Bet
          </button>
        </div>
      </div>
      <div className="mt-3 w-2/3">
        <EdgeBar
          modelWinChance={item.model_win_chance}
          impliedProb={item.implied_probability}
        />
      </div>
    </div>
  );
});

export const OddsGrid = () => {
  useLiveOdds();

  const parentRef = useRef<HTMLDivElement>(null);
  const marketIds = useOddsMarketIds();
  const setScoutContext = useBetStore((state) => state.setScoutContext);

  const getItemKey = useCallback(
    (index: number): string | number => marketIds[index] ?? index,
    [marketIds],
  );

  // Stable membership order prevents live edge changes from moving cards.
  const rowVirtualizer = useVirtualizer({
    count: marketIds.length,
    getScrollElement: () => parentRef.current,
    getItemKey,
    estimateSize: () => 130,
    overscan: 5,
  });

  const handleRowClick = useCallback(
    (marketId: string) => {
      setScoutContext(marketId);
    },
    [setScoutContext],
  );

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
          const marketId = marketIds[virtualRow.index];
          if (marketId === undefined) return null;

          return (
            <OddsRow
              key={virtualRow.key}
              marketId={marketId}
              size={virtualRow.size}
              start={virtualRow.start}
              onSelect={handleRowClick}
            />
          );
        })}
      </div>
    </div>
  );
};
