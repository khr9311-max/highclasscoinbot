"""Production adapters. Mutations only submit owned orders/cancel owned stops.

No transfers, account-mode changes, leverage changes or withdrawals are exposed.
"""
import asyncio
import time
from decimal import Decimal
from urllib.parse import urlsplit

from binance_coinm_v1.exchange.binance_gateway import BinanceGateway
from binance_coinm_v1.exchange.contract import resolve_contract
from binance_coinm_v1.exchange.models import OrderRequest
from binance_coinm_v1.exchange.rest_client import BinanceRestClient, HttpResponse
from btc_spot.gateway import AiohttpTransport, GatewayError
from btc_spot.store import number
from .spot_gateway import SpotGateway


class CoinTransport:
    def __init__(self):
        self.session = None

    async def request(self, method, url, headers, timeout):
        import aiohttp
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "dapi.binance.com" or parsed.fragment:
            raise ValueError("COIN-M host rejected")
        if self.session is None:
            self.session = aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with self.session.request(method, url, headers=headers, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if 300 <= response.status < 400:
                    raise ValueError("Redirect rejected")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 4_000_000:
                        raise ValueError("Response too large")
                    chunks.append(chunk)
                return HttpResponse(response.status, dict(response.headers), b"".join(chunks).decode())
        except Exception:
            raise GatewayError("COIN-M transport failed", maybe_sent=method != "GET") from None

    async def close(self):
        if self.session:
            await self.session.close()


class Venues:
    def __init__(self, config, credentials=None, *, allow_orders=False):
        key = credentials.api_key if credentials else ""
        secret = credentials.api_secret if credentials else ""
        self.config, self.allow_orders = config, allow_orders
        self.stop_requested = lambda: False
        self.spots = {s: SpotGateway(s, key, secret, allow_orders, intraday=config.strategy_mode == "intraday") for s in config.symbols}
        for gateway in self.spots.values():
            gateway.submission_guard = lambda: not self.stop_requested()
        self.rest = BinanceRestClient("https://dapi.binance.com", key, secret,
            transport=CoinTransport(), mutation_guard=self.guard)
        self.coin = BinanceGateway(self.rest, "live")
        self.spec = None
        self._info_cache = None
        self._fee_cache = None

    async def _market_metadata(self):
        if self._info_cache and time.monotonic()-self._info_cache[0] < 300:
            return self._info_cache[1]
        info = await self.rest.get_public("/dapi/v1/exchangeInfo")
        resolve_contract(info, "BTCUSD_PERP")
        self._info_cache = (time.monotonic(), info)
        return info

    def guard(self, method, path, params):
        if not self.allow_orders or self.stop_requested():
            raise ValueError("Portfolio order submission disabled")
        if not self.config.coinm_managed:
            raise ValueError("COIN-M is managed by the user")
        if (method, path) not in {("POST", "/dapi/v1/order"), ("POST", "/dapi/v1/algoOrder"),
                                  ("DELETE", "/dapi/v1/algoOrder")}:
            raise ValueError("Portfolio mutation endpoint rejected")
        ident = params.get("newClientOrderId", params.get("clientAlgoId", ""))
        if not isinstance(ident, str) or not ident.startswith("bsg_") or len(ident) > 36:
            raise ValueError("Only owned orders may be changed")
        if method == "POST" and params.get("symbol") != "BTCUSD_PERP":
            raise ValueError("Only BTC-settled COIN-M is supported")

    async def markets(self):
        await self.rest.sync_time()
        received = time.time_ns()//1_000_000
        intraday = self.config.strategy_mode == "intraday"
        info, book, premium, rows = await asyncio.gather(
            self._market_metadata(),
            self.rest.get_public("/dapi/v1/ticker/bookTicker", {"symbol": "BTCUSD_PERP"}),
            self.rest.get_public("/dapi/v1/premiumIndex", {"symbol": "BTCUSD_PERP"}),
            self.rest.get_public("/dapi/v1/klines", {"symbol": "BTCUSD_PERP", "interval": "5m" if intraday else "4h", "limit": 120 if intraday else 300}))
        self.spec = resolve_contract(info, "BTCUSD_PERP")
        book = book[0] if isinstance(book, list) and len(book) == 1 else book
        premium = premium[0] if isinstance(premium, list) and len(premium) == 1 else premium
        if book.get("symbol") != self.spec.symbol or premium.get("symbol") != self.spec.symbol:
            raise ValueError("Unexpected COIN-M market symbol")
        bid, ask, mark = map(number, (book["bidPrice"], book["askPrice"], premium["markPrice"]))
        if min(bid, ask, mark) <= 0 or bid > ask:
            raise ValueError("Invalid COIN-M book")
        result = {"coinm": {"symbol": self.spec.symbol, "bid": str(bid), "ask": str(ask),
            "mark": str(mark), "klines": rows, "server_time_ms": self.rest.now_ms(),
            "received_at_ms": received, "spec": self.spec}}
        if intraday:
            result["coinm"]["trend_klines"] = await self.rest.get_public("/dapi/v1/klines", {"symbol": "BTCUSD_PERP", "interval": "1h", "limit": 120})
        result["spot"] = dict(zip(self.spots, await asyncio.gather(*(g.market() for g in self.spots.values()))))
        return result

    async def account(self):
        first = next(iter(self.spots.values()))
        if not self.config.coinm_managed:
            # The COIN-M account is the user's: no private COIN-M request at all.
            spot, permissions, orders, bnb_burn = await asyncio.gather(
                first.account(), first.permissions(), first.open_orders(all_symbols=True), first.bnb_burn_status())
            return {"spot": spot, "permissions": permissions, "spot_bnb_burn": bnb_burn, "spot_orders": orders}
        spot, permissions, orders, coin, mode, positions, algos = await asyncio.gather(
            first.account(), first.permissions(), first.open_orders(all_symbols=True), self.coin.get_account(),
            self.coin.get_position_mode(), self.coin.get_positions("BTCUSD_PERP"),
            self.coin.get_open_algo_orders("BTCUSD_PERP"))
        coin_orders = await self.coin.get_open_orders("BTCUSD_PERP")
        # positionRisk includes leverage and margin mode even for a flat position.
        position = next((p for p in positions if p.position_side == "BOTH"), None)
        if position is None:
            raise ValueError("Missing one-way position metadata")
        bnb_burn = await first.bnb_burn_status() if self.config.strategy_mode == "aggressive" else None
        return {"spot": spot, "permissions": permissions, "spot_bnb_burn": bnb_burn, "spot_orders": orders,
                "coin": coin, "hedge_mode": mode, "position": position,
                "coin_orders": coin_orders, "algos": algos}

    async def fees(self):
        if self._fee_cache and time.monotonic()-self._fee_cache[0] < 60:
            return self._fee_cache[1]
        spot = dict(zip(self.spots, await asyncio.gather(*(g.commission_rate() for g in self.spots.values()))))
        if not self.config.coinm_managed:
            result = {"spot": spot, "coinm": None}
            self._fee_cache = (time.monotonic(), result)
            return result
        _, taker = await self.coin.get_commission_rate("BTCUSD_PERP")
        coin = number(taker)
        if not 0 <= coin < Decimal(".01"):
            raise ValueError("Invalid COIN-M commission")
        result = {"spot": spot, "coinm": coin}
        self._fee_cache = (time.monotonic(), result)
        return result

    async def submit(self, ident, venue, request):
        if venue == "spot":
            return await self.spots[request["symbol"]].place_order(ident, request["side"],
                    number(request["quantity"]), limit_price=number(request["price"]))
        emergency = request.get("emergency", False)
        return await self.coin.place_order(OrderRequest(ident, "BTCUSD_PERP", request["side"],
            "MARKET" if emergency else "LIMIT", quantity=number(request["quantity"]),
            price=None if emergency else number(request["price"]),
            time_in_force=None if emergency else "IOC", reduce_only=request.get("reduce_only", False)))

    async def stop(self, ident, side, price):
        return await self.coin.place_order(OrderRequest(ident, "BTCUSD_PERP", side, "STOP_MARKET",
            trigger_price=number(price), close_position=True, working_type="MARK_PRICE"))

    async def close(self):
        await asyncio.gather(self.coin.close(), *(g.close() for g in self.spots.values()))
