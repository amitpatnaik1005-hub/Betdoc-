export const EdgeBar = ({ modelWinChance, impliedProb }: { modelWinChance: number, impliedProb: number }) => {
  const edge = modelWinChance - impliedProb;
  const isPositive = edge > 0;
  
  // Calculate width for visual presentation (clamped between 0 and 100)
  const widthPercent = Math.min(100, Math.max(0, edge * 100));

  return (
    <div className="mt-1.5 w-full">
      <div className="flex justify-between text-[9px] font-medium mb-1 uppercase tracking-wider text-slate-500 dark:text-[#A6A39E]">
        <span>Implied: {(impliedProb * 100).toFixed(1)}%</span>
        <span className={isPositive ? "text-[#C89B3C] dark:text-[#E0B85A] font-semibold" : ""}>
          Model: {(modelWinChance * 100).toFixed(1)}%
        </span>
      </div>
      <div className="w-full bg-slate-100 dark:bg-white/10 rounded-full h-1 overflow-hidden flex shadow-inner">
        <div 
          className={`h-full transition-all duration-500 ${isPositive ? "bg-[#C89B3C] shadow-[0_0_8px_rgba(200,155,60,0.5)]" : "bg-slate-300 dark:bg-slate-600"}`} 
          style={{ width: `${isPositive ? Math.max(5, widthPercent * 4) : 0}%` }}
        />
      </div>
    </div>
  );
};
