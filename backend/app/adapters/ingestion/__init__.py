"""Registry of fleet ingestors. The key is the data source id used for locks, config rows and the UI."""

from app.adapters.ingestion.base import BaseDataIngestor
from app.adapters.ingestion.odds_api_adapter import OddsApiIngestor
from app.adapters.ingestion.oddspapi_adapter import OddsPapiIngestor
from app.adapters.ingestion.polymarket_api_adapter import PolymarketIngestor

INGESTORS: dict[str, type[BaseDataIngestor]] = {
    PolymarketIngestor.source_id: PolymarketIngestor,
    OddsApiIngestor.source_id: OddsApiIngestor,
    OddsPapiIngestor.source_id: OddsPapiIngestor,  # Group 128: The Odds API's failover
}

__all__ = ["INGESTORS", "BaseDataIngestor", "OddsApiIngestor", "OddsPapiIngestor", "PolymarketIngestor"]
