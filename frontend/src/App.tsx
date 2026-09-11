import { useBetStore } from './store/useBetStore';
import { OddsGrid } from './components/board/OddsGrid';
import { ScoutDrawer } from './components/oracle/ScoutDrawer';
import { IdempotentBetslip } from './components/betslip/IdempotentBetslip';

function App() {
  const bankroll = useBetStore(state => state.bankroll);

  return (
    <div className="min-h-screen bg-slate-100 font-sans text-foreground selection:bg-primary selection:text-white flex overflow-hidden">
      
      {/* Left Sidebar */}
      <aside className="w-64 border-r-4 border-onyx bg-white shadow-glass z-10 flex flex-col hidden md:flex">
        <div className="h-16 flex items-center px-6 border-b-4 border-onyx bg-accent">
          <div className="text-2xl font-display font-black tracking-tight text-onyx flex items-center gap-2 uppercase">
            <div className="w-4 h-4 bg-primary border-2 border-onyx shadow-solid"></div>
            BetDoc
          </div>
        </div>
        <nav className="flex-1 p-6 space-y-4">
          <a href="#" className="flex items-center gap-3 px-4 py-3 rounded-lg bg-onyx text-white font-bold font-display uppercase tracking-wider shadow-[4px_4px_0_rgba(230,57,70,1)] border-2 border-transparent transition-all">
            <span className="material-symbols-outlined text-xl">dashboard</span>
            The Board
          </a>
          <a href="#" className="flex items-center gap-3 px-4 py-3 rounded-lg text-onyx font-bold border-2 border-transparent hover:border-onyx hover:shadow-[4px_4px_0_rgba(0,0,0,1)] hover:-translate-y-px transition-all">
            <span className="material-symbols-outlined text-xl">leaderboard</span>
            Top Models
          </a>
          <a href="#" className="flex items-center gap-3 px-4 py-3 rounded-lg text-onyx font-bold border-2 border-transparent hover:border-onyx hover:shadow-[4px_4px_0_rgba(0,0,0,1)] hover:-translate-y-px transition-all">
            <span className="material-symbols-outlined text-xl">history</span>
            My Bets
          </a>
        </nav>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col min-w-0 relative h-screen">
        {/* Top Header */}
        <header className="h-16 border-b-4 border-onyx bg-white sticky top-0 z-20 flex items-center justify-between px-6 shadow-sm">
          <div className="flex items-center gap-4">
            <div className="hidden md:flex bg-slate-100 rounded-lg p-1 border-2 border-onyx shadow-[2px_2px_0_rgba(0,0,0,1)]">
              <button className="px-4 py-1.5 rounded bg-onyx text-white text-sm font-bold uppercase tracking-wider">System AI</button>
              <button className="px-4 py-1.5 rounded text-onyx hover:bg-slate-200 text-sm font-bold uppercase tracking-wider transition-colors">Custom</button>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-3 bg-white px-4 py-2 rounded-lg border-2 border-onyx shadow-solid">
              <span className="text-xs text-onyx font-black uppercase tracking-widest">Bankroll</span>
              <span className="font-mono font-black text-primary text-lg">${(bankroll || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
            </div>
          </div>
        </header>

        {/* Scrollable Stage */}
        <div className="flex-1 overflow-auto p-6 lg:p-8 flex gap-8">
          <div className="flex-1 flex flex-col max-w-5xl">
            {/* Page Header */}
            <div className="mb-6 flex justify-between items-end">
              <div>
                <h1 className="text-4xl font-display font-black text-onyx uppercase tracking-tighter">Live Board</h1>
                <p className="text-onyx/70 font-mono text-sm mt-1 font-bold">High-confidence edge plays verified by the Session Oracle.</p>
              </div>
            </div>

            {/* The TanStack Virtual Grid */}
            <OddsGrid />
          </div>

          {/* Betslip Column */}
          <div className="w-80 hidden lg:block pt-16">
            <IdempotentBetslip />
          </div>
        </div>
      </main>

      {/* Right Sidebar (Scout Oracle) */}
      <aside className="w-[350px] z-10 flex flex-col">
        <ScoutDrawer />
      </aside>
    </div>
  );
}

export default App;
