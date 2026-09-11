export const EdgeBar = ({ modelWinChance, impliedProb }: { modelWinChance: number, impliedProb: number }) => {
  const edge = modelWinChance - impliedProb;
  const isPositive = edge > 0;
  
  // Calculate width for visual presentation (clamped between 0 and 100)
  const widthPercent = Math.min(100, Math.max(0, edge * 100));

  return (
    <div className="mt-2 w-full">
      <div className="flex justify-between text-[11px] font-mono mb-1 uppercase tracking-wider">
        <span className="text-slate-500">Implied: {(impliedProb * 100).toFixed(1)}%</span>
        <span className={isPositive ? "text-primary font-bold" : "text-slate-500"}>
          Model: {(modelWinChance * 100).toFixed(1)}%
        </span>
      </div>
      <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden flex shadow-inner">
        <div 
          className={`h-full transition-all duration-500 ${isPositive ? "bg-primary shadow-[0_0_8px_rgba(230,57,70,0.5)]" : "bg-slate-300"}`} 
          style={{ width: `${isPositive ? Math.max(5, widthPercent * 4) : 0}%` }}
        />
      </div>
    </div>
  );
};
