"""BTC-quoted alt Spot REST boundary (isolated from the deployed BTCUSDT gateway). No credentials are logged or represented.

Only this module's fixed endpoints are supported; this is not a general client.
POST is attempted once. A client order ID is NOT an idempotency guarantee after
an order fills, so an uncertain result must be reconciled, never blindly resent.
Market data includes the still-open UTC daily candle; callers exclude it.

Official semantics checked 2026-09-24:
https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade
https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account
https://developers.binance.com/en/docs/products/spot/rest-api
https://developers.binance.com/en/docs/products/spot/errors
https://developers.binance.com/en/docs/products/spot/filters
https://developers.binance.com/en/docs/products/spot/faqs/commission_faq
https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
import time
from typing import Mapping
from urllib.parse import urlencode, urlsplit


BASE_URL = "https://api.binance.com"
ALLOWED = {"ETHBTC": ("ETH", "BTC"), "BNBBTC": ("BNB", "BTC"), "SOLBTC": ("SOL", "BTC"), "XRPBTC": ("XRP", "BTC")}
MAX_RESPONSE_BYTES = 4_000_000
GET_ATTEMPTS = 3
TRADE_PAGE_SIZE = 1000
MAX_TRADE_PAGES = 100
_CLIENT_ID = re.compile(r"bsg_[A-Za-z0-9_-]{1,32}\Z")
_PUBLIC = {
    "/api/v3/time": frozenset(),
    "/api/v3/exchangeInfo": frozenset({"symbol"}),
    "/api/v3/ticker/bookTicker": frozenset({"symbol"}),
    "/api/v3/referencePrice": frozenset({"symbol"}),
    "/api/v3/avgPrice": frozenset({"symbol"}),
    "/api/v3/ticker/price": frozenset({"symbol"}),
    "/api/v3/klines": frozenset({"symbol", "interval", "limit"}),
}
_SIGNED = {
    "/api/v3/account": frozenset(),
    "/api/v3/account/commission": frozenset({"symbol"}),
    "/api/v3/myFilters": frozenset({"symbol"}),
    "/sapi/v1/account/apiRestrictions": frozenset(),
    "/sapi/v1/bnbBurn": frozenset(),
    "/api/v3/openOrders": frozenset({"symbol"}),
    "/api/v3/order": frozenset({"symbol", "origClientOrderId"}),
    "/api/v3/myTrades": frozenset({"symbol", "orderId", "fromId", "limit"}),
}
_ORDER_KEYS = frozenset({"symbol", "side", "type", "quantity", "newClientOrderId", "newOrderRespType"})
_LIMIT_ORDER_KEYS = _ORDER_KEYS | {"price", "timeInForce"}


class GatewayError(RuntimeError):
    """Sanitized error: no remote message, URL query, credentials or body."""
    def __init__(self, message, *, code=None, status=None, maybe_sent=False, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.maybe_sent = maybe_sent
        self.retry_after = retry_after


class OrderOutcomeUnknown(GatewayError):
    def __init__(self, message="Spot order outcome unknown; reconcile client ID", **kwargs):
        kwargs["maybe_sent"] = True
        super().__init__(message, **kwargs)


class OrderSubmissionStopped(GatewayError):
    """The local stop gate rejected a POST before transport was attempted."""
    definitely_not_submitted = True


@dataclass(repr=False)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    text: str


class AiohttpTransport:
    """Injectable transport contract: request(method,url,headers,timeout,body=None).

    Returns HttpResponse. Implementations must never follow redirects. The default
    disables environment proxies and cookies and verifies TLS normally.
    """
    def __init__(self):
        self._session = None

    async def request(self, method, url, headers, timeout, body=None):
        import aiohttp
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.binance.com" or parsed.fragment:
            raise GatewayError("Transport host rejected")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with self._session.request(method, url, headers=headers, data=body,
                    timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=False) as response:
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise GatewayError("Spot response exceeded size limit")
                    chunks.append(chunk)
                return HttpResponse(response.status, dict(response.headers), b"".join(chunks).decode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise GatewayError("Spot transport failed", maybe_sent=method != "GET") from None

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()


def _decimal(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise GatewayError("Invalid decimal value")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise GatewayError("Invalid decimal value") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise GatewayError("Invalid decimal value")
    return result


def _integer(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise GatewayError("Invalid integer value")
    return value


def _client_id(value):
    if not isinstance(value, str) or not _CLIENT_ID.fullmatch(value):
        raise GatewayError("Client order ID must use the owned bsg_ prefix")
    return value


def _quantity(value):
    number = _decimal(value, positive=True)
    # Bound formatting work; actual lot/minimum filters remain the caller's gate.
    if abs(number.as_tuple().exponent) > 18 or len(number.as_tuple().digits) > 32:
        raise GatewayError("Invalid quantity precision")
    return format(number, "f")


def _object(value):
    if not isinstance(value, dict):
        raise GatewayError("Malformed Spot object response")
    return value


class SpotGateway:
    def __init__(self, symbol, api_key="", api_secret="", allow_orders=False, transport=None, *, intraday=False):
        if symbol not in ALLOWED:
            raise ValueError("Only configured BTC-quoted alt pairs are supported")
        self.symbol = symbol
        self.base_asset, self.quote_asset = ALLOWED[symbol]
        if not isinstance(api_key, str) or not isinstance(api_secret, str) or not isinstance(allow_orders, bool):
            raise ValueError("Invalid Spot gateway configuration")
        self._api_key, self._api_secret = api_key, api_secret
        self.allow_orders = allow_orders
        self.submission_guard = lambda: True
        self.transport = transport if transport is not None else AiohttpTransport()
        self._clock_server_ms = None
        self._clock_monotonic = 0.0
        self._clock_lock = asyncio.Lock()
        self._closed = False
        self.intraday = intraday
        self._metadata_cache = None

    def __repr__(self):
        return f"SpotGateway(symbol={self.symbol!r}, allow_orders={self.allow_orders!r})"

    @property
    def has_credentials(self):
        return bool(self._api_key or self._api_secret)

    async def close(self):
        if not self._closed:
            self._closed = True
            await self.transport.close()

    async def _sync_time(self, force=False):
        async with self._clock_lock:
            if not force and self._clock_server_ms is not None and time.monotonic() - self._clock_monotonic < 300:
                return self._timestamp()
            # A delayed GET does not give a reliable clock offset. Retry a
            # fresh sample instead of signing with the midpoint of that delay.
            for _ in range(3):
                before = time.monotonic()
                payload = _object(await self._request("GET", "/api/v3/time"))
                after = time.monotonic()
                server = _integer(payload.get("serverTime"), positive=True)
                if after - before <= 2:
                    self._clock_server_ms = server
                    self._clock_monotonic = (before + after) / 2
                    return self._timestamp()
            raise GatewayError("Spot clock synchronization latency too high")

    def _timestamp(self):
        return int(self._clock_server_ms + (time.monotonic() - self._clock_monotonic) * 1000)

    def _validate_request(self, method, path, params, signed):
        if self._closed:
            raise GatewayError("Spot gateway closed")
        if method == "POST":
            if not self.submission_guard():
                raise OrderSubmissionStopped("Spot order submission stopped")
            if (not self.allow_orders or not signed or path != "/api/v3/order"
                    or set(params) not in (_ORDER_KEYS, _LIMIT_ORDER_KEYS)):
                raise GatewayError("Spot order transmission disabled or rejected")
            _client_id(params["newClientOrderId"])
            _quantity(params["quantity"])
            if params["side"] not in ("BUY", "SELL") or params["newOrderRespType"] != "FULL":
                raise GatewayError("Unsupported Spot order")
            if set(params) == _LIMIT_ORDER_KEYS:
                if params["type"] != "LIMIT" or params["timeInForce"] != "IOC":
                    raise GatewayError("Only immediate-or-cancel limit orders are supported")
                _quantity(params["price"])
            elif params["type"] != "MARKET":
                raise GatewayError("Unsupported Spot order")
        elif method == "GET":
            endpoints = _SIGNED if signed else _PUBLIC
            account_wide_orders = signed and path == "/api/v3/openOrders" and not params
            if path not in endpoints or (set(params) != endpoints[path] and not account_wide_orders):
                raise GatewayError("Spot endpoint or parameters rejected")
        else:
            raise GatewayError("Spot method rejected")
        if "symbol" in params and params["symbol"] != self.symbol:
            raise GatewayError("Spot symbol rejected")
        if path == "/api/v3/klines":
            valid = (params["interval"] in {"5m", "1h"} and params["limit"] == 120) if self.intraday else (params["interval"] == "1d" and params["limit"] == 1000)
            if not valid:
                raise GatewayError("Unsupported candle request")
        if path == "/api/v3/myTrades":
            _integer(params["orderId"])
            _integer(params["fromId"])
            if params["limit"] != TRADE_PAGE_SIZE:
                raise GatewayError("Unsupported trade page")
        if "origClientOrderId" in params:
            _client_id(params["origClientOrderId"])
        if signed and (not self._api_key or not self._api_secret):
            raise GatewayError("Spot credentials required")

    async def _request(self, method, path, params=None, *, signed=False):
        params = dict(params or {})
        self._validate_request(method, path, params, signed)
        if signed:
            await self._sync_time()
        attempts = GET_ATTEMPTS if method == "GET" else 1
        resynchronized = False
        for attempt in range(attempts):
            headers = {}
            values = dict(params)
            if signed:
                values.update(timestamp=self._timestamp(), recvWindow=5000)
                headers["X-MBX-APIKEY"] = self._api_key
            encoded = urlencode(values)
            if signed:
                signature = hmac.new(self._api_secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
                encoded += "&signature=" + signature
            url, body = BASE_URL + path, None
            if method == "POST":
                body = encoded
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif encoded:
                url += "?" + encoded
            # Recheck the mutation guard immediately before transport invocation.
            self._validate_request(method, path, params, signed)
            try:
                response = await self.transport.request(method, url, headers, 10.0, body=body)
            except asyncio.CancelledError:
                raise
            except Exception:
                if method == "POST":
                    raise OrderOutcomeUnknown() from None
                if attempt + 1 < attempts:
                    await asyncio.sleep(.2 * (attempt + 1))
                    continue
                raise GatewayError("Spot GET transport failed") from None
            status = response.status
            if 300 <= status < 400:
                error = OrderOutcomeUnknown if method == "POST" else GatewayError
                raise error("Spot redirect rejected", status=status)
            try:
                if len(response.text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise ValueError("size")
                payload = json.loads(response.text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            except (ValueError, TypeError, UnicodeError):
                if method == "POST":
                    raise OrderOutcomeUnknown(status=status) from None
                if status >= 500 and attempt + 1 < attempts:
                    await asyncio.sleep(.2 * (attempt + 1))
                    continue
                raise GatewayError("Malformed Spot response", status=status) from None
            code = payload.get("code") if isinstance(payload, dict) else None
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            if 200 <= status < 300 and (code is None or code >= 0):
                return payload
            retry_after = None
            try:
                value = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
                if value is not None:
                    retry_after = float(_decimal(value))
            except (GatewayError, ValueError, OverflowError):
                pass
            if method == "POST":
                if status >= 500 or status in (408, 429, 418) or code in (-1000, -1006, -1007):
                    raise OrderOutcomeUnknown(code=code, status=status, retry_after=retry_after)
                raise GatewayError("Spot order rejected", code=code, status=status)
            if code == -1021 and signed and not resynchronized and attempt + 1 < attempts:
                await self._sync_time(force=True)
                resynchronized = True
                continue
            retryable = status >= 500 or code in (-1000, -1006, -1007)
            if status == 429 and retry_after is not None and retry_after <= 2:
                retryable = True
            if retryable and status != 418 and attempt + 1 < attempts:
                await asyncio.sleep(retry_after if status == 429 else .2 * (attempt + 1))
                continue
            raise GatewayError("Spot GET rejected", code=code, status=status, retry_after=retry_after)
        raise GatewayError("Spot GET retry budget exhausted")

    async def _market_metadata(self):
        if self._metadata_cache and time.monotonic()-self._metadata_cache[0] < 300:
            return self._metadata_cache[1]
        value = _object(await self._request("GET", "/api/v3/exchangeInfo", {"symbol": self.symbol}))
        if not isinstance(value.get("symbols"), list) or len(value["symbols"]) != 1:
            raise GatewayError("Unexpected Spot market metadata")
        self._metadata_cache = (time.monotonic(), value)
        return value

    async def market(self):
        """Fresh public filters/book/reference and up to 1000 UTC daily rows."""
        received_at_ms, fetched_at_monotonic = int(time.time() * 1000), time.monotonic()
        params = {"symbol": self.symbol}
        info, book, klines = await asyncio.gather(
            self._market_metadata(),
            self._request("GET", "/api/v3/ticker/bookTicker", params),
            self._request("GET", "/api/v3/klines", {**params, "interval": "5m" if self.intraday else "1d", "limit": 120 if self.intraday else 1000}))
        info, book = _object(info), _object(book)
        symbols = info.get("symbols", [])
        if not isinstance(symbols, list) or len(symbols) != 1:
            raise GatewayError("Unexpected Spot market metadata")
        spec = _object(symbols[0])
        if (spec.get("symbol"), spec.get("baseAsset"), spec.get("quoteAsset")) != (self.symbol, self.base_asset, self.quote_asset) or book.get("symbol") != self.symbol:
            raise GatewayError("Unexpected Spot market assets")
        filters = spec.get("filters")
        if not isinstance(filters, list) or not filters or any(not isinstance(f, dict) for f in filters):
            raise GatewayError("Malformed Spot filters")
        bid, ask = _decimal(book.get("bidPrice"), positive=True), _decimal(book.get("askPrice"), positive=True)
        if bid > ask:
            raise GatewayError("Crossed Spot market")
        for name in ("bidQty", "askQty"):
            _decimal(book.get(name), positive=True)
        try:
            reference = _object(await self._request("GET", "/api/v3/referencePrice", params)).get("referencePrice")
        except GatewayError as error:
            if error.code != -2043:
                raise
            reference = None
        if reference is None:
            windows = set()
            for item in filters:
                kind = item.get("filterType")
                active = kind == "MIN_NOTIONAL" and item.get("applyToMarket") is True
                active |= kind == "NOTIONAL" and (item.get("applyMinToMarket") is True or item.get("applyMaxToMarket") is True)
                if active:
                    windows.add(_integer(item.get("avgPriceMins")))
            if len(windows) != 1:
                raise GatewayError("Cannot resolve Spot notional reference window")
            window = windows.pop()
            path = "/api/v3/ticker/price" if window == 0 else "/api/v3/avgPrice"
            average = _object(await self._request("GET", path, params))
            if window and average.get("mins") != window:
                raise GatewayError("Spot average price window mismatch")
            reference = average.get("price")
        _decimal(reference, positive=True)
        if not isinstance(klines, list) or not klines:
            raise GatewayError("Missing Spot daily candles")
        previous = -1
        for row in klines:
            if not isinstance(row, list) or len(row) < 12:
                raise GatewayError("Malformed Spot daily candle")
            opened, closed = _integer(row[0]), _integer(row[6])
            if opened <= previous or closed < opened:
                raise GatewayError("Invalid Spot candle chronology")
            for value in row[1:5]:
                _decimal(value, positive=True)
            previous = opened
        server_time = await self._sync_time(force=True)
        trend_rows = await self._request("GET", "/api/v3/klines", {**params, "interval": "1h", "limit": 120}) if self.intraday else None
        return {"server_time_ms": server_time, "bid": str(book["bidPrice"]), "ask": str(book["askPrice"]),
            **({"trend_klines": trend_rows} if self.intraday else {}),
            "reference_price": str(reference), "filters": filters, "klines": klines, "symbol": self.symbol,
            "base_asset": self.base_asset, "quote_asset": self.quote_asset, "status": spec.get("status"),
            "spot_allowed": spec.get("isSpotTradingAllowed") is True,
            "bid_qty": str(book["bidQty"]), "ask_qty": str(book["askQty"]),
            # Timestamp the start, not the end, so later requests/retries cannot
            # make the earlier book falsely appear newer than it really is.
            "received_at_ms": received_at_ms, "fetched_at_monotonic": fetched_at_monotonic}

    async def account(self):
        return _object(await self._request("GET", "/api/v3/account", signed=True))

    async def commission_rate(self):
        """Conservative maximum across sides/roles, all three fee components.

        MARKET uses taker; retaining a larger maker rate is conservative. No BNB
        discount is subtracted. The result is a sizing bound, not fill accounting.
        """
        result = _object(await self._request("GET", "/api/v3/account/commission", {"symbol": self.symbol}, signed=True))
        if result.get("symbol") != self.symbol:
            raise GatewayError("Unexpected Spot commission symbol")
        groups = []
        for name in ("standardCommission", "taxCommission", "specialCommission"):
            group = _object(result.get(name))
            groups.append({key: _decimal(group.get(key)) for key in ("maker", "taker", "buyer", "seller")})
        maximum = max(sum((g[role] + g[side] for g in groups), Decimal(0))
                      for role in ("maker", "taker") for side in ("buyer", "seller"))
        if maximum >= 1:
            raise GatewayError("Unusable Spot commission rate")
        return maximum

    async def permissions(self):
        return _object(await self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True))

    async def bnb_burn_status(self):
        value = _object(await self._request("GET", "/sapi/v1/bnbBurn", signed=True))
        if type(value.get("spotBNBBurn")) is not bool:
            raise GatewayError("Missing Spot BNB fee status")
        return value["spotBNBBurn"]

    async def relevant_filters(self):
        return _object(await self._request("GET", "/api/v3/myFilters", {"symbol": self.symbol}, signed=True))

    async def open_orders(self, *, all_symbols=False):
        if not isinstance(all_symbols, bool):
            raise GatewayError("Invalid open orders scope")
        rows = await self._request("GET", "/api/v3/openOrders", {} if all_symbols else {"symbol": self.symbol}, signed=True)
        if not isinstance(rows, list) or any(not isinstance(r, dict)
                or not isinstance(r.get("symbol"), str) or not r["symbol"]
                or (not all_symbols and r["symbol"] != self.symbol) for r in rows):
            raise GatewayError("Unexpected Spot open order response")
        return rows

    async def place_order(self, client_id, side, quantity, *, limit_price=None):
        """Submit once; runtime uses LIMIT IOC for a deterministic price bound.

        Price ticks, min/max notional, fee reserve and available allocation are
        validated by the engine. IOC may expire with zero or partial execution;
        callers reconcile the terminal order and trades instead of assuming a
        successful acknowledgement means a full fill. MARKET remains available
        only for explicit legacy callers that omit limit_price.
        """
        params = {"symbol": self.symbol, "side": side, "type": "MARKET", "quantity": _quantity(quantity),
                  "newClientOrderId": _client_id(client_id), "newOrderRespType": "FULL"}
        if limit_price is not None:
            params.update(type="LIMIT", timeInForce="IOC", price=_quantity(limit_price))
        result = await self._request("POST", "/api/v3/order", params, signed=True)
        if not isinstance(result, dict) or result.get("symbol") != self.symbol or result.get("clientOrderId") != client_id:
            raise OrderOutcomeUnknown("Unexpected Spot order acknowledgement")
        return result

    async def get_order(self, client_id):
        try:
            result = await self._request("GET", "/api/v3/order", {"symbol": self.symbol, "origClientOrderId": _client_id(client_id)}, signed=True)
        except GatewayError as error:
            if error.code == -2013:
                return None
            raise
        result = _object(result)
        if result.get("symbol") != self.symbol or result.get("clientOrderId") != client_id:
            raise GatewayError("Unexpected Spot order lookup response")
        return result

    async def trades(self, order_id):
        """All currently visible fills for this order, from oldest trade ID.

        Pagination completeness is not exchange settlement completeness: the
        engine must reconcile summed qty/quote to a terminal order response.
        """
        _integer(order_id)
        cursor, result = 0, []
        for _ in range(MAX_TRADE_PAGES):
            rows = await self._request("GET", "/api/v3/myTrades", {"symbol": self.symbol,
                "orderId": order_id, "fromId": cursor, "limit": TRADE_PAGE_SIZE}, signed=True)
            if not isinstance(rows, list) or len(rows) > TRADE_PAGE_SIZE:
                raise GatewayError("Malformed Spot trade page")
            previous = cursor - 1
            for row in rows:
                row = _object(row)
                trade_id = _integer(row.get("id"))
                if row.get("symbol") != self.symbol or row.get("orderId") != order_id or trade_id <= previous:
                    raise GatewayError("Spot trade pagination identity or progress mismatch")
                previous = trade_id
            result.extend(rows)
            if len(rows) < TRADE_PAGE_SIZE:
                return result
            cursor = previous + 1
        raise GatewayError("Spot trade pagination limit reached; incomplete result withheld")
