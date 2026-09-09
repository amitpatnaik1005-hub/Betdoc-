import React, { useState, useRef, useEffect } from "react";
import { useBetStore } from "../../store/useBetStore";

export const ScoutDrawer = () => {
  const { oracleHistory, addOracleMessage, scoutContextMarketId, isOracleLoading, setOracleLoading } = useBetStore();
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [oracleHistory, isOracleLoading]);

  const handleSend = async () => {
    if (!input.trim()) return;

    // Add user message to Zustand
    addOracleMessage({
      id: crypto.randomUUID(),
      role: 'user',
      content: input,
      context_market_id: scoutContextMarketId
    });

    setInput("");
    setOracleLoading(true);

    // Mock API call to backend session_oracle.py
    setTimeout(() => {
      addOracleMessage({
        id: crypto.randomUUID(),
        role: 'scout',
        content: scoutContextMarketId 
          ? `Analysis for market ${scoutContextMarketId}: The edge here is driven by a significant discrepancy in our proprietary volatility engine vs the sportsbook's implied projection. I recommend sizing this at 1.5U.`
          : `I am the Scout Oracle. Click any game on the board to give me context, or ask me general betting questions.`
      });
      setOracleLoading(false);
    }, 1500);
  };

  return (
    <div className="flex flex-col h-full bg-white border-l-4 border-onyx shadow-[-8px_0_0_rgba(0,0,0,1)]">
      {/* Header */}
      <div className="p-6 border-b-4 border-onyx bg-accent">
        <h2 className="font-display font-black text-2xl text-onyx uppercase tracking-tighter">
          Scout Oracle
        </h2>
        <p className="font-mono text-sm font-bold mt-1 text-onyx/70">
          AI Betting Assistant
        </p>
        {scoutContextMarketId && (
          <div className="mt-3 px-3 py-1.5 bg-onyx text-white font-mono text-xs font-bold rounded flex justify-between items-center shadow-solid">
            <span>Context: {scoutContextMarketId}</span>
            <button className="text-white/50 hover:text-white" onClick={() => useBetStore.getState().setScoutContext(undefined)}>✕</button>
          </div>
        )}
      </div>

      {/* Chat History */}
      <div className="flex-1 overflow-y-auto p-6 space-y-4 bg-slate-50">
        {oracleHistory.length === 0 && (
          <div className="text-center text-slate-400 font-mono text-sm mt-10">
            No messages yet. Ask Scout a question.
          </div>
        )}
        
        {oracleHistory.map((msg) => (
          <div key={msg.id} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`max-w-[85%] p-4 border-2 border-onyx shadow-solid rounded-xl ${msg.role === 'user' ? 'bg-primary text-white rounded-br-none' : 'bg-white text-onyx rounded-bl-none'}`}>
              <p className={`font-sans text-sm ${msg.role === 'user' ? 'font-medium' : 'font-bold'}`}>
                {msg.content}
              </p>
            </div>
          </div>
        ))}
        
        {isOracleLoading && (
          <div className="flex justify-start">
            <div className="max-w-[85%] p-4 border-2 border-onyx shadow-solid rounded-xl bg-white text-onyx rounded-bl-none">
              <div className="flex gap-1 items-center h-5">
                <div className="w-2 h-2 rounded-full bg-accent animate-bounce" style={{ animationDelay: '0ms' }} />
                <div className="w-2 h-2 rounded-full bg-accent animate-bounce" style={{ animationDelay: '150ms' }} />
                <div className="w-2 h-2 rounded-full bg-accent animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Box */}
      <div className="p-4 border-t-4 border-onyx bg-white">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSend()}
            placeholder="Ask the Oracle..."
            className="flex-1 px-4 py-3 border-2 border-onyx rounded-lg font-mono text-sm focus:outline-none focus:ring-4 focus:ring-accent/50 transition-all"
          />
          <button 
            onClick={handleSend}
            disabled={isOracleLoading || !input.trim()}
            className="px-6 py-3 bg-onyx text-white font-bold font-display uppercase tracking-wider rounded-lg shadow-solid hover:translate-y-px active:shadow-none transition-all disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
};
