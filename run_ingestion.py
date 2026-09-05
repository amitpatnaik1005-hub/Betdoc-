import asyncio
import logging
import os
import sys

# Add the src directory to the Python path so it can find 'betdoc'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from dotenv import load_dotenv
from betdoc.adapters.bookmakers.the_odds_api import TheOddsApiAdapter

# Configure logging so we can see the engine working under the hood
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

async def main():
    # Load environment variables from the .env file
    load_dotenv()
    
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("❌ ERROR: Ignition failed.")
        print("Please paste your real API key into the .env file.")
        print("Get a free key here: https://the-odds-api.com/")
        return

    from betdoc.adapters.bookmakers.failover_router import FailoverRouter
    from betdoc.adapters.bookmakers.mock_adapter import MockAdapter

    print("🚀 IGNITING UNIVERSAL DATA INGESTION ENGINE (FAILOVER MODE)...")
    
    # 1. Primary API (The Odds API)
    primary_api = TheOddsApiAdapter(
        api_key=api_key,
        sports=["soccer_epl"], 
        markets=["h2h", "spreads", "totals"],
        poll_interval=120.0
    )

    # 2. Infinite Free Mock API (Fallback 1)
    mock_api_1 = MockAdapter(bookmaker_name="mock_bookie_A", poll_interval=2.0)
    
    # 3. Infinite Free Mock API (Fallback 2)
    mock_api_2 = MockAdapter(bookmaker_name="mock_bookie_B", poll_interval=2.0)

    # Wire them into the Switch (Router)
    # The Router will try primary_api. If it runs out of quota, it switches to mock 1, then mock 2.
    router = FailoverRouter(adapters=[primary_api, mock_api_1, mock_api_2])
    
    try:
        # The engine asks the ROUTER for data, the router manages the APIs automatically
        async for tick in router.stream_live_ticks():
            print(f"✅ [LIVE TICK] {tick.bookmaker.upper()} | {tick.home_team} vs {tick.away_team}")
            print(f"   -> Event ID: {tick.event_id}")
            print(f"   -> Markets Captured: {len(tick.markets)}")
            print("-" * 60)
            
    except KeyboardInterrupt:
        print("\n🛑 Engine shut down gracefully by user.")
    except Exception as e:
        print(f"\n❌ FATAL ENGINE ERROR: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Engine shut down gracefully by user.")
