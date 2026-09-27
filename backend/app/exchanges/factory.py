from app.exchanges.base import BaseExchangeAdapter
from app.exchanges.mock import MockExchangeAdapter

_ADAPTERS: dict[str, type[BaseExchangeAdapter]] = {
    # "betfair": BetfairExchangeAdapter,
    # "smarkets": SmarketsExchangeAdapter,
}


def get_exchange_adapter(
    exchange_name: str,
    api_key: str,
    api_secret: str,
) -> BaseExchangeAdapter:
    adapter_cls = _ADAPTERS.get((exchange_name or "").strip().lower(), MockExchangeAdapter)
    return adapter_cls(api_key, api_secret)
