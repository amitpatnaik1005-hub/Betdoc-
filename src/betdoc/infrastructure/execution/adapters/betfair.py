
"""Betfair Exchange adapter over the JSON-RPC API.



Credentials are pulled from :class:`ConfigProvider` at authentication time and

never cached on the instance beyond the resulting session token. Transport

faults are normalised into :class:`ExecutionError` so callers, including the

end-of-day auditor, can degrade rather than crash.

"""



from __future__ import annotations



import asyncio

import itertools

import logging

from decimal import Decimal, InvalidOperation

from typing import Any, Final, Mapping



import aiohttp



from betdoc.infrastructure.config.provider import ConfigError, ConfigProvider

from betdoc.infrastructure.execution.client import (

    ExecutionClient,

    ExecutionError,

    ExecutionResult,

    ExecutionStatus,

)



__all__ = ["BetfairClient"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_IDENTITY_URL: Final[str] = "https://identitysso.betfair.com/api/login"

DEFAULT_BETTING_URL: Final[str] = "https://api.betfair.com/exchange/betting/json-rpc/v1"

DEFAULT_ACCOUNT_URL: Final[str] = "https://api.betfair.com/exchange/account/json-rpc/v1"

DEFAULT_SERVICE_NAME: Final[str] = "betfair"

DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0

MIN_DECIMAL_ODDS: Final[Decimal] = Decimal("1.01")

MAX_DECIMAL_ODDS: Final[Decimal] = Decimal("1000")



# Betfair orderStatus / instruction errors mapped to our taxonomy.

_ORDER_STATUS_MAP: Final[dict[str, ExecutionStatus]] = {

    "EXECUTION_COMPLETE": ExecutionStatus.ACCEPTED,

    "EXECUTABLE": ExecutionStatus.PARTIALLY_MATCHED,

    "PENDING": ExecutionStatus.PENDING,

    "EXPIRED": ExecutionStatus.EXPIRED,

}

_REPORT_STATUS_MAP: Final[dict[str, ExecutionStatus]] = {

    "SUCCESS": ExecutionStatus.ACCEPTED,

    "FAILURE": ExecutionStatus.REJECTED,

    "PROCESSED_WITH_ERRORS": ExecutionStatus.REJECTED,

    "TIMEOUT": ExecutionStatus.PENDING,

}





class BetfairClient(ExecutionClient):

    """Concrete :class:`ExecutionClient` for the Betfair Exchange."""



    __slots__ = (

        "_account_url",

        "_app_key",

        "_betting_url",

        "_config",

        "_identity_url",

        "_lock",

        "_owns_session",

        "_request_ids",

        "_service",

        "_session",

        "_session_token",

        "_timeout",

    )



    def __init__(

        self,

        config: ConfigProvider,

        *,

        name: str = DEFAULT_SERVICE_NAME,

        service: str = DEFAULT_SERVICE_NAME,

        identity_url: str = DEFAULT_IDENTITY_URL,

        betting_url: str = DEFAULT_BETTING_URL,

        account_url: str = DEFAULT_ACCOUNT_URL,

        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,

        session: aiohttp.ClientSession | None = None,

    ) -> None:

        super().__init__(name)

        self._config: ConfigProvider = config

        self._service: str = service

        self._identity_url: str = identity_url

        self._betting_url: str = betting_url

        self._account_url: str = account_url

        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(

            total=float(timeout_seconds)

        )

        self._session: aiohttp.ClientSession | None = session

        self._owns_session: bool = session is None

        self._session_token: str | None = None

        self._app_key: str | None = None

        self._lock: asyncio.Lock = asyncio.Lock()

        self._request_ids: "itertools.count[int]" = itertools.count(1)



    @property

    def session_token(self) -> str | None:

        """Current Betfair session token, if authenticated."""

        return self._session_token



    async def authenticate(self) -> None:

        """Resolve credentials and exchange them for a session token.



        Raises

        ------

        ExecutionError

            ``CONFIG`` when credentials cannot be resolved, ``AUTH_FAILED``

            when Betfair rejects the login, ``TRANSPORT`` on a network fault.

        """

        async with self._lock:

            try:

                credentials = await self._config.get_credentials(self._service)

                credentials.require("app_key", "username", "password")

            except ConfigError as error:

                raise ExecutionError(

                    f"unable to resolve Betfair credentials: {error}",

                    code="CONFIG",

                    bookmaker=self.name,

                    details={"config_code": error.code},

                ) from error



            session = await self._ensure_session()

            headers: dict[str, str] = {

                "X-Application": credentials.app_key,

                "Content-Type": "application/x-www-form-urlencoded",

                "Accept": "application/json",

            }

            form: dict[str, str] = {

                "username": credentials.username,

                "password": credentials.password,

            }



            try:

                async with session.post(

                    self._identity_url,

                    data=form,

                    headers=headers,

                    timeout=self._timeout,

                ) as response:

                    if response.status == 401 or response.status == 403:

                        raise ExecutionError(

                            f"Betfair rejected the login (HTTP {response.status})",

                            code="AUTH_FAILED",

                            bookmaker=self.name,

                        )

                    if response.status >= 400:

                        raise ExecutionError(

                            f"Betfair identity service returned HTTP {response.status}",

                            code="TRANSPORT",

                            bookmaker=self.name,

                        )

                    body: Any = await response.json(content_type=None)

            except asyncio.TimeoutError as error:

                raise ExecutionError(

                    "Betfair login timed out",

                    code="TIMEOUT",

                    bookmaker=self.name,

                ) from error

            except aiohttp.ClientError as error:

                raise ExecutionError(

                    f"Betfair login transport failure: {error}",

                    code="TRANSPORT",

                    bookmaker=self.name,

                ) from error



            if not isinstance(body, Mapping):

                raise ExecutionError(

                    "Betfair login returned an unexpected body",

                    code="AUTH_FAILED",

                    bookmaker=self.name,

                )



            status = str(body.get("status", "")).upper()

            token = str(body.get("token", "") or "")

            if status != "SUCCESS" or not token:

                raise ExecutionError(

                    f"Betfair login unsuccessful: {body.get('error') or status or 'unknown'}",

                    code="AUTH_FAILED",

                    bookmaker=self.name,

                    details={"status": status},

                )



            self._session_token = token

            self._app_key = credentials.app_key

            self._authenticated = True

            _LOG.info("authenticated with Betfair as service %s", self._service)



    async def get_balance(self) -> Decimal:

        """Return ``availableToBetBalance`` as a ``Decimal`` INR amount."""

        response = await self._rpc(

            self._account_url, "AccountAPING/v1.0/getAccountFunds", {}

        )

        if not isinstance(response, Mapping):

            raise ExecutionError(

                "getAccountFunds returned an unexpected payload",

                code="MALFORMED",

                bookmaker=self.name,

            )

        raw_balance = response.get("availableToBetBalance")

        if raw_balance is None:

            raise ExecutionError(

                "getAccountFunds response is missing availableToBetBalance",

                code="MALFORMED",

                bookmaker=self.name,

            )

        return self._quantize_money(self._to_decimal(raw_balance, "availableToBetBalance"))



    async def place_order(

        self,

        market_id: str,

        selection_id: int,

        odds: Decimal,

        stake: Decimal,

    ) -> ExecutionResult:

        """Place a persistent LIMIT back order and normalise the report."""

        requested_odds = self._quantize_odds(self._validate_odds(odds))

        requested_stake = self._quantize_money(self._validate_stake(stake))



        params: dict[str, Any] = {

            "marketId": market_id,

            "instructions": [

                {

                    "selectionId": int(selection_id),

                    "handicap": 0,

                    "side": "BACK",

                    "orderType": "LIMIT",

                    "limitOrder": {

                        "size": float(requested_stake),

                        "price": float(requested_odds),

                        "persistenceType": "LAPSE",

                    },

                }

            ],

        }



        report = await self._rpc(

            self._betting_url, "SportsAPING/v1.0/placeOrders", params

        )

        return self._map_place_report(

            report,

            market_id=market_id,

            selection_id=int(selection_id),

            requested_odds=requested_odds,

            requested_stake=requested_stake,

        )



    async def cancel_order(self, order_id: str) -> ExecutionResult:

        """Fully cancel a resting order.



        Betfair requires the market id alongside the bet id, so the order is

        located through ``listCurrentOrders`` before cancellation.

        """

        if not order_id:

            raise ExecutionError(

                "order_id must not be empty", code="MALFORMED", bookmaker=self.name

            )



        market_id = await self._market_for_order(order_id)

        report = await self._rpc(

            self._betting_url,

            "SportsAPING/v1.0/cancelOrders",

            {"marketId": market_id, "instructions": [{"betId": order_id}]},

        )



        if not isinstance(report, Mapping):

            raise ExecutionError(

                "cancelOrders returned an unexpected payload",

                code="MALFORMED",

                bookmaker=self.name,

            )



        status = str(report.get("status", "")).upper()

        instructions = report.get("instructionReports") or []

        instruction: Mapping[str, Any] = (

            instructions[0] if instructions and isinstance(instructions[0], Mapping) else {}

        )

        instruction_status = str(instruction.get("status", "")).upper()

        cancelled = self._to_decimal(instruction.get("sizeCancelled", 0), "sizeCancelled")

        succeeded = status == "SUCCESS" and instruction_status == "SUCCESS"



        return ExecutionResult(

            success=succeeded,

            status=ExecutionStatus.CANCELLED if succeeded else ExecutionStatus.REJECTED,

            bookmaker=self.name,

            order_id=order_id,

            bet_id=order_id,

            market_id=market_id,

            remaining_stake=self._quantize_money(cancelled),

            code=str(

                instruction.get("errorCode") or report.get("errorCode") or status or "OK"

            ),

            message=None if succeeded else "Betfair declined the cancellation",

            raw=dict(report),

        )



    async def cashout(self, bet_id: str) -> ExecutionResult:

        """Unsupported on the Betfair JSON-RPC API.



        Cash-out is a website/Exchange-app feature with no JSON-RPC equivalent.

        Exposing a partial emulation here would let strategy code assume a

        guaranteed exit that does not exist, so this always raises.



        Raises

        ------

        ExecutionError

            Always, with code ``UNSUPPORTED``.

        """

        raise ExecutionError(

            "Betfair does not expose cashout over the JSON-RPC API; "

            "close the position by placing an offsetting LAY order instead",

            code="UNSUPPORTED",

            bookmaker=self.name,

            details={"bet_id": bet_id},

        )



    async def close(self) -> None:

        """Discard the session token and close an owned HTTP session."""

        self._session_token = None

        self._app_key = None

        self._authenticated = False

        if self._owns_session and self._session is not None and not self._session.closed:

            await self._session.close()

        self._session = None



    async def _ensure_session(self) -> aiohttp.ClientSession:

        if self._session is None or self._session.closed:

            self._session = aiohttp.ClientSession(timeout=self._timeout)

            self._owns_session = True

        return self._session



    def _auth_headers(self) -> dict[str, str]:

        if not self._session_token or not self._app_key:

            raise ExecutionError(

                "client is not authenticated; call authenticate() first",

                code="NOT_AUTHENTICATED",

                bookmaker=self.name,

            )

        return {

            "X-Application": self._app_key,

            "X-Authentication": self._session_token,

            "Content-Type": "application/json",

            "Accept": "application/json",

        }



    async def _rpc(self, url: str, method: str, params: Mapping[str, Any]) -> Any:

        """Issue a single JSON-RPC call and return the unwrapped ``result``.



        Raises

        ------

        ExecutionError

            ``NOT_AUTHENTICATED``, ``SESSION_EXPIRED``, ``TIMEOUT``,

            ``TRANSPORT``, ``MALFORMED``, or ``API_ERROR``.

        """

        headers = self._auth_headers()

        session = await self._ensure_session()

        payload: dict[str, Any] = {

            "jsonrpc": "2.0",

            "method": method,

            "params": dict(params),

            "id": next(self._request_ids),

        }



        try:

            async with session.post(

                url, json=payload, headers=headers, timeout=self._timeout

            ) as response:

                if response.status in (401, 403):

                    self._authenticated = False

                    raise ExecutionError(

                        f"Betfair session rejected on {method} (HTTP {response.status})",

                        code="SESSION_EXPIRED",

                        bookmaker=self.name,

                    )

                if response.status >= 400:

                    raise ExecutionError(

                        f"Betfair returned HTTP {response.status} for {method}",

                        code="TRANSPORT",

                        bookmaker=self.name,

                        details={"http_status": response.status},

                    )

                body: Any = await response.json(content_type=None)

        except asyncio.TimeoutError as error:

            raise ExecutionError(

                f"Betfair call {method} timed out",

                code="TIMEOUT",

                bookmaker=self.name,

            ) from error

        except aiohttp.ClientError as error:

            raise ExecutionError(

                f"Betfair transport failure on {method}: {error}",

                code="TRANSPORT",

                bookmaker=self.name,

            ) from error



        if not isinstance(body, Mapping):

            raise ExecutionError(

                f"Betfair returned a non-object JSON-RPC response for {method}",

                code="MALFORMED",

                bookmaker=self.name,

            )



        error_body = body.get("error")

        if error_body is not None:

            code, message = self._describe_rpc_error(error_body)

            raise ExecutionError(

                f"Betfair API error on {method}: {message}",

                code="API_ERROR",

                bookmaker=self.name,

                details={"api_code": code, "method": method},

            )



        if "result" not in body:

            raise ExecutionError(

                f"Betfair JSON-RPC response for {method} has no result",

                code="MALFORMED",

                bookmaker=self.name,

            )

        return body["result"]



    @staticmethod

    def _describe_rpc_error(error_body: Any) -> tuple[str, str]:

        """Extract ``(code, message)`` from a JSON-RPC error envelope."""

        if not isinstance(error_body, Mapping):

            return "UNKNOWN", str(error_body)

        data = error_body.get("data")

        if isinstance(data, Mapping):

            exception = data.get("APINGException")

            if isinstance(exception, Mapping):

                code = str(exception.get("errorCode", "UNKNOWN"))

                return code, str(exception.get("errorDetails") or code)

        return str(error_body.get("code", "UNKNOWN")), str(

            error_body.get("message", "unspecified API error")

        )



    async def _market_for_order(self, order_id: str) -> str:

        """Resolve the market id that owns ``order_id``."""

        result = await self._rpc(

            self._betting_url,

            "SportsAPING/v1.0/listCurrentOrders",

            {"betIds": [order_id], "orderProjection": "ALL"},

        )

        orders = result.get("currentOrders") if isinstance(result, Mapping) else None

        if not orders:

            raise ExecutionError(

                f"no open Betfair order found with id {order_id}",

                code="NOT_FOUND",

                bookmaker=self.name,

            )

        first = orders[0]

        market_id = first.get("marketId") if isinstance(first, Mapping) else None

        if not market_id:

            raise ExecutionError(

                f"Betfair order {order_id} has no marketId",

                code="MALFORMED",

                bookmaker=self.name,

            )

        return str(market_id)



    def _map_place_report(

        self,

        report: Any,

        *,

        market_id: str,

        selection_id: int,

        requested_odds: Decimal,

        requested_stake: Decimal,

    ) -> ExecutionResult:

        """Translate a Betfair ``PlaceExecutionReport`` into our result type."""

        if not isinstance(report, Mapping):

            raise ExecutionError(

                "placeOrders returned an unexpected payload",

                code="MALFORMED",

                bookmaker=self.name,

            )



        report_status = str(report.get("status", "")).upper()

        instructions = report.get("instructionReports") or []

        instruction: Mapping[str, Any] = (

            instructions[0] if instructions and isinstance(instructions[0], Mapping) else {}

        )



        order_status = str(instruction.get("orderStatus", "")).upper()

        instruction_status = str(instruction.get("status", "")).upper()



        status: ExecutionStatus = _ORDER_STATUS_MAP.get(

            order_status,

            _REPORT_STATUS_MAP.get(report_status, ExecutionStatus.FAILED),

        )

        if instruction_status == "FAILURE":

            status = ExecutionStatus.REJECTED



        matched_stake = self._quantize_money(

            self._to_decimal(instruction.get("sizeMatched", 0), "sizeMatched")

        )

        average_price = self._to_decimal(

            instruction.get("averagePriceMatched", 0), "averagePriceMatched"

        )

        matched_odds = (

            self._quantize_odds(average_price) if average_price > 0 else Decimal("0.00")

        )

        remaining = requested_stake - matched_stake

        if remaining < 0:

            remaining = Decimal("0.00")

        if status is ExecutionStatus.PARTIALLY_MATCHED and remaining == 0:

            status = ExecutionStatus.ACCEPTED



        error_code = str(

            instruction.get("errorCode") or report.get("errorCode") or ""

        ) or None

        success = report_status == "SUCCESS" and status.is_successful



        bet_id = instruction.get("betId")

        return ExecutionResult(

            success=success,

            status=status,

            bookmaker=self.name,

            order_id=str(bet_id) if bet_id else None,

            bet_id=str(bet_id) if bet_id else None,

            market_id=market_id,

            selection_id=selection_id,

            requested_odds=requested_odds,

            matched_odds=matched_odds,

            requested_stake=requested_stake,

            matched_stake=matched_stake,

            remaining_stake=self._quantize_money(remaining),

            code=error_code or report_status or None,

            message=None if success else f"Betfair status {report_status}/{order_status}",

            raw=dict(report),

        )



    def _validate_odds(self, odds: Decimal) -> Decimal:

        value = self._to_decimal(odds, "odds")

        if value < MIN_DECIMAL_ODDS or value > MAX_DECIMAL_ODDS:

            raise ExecutionError(

                f"decimal odds {value} outside Betfair range "

                f"[{MIN_DECIMAL_ODDS}, {MAX_DECIMAL_ODDS}]",

                code="MALFORMED",

                bookmaker=self.name,

            )

        return value



    def _validate_stake(self, stake: Decimal) -> Decimal:

        value = self._to_decimal(stake, "stake")

        if value <= 0:

            raise ExecutionError(

                f"stake must be positive, got {value}",

                code="MALFORMED",

                bookmaker=self.name,

            )

        return value



    def _to_decimal(self, value: Any, field_name: str) -> Decimal:

        """Coerce an API value to ``Decimal`` without binary float drift."""

        if isinstance(value, Decimal):

            return value

        try:

            if isinstance(value, float):

                return Decimal(repr(value))

            return Decimal(str(value))

        except (InvalidOperation, TypeError, ValueError) as error:

            raise ExecutionError(

                f"Betfair returned a non-numeric {field_name}: {value!r}",

                code="MALFORMED",

                bookmaker=self.name,

            ) from error

