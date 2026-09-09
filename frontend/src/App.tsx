import React from 'react';

function App() {
  return (
    <div className="min-h-screen bg-background font-sans text-foreground selection:bg-primary selection:text-white flex overflow-hidden">
      
      {/* Left Sidebar */}
      <aside className="w-64 border-r border-slate-200 bg-white shadow-glass z-10 flex flex-col hidden md:flex">
        <div className="h-16 flex items-center px-6 border-b border-slate-200">
          <div className="text-xl font-display font-bold tracking-tight text-onyx flex items-center gap-2">
            <div className="w-4 h-4 bg-primary rounded-full"></div>
            BetDoc
          </div>
        </div>
        <nav className="flex-1 p-4 space-y-2">
          <a href="#" className="flex items-center gap-3 px-3 py-2 rounded-lg bg-slate-50 text-primary font-medium">
            <span className="material-symbols-outlined text-lg">dashboard</span>
            The Board
          </a>
          <a href="#" className="flex items-center gap-3 px-3 py-2 rounded-lg text-muted hover:bg-slate-50 hover:text-foreground transition-colors">
            <span className="material-symbols-outlined text-lg">leaderboard</span>
            Top Models
          </a>
          <a href="#" className="flex items-center gap-3 px-3 py-2 rounded-lg text-muted hover:bg-slate-50 hover:text-foreground transition-colors">
            <span className="material-symbols-outlined text-lg">history</span>
            My Bets
          </a>
          <a href="#" className="flex items-center gap-3 px-3 py-2 rounded-lg text-muted hover:bg-slate-50 hover:text-foreground transition-colors">
            <span className="material-symbols-outlined text-lg">settings</span>
            Settings
          </a>
        </nav>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col min-w-0 relative">
        {/* Top Header */}
        <header className="h-16 border-b border-slate-200 bg-white/80 backdrop-blur-md sticky top-0 z-20 flex items-center justify-between px-6">
          <div className="flex items-center gap-4">
            <div className="hidden md:flex bg-slate-100 rounded-full p-1 border border-slate-200">
              <button className="px-4 py-1.5 rounded-full bg-white shadow-sm text-sm font-medium">System AI</button>
              <button className="px-4 py-1.5 rounded-full text-muted hover:text-foreground text-sm font-medium transition-colors">My Custom Model</button>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 bg-slate-50 px-3 py-1.5 rounded-lg border border-slate-200">
              <span className="text-xs text-muted font-medium">BANKROLL</span>
              <span className="font-mono font-bold">$10,420.00</span>
            </div>
          </div>
        </header>

        {/* Scrollable Stage */}
        <div className="flex-1 overflow-auto p-6 lg:p-8">
          <div className="max-w-6xl mx-auto space-y-6">
            
            {/* Page Header */}
            <div>
              <h1 className="text-3xl font-display font-bold text-foreground">Live Board</h1>
              <p className="text-muted mt-1">High-confidence edge plays verified by the Session Oracle.</p>
            </div>

            {/* Odds Table Scaffold */}
            <div className="bg-white rounded-xl shadow-glass border border-slate-200 overflow-hidden">
              <div className="p-4 border-b border-slate-200 bg-slate-50/50 flex items-center justify-between">
                <h2 className="font-medium">NFL Week 7 • Moneyline</h2>
                <button className="text-sm text-primary font-medium hover:underline">Compare Lines</button>
              </div>
              <div className="p-12 flex flex-col items-center justify-center text-center">
                <div className="w-16 h-16 bg-slate-100 rounded-2xl border-2 border-dashed border-slate-300 mb-4 flex items-center justify-center">
                  <span className="material-symbols-outlined text-muted text-3xl">table_chart</span>
                </div>
                <h3 className="text-lg font-medium text-foreground">Astra Data Grid Pending</h3>
                <p className="text-sm text-muted mt-2 max-w-md">
                  This is where the high-frequency TanStack Virtualized table will render 
                  the odds data without freezing the DOM.
                </p>
                <div className="mt-6 px-6 py-2 bg-primary text-white font-medium rounded-lg shadow-solid cursor-not-allowed opacity-80">
                  Awaiting Astra Logic...
                </div>
              </div>
            </div>

          </div>
        </div>
      </main>

      {/* Right Sidebar (Scout Oracle) */}
      <aside className="w-80 border-l border-slate-200 bg-white shadow-[-10px_0_30px_rgba(0,0,0,0.02)] z-10 flex flex-col">
        <div className="p-6 border-b border-slate-200 bg-accent/5">
          <div className="flex items-center justify-between">
            <h2 className="font-display font-bold text-accent flex items-center gap-2">
              <span className="material-symbols-outlined">temp_preferences_custom</span>
              Scout Oracle
            </h2>
            <div className="w-2 h-2 rounded-full bg-accent animate-pulse"></div>
          </div>
          <p className="text-xs text-muted mt-2">Your AI betting analyst. Ask me why we are taking the spread.</p>
        </div>
        <div className="flex-1 p-4 overflow-auto">
          {/* Empty Chat State */}
        </div>
        <div className="p-4 border-t border-slate-200 bg-slate-50">
          <div className="relative">
            <input 
              type="text" 
              placeholder="Ask Scout..." 
              className="w-full bg-white border border-slate-200 rounded-lg pl-4 pr-10 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent transition-all"
            />
            <button className="absolute right-2 top-2 w-7 h-7 bg-accent text-white rounded-md flex items-center justify-center hover:bg-accent/90 transition-colors">
              <span className="material-symbols-outlined text-[16px]">arrow_upward</span>
            </button>
          </div>
        </div>
      </aside>
    </div>
  );
}

export default App;
