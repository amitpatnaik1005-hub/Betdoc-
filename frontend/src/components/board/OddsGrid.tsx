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
      className="absolute top-0 left-0 w-full px-4 py-2 border-b border-slate-900/[0.04] dark:border-white/[0.04] bg-white dark:bg-[#161514] hover:bg-[#F8F6F0] dark:hover:bg-white/[0.02] transition-colors cursor-pointer flex flex-col justify-center"
      style={{
        height: `${size}px`,
        transform: `translateY(${start}px)`,
      }}
      onClick={handleClick}
    >
      <div className="flex justify-between items-center w-full">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="font-medium text-slate-800 dark:text-[#E8E6E3] text-sm tracking-tight">
              {item.team_away}{' '}
              <span className="text-slate-400 dark:text-[#8A8783] font-normal mx-0.5">@</span>{' '}
              {item.team_home}
            </h3>
            <span className="px-1.5 py-0.5 bg-slate-100 border border-slate-200 text-slate-600 dark:bg-white/[0.04] dark:border-white/[0.08] dark:text-[#A6A39E] rounded text-[10px] font-medium uppercase tracking-wide">
              {item.market_type}
            </span>
            {isEdge && (
              <span className="px-1.5 py-0.5 bg-[#C89B3C]/10 border border-[#C89B3C]/20 text-[#A87F2C] dark:bg-[#C89B3C]/15 dark:border-[#C89B3C]/30 dark:text-[#E0B85A] rounded text-[10px] font-medium uppercase tracking-wide flex items-center">
                🔥 EDGE
              </span>
            )}
          </div>
          <div className="w-64">
            <EdgeBar
              modelWinChance={item.model_win_chance}
              impliedProb={item.implied_probability}
            />
          </div>
        </div>

        <div className="flex items-center gap-4">
          <div className="font-medium text-base text-slate-900 dark:text-[#E8E6E3] font-mono tracking-tight text-right w-16">
            {item.suspended
              ? '—'
              : item.sportsbook_odds > 0
                ? `+${item.sportsbook_odds}`
                : item.sportsbook_odds}
          </div>
          <button
            type="button"
            className="px-4 py-1.5 bg-[#C89B3C]/10 text-[#C89B3C] text-xs font-semibold uppercase tracking-wide border border-[#C89B3C]/30 dark:border-[#E0B85A]/30 rounded hover:bg-[#C89B3C]/20 active:bg-[#C89B3C]/30 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none transition-colors"
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
    estimateSize: () => 64, // denser rows
    overscan: 10,
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
      className="h-[calc(100vh-140px)] overflow-auto rounded-xl border border-slate-900/[0.08] dark:border-white/[0.1] bg-white dark:bg-[#161514] shadow-[0_8px_28px_-8px_rgba(200,155,60,0.25)] dark:shadow-[0_8px_28px_-8px_rgba(200,155,60,0.18)] scrollbar-hide"
    >
      <div
        className="w-full relative bg-[#F8F6F0] dark:bg-[#121110]"
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
