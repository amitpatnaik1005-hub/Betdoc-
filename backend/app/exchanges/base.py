from abc import ABC, abstractmethod
from decimal import Decimal


class ExchangeRejectionError(ValueError):
    """The exchange definitively refused the bet. Safe to release its exposure.

    This is a dedicated subclass so that a stray ValueError from a bug in an adapter
    is not treated as a definitive rejection, which would free capital for a bet that
    may actually be live.
    """


class BaseExchangeAdapter(ABC):
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    def __repr__(self) -> str:
        # Never leak credentials into logs or tracebacks.
        return f"{self.__class__.__name__}(api_key='***', api_secret='***')"

    @abstractmethod
    async def place_bet(
        self,
        match_id: str,
        selection: str,
        odds: Decimal,
        stake: Decimal,
    ) -> str:
        """Place a bet and return the exchange_bet_id.

        Raises:
            ExchangeRejectionError: the exchange definitively refused the bet.
            Exception: any other failure. The outcome is unknown.
        """
        raise NotImplementedError
