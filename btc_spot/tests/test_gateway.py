import asyncio
from collections import defaultdict, deque
from decimal import Decimal
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from btc_spot.gateway import (AiohttpTransport, GatewayError, HttpResponse,
                              OrderOutcomeUnknown, SpotGateway)


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, json.dumps(payload))


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.replies = defaultdict(deque)
        self.closed = False

    def queue(self, path, *values):
        self.replies[path].extend(values)
        return self

    async def request(self, method, url, headers, timeout, body=None):
        parsed = urlsplit(url)
        self.calls.append((method, parsed.path, parse_qs(body if body is not None else parsed.query), headers, url, body))
        if parsed.path == "/api/v3/time" and not self.replies[parsed.path]:
            return response({"serverTime": 1770000000000})
        if not self.replies[parsed.path]:
            raise AssertionError("Missing fake response: " + parsed.path)
        result = self.replies[parsed.path].popleft()
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        self.closed = True


def gateway(fake=None, *, allow_orders=False):
    return SpotGateway("dummy-key", "dummy-secret", allow_orders, fake or FakeTransport())


def ack(client_id="bsg_demo"):
    return {"symbol": "BTCUSDT", "clientOrderId": client_id, "orderId": 123,
            "status": "FILLED", "executedQty": "0.0001", "cummulativeQuoteQty": "8", "fills": []}


def test_stop_arriving_during_clock_sync_prevents_post():
    from btc_spot.gateway import OrderSubmissionStopped
    stopped = [False]
    class StopDuringClock(FakeTransport):
        async def request(self, method, url, headers, timeout, body=None):
            result = await super().request(method, url, headers, timeout, body)
            stopped[0] = True
            return result
    fake = StopDuringClock()
    client = gateway(fake, allow_orders=True)
    client.submission_guard = lambda: not stopped[0]
    with pytest.raises(OrderSubmissionStopped):
        asyncio.run(client.place_order("bsg_stop", "BUY", "0.001", limit_price="80000"))
    assert len(fake.calls) == 1
    assert all(call[0] == "GET" for call in fake.calls)


def test_signed_get_uses_spot_clock_and_exact_hmac_without_secret_in_repr():
    fake = FakeTransport().queue("/api/v3/account", response({"balances": []}))
    client = gateway(fake)
    assert asyncio.run(client.account()) == {"balances": []}
    call = fake.calls[-1]
    assert fake.calls[0][1] == "/api/v3/time"
    assert int(call[2]["timestamp"][0]) >= 1770000000000
    assert int(call[2]["timestamp"][0]) < 1770000005000
    assert call[2]["recvWindow"] == ["5000"]
    query = urlsplit(call[4]).query
    unsigned, signature = query.rsplit("&signature=", 1)
    assert signature == hmac.new(b"dummy-secret", unsigned.encode(), hashlib.sha256).hexdigest()
    assert call[3] == {"X-MBX-APIKEY": "dummy-key"}
    assert "dummy" not in repr(client)
    assert client.has_credentials
    assert not SpotGateway(transport=FakeTransport()).has_credentials


def test_missing_credentials_stops_before_public_time_request():
    fake = FakeTransport()
    with pytest.raises(GatewayError, match="credentials"):
        asyncio.run(SpotGateway(transport=fake).account())
    assert fake.calls == []


@pytest.mark.parametrize("changes", [{"client_id": "foreign_123"}, {"client_id": "bsg_"},
    {"client_id": "bsg_" + "a" * 33}, {"client_id": "bsg_bad?signature=secret"},
    {"side": "buy"}, {"quantity": "0"}, {"quantity": "-1"}, {"quantity": "NaN"},
    {"quantity": "Infinity"}, {"quantity": "1e-10000"}, {"quantity": .0001}])
def test_invalid_order_is_not_sent(changes):
    fake = FakeTransport()
    values = {"client_id": "bsg_demo", "side": "BUY", "quantity": Decimal(".0001")}
    values.update(changes)
    with pytest.raises(GatewayError):
        asyncio.run(gateway(fake, allow_orders=True).place_order(**values))
    assert fake.calls == []


def test_disabled_orders_never_make_any_http_request():
    fake = FakeTransport()
    with pytest.raises(GatewayError, match="disabled"):
        asyncio.run(gateway(fake).place_order("bsg_demo", "BUY", Decimal(".0001")))
    assert not fake.calls


def test_market_order_body_has_fixed_symbol_base_qty_and_full_response():
    fake = FakeTransport().queue("/api/v3/order", response(ack()))
    assert asyncio.run(gateway(fake, allow_orders=True).place_order("bsg_demo", "SELL", Decimal(".00010"))) == ack()
    call = fake.calls[-1]
    assert call[0] == "POST"
    assert call[4] == "https://api.binance.com/api/v3/order"
    assert call[2]["symbol"] == ["BTCUSDT"]
    assert call[2]["quantity"] == ["0.00010"]
    assert call[2]["type"] == ["MARKET"]
    assert call[2]["newOrderRespType"] == ["FULL"]
    assert call[2]["newClientOrderId"] == ["bsg_demo"]
    assert "quoteOrderQty" not in call[2]


@pytest.mark.parametrize("side,executed", [("BUY", "0"), ("SELL", "0.00004")])
def test_limit_ioc_uses_exact_price_and_allows_normal_partial_or_empty_expiry(side, executed):
    result = {**ack(), "type": "LIMIT", "timeInForce": "IOC", "price": "80024.00",
              "status": "EXPIRED", "executedQty": executed}
    fake = FakeTransport().queue("/api/v3/order", response(result))
    received = asyncio.run(gateway(fake, allow_orders=True).place_order("bsg_demo", side,
        Decimal(".0001"), limit_price=Decimal("80024.00")))
    assert received == result
    call = fake.calls[-1]
    assert call[2]["type"] == ["LIMIT"] and call[2]["timeInForce"] == ["IOC"]
    assert call[2]["price"] == ["80024.00"]
    assert call[2]["quantity"] == ["0.0001"]
    assert sum(c[0] == "POST" for c in fake.calls) == 1


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", .01])
def test_invalid_limit_price_is_rejected_before_network(price):
    fake = FakeTransport()
    with pytest.raises(GatewayError):
        asyncio.run(gateway(fake, allow_orders=True).place_order("bsg_demo", "BUY", ".0001", limit_price=price))
    assert not fake.calls


def test_limit_ioc_timeout_is_unknown_and_not_retried():
    fake = FakeTransport().queue("/api/v3/order", TimeoutError("sent or not unknown"), response(ack()))
    with pytest.raises(OrderOutcomeUnknown):
        asyncio.run(gateway(fake, allow_orders=True).place_order("bsg_demo", "BUY", ".0001", limit_price="80024"))
    assert sum(c[0] == "POST" for c in fake.calls) == 1


@pytest.mark.parametrize("reply", [TimeoutError("url?signature=leak dummy-key dummy-secret"),
    response({"code": -1007, "msg": "dummy-secret"}, 400), response({"code": -1000}, 503),
    HttpResponse(504, {}, "private raw HTML"), response(ack("foreign")),
    response({}, 302, {"Location": "https://attacker.example"})])
def test_uncertain_post_is_never_retried_and_error_is_redacted(reply):
    fake = FakeTransport().queue("/api/v3/order", reply, response(ack()))
    with pytest.raises(OrderOutcomeUnknown) as caught:
        asyncio.run(gateway(fake, allow_orders=True).place_order("bsg_demo", "BUY", Decimal(".0001")))
    assert caught.value.maybe_sent
    assert sum(call[0] == "POST" for call in fake.calls) == 1
    assert all(secret not in str(caught.value) for secret in ("dummy", "signature=", "private raw", "attacker"))


def test_even_timestamp_order_rejection_is_not_automatically_resent():
    fake = FakeTransport().queue("/api/v3/order", response({"code": -1021, "msg": "ignored"}, 400))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(gateway(fake, allow_orders=True).place_order("bsg_demo", "BUY", Decimal(".0001")))
    assert caught.value.code == -1021
    assert not caught.value.maybe_sent
    assert len(fake.calls) == 2


def test_get_timestamp_error_resynchronizes_once():
    fake = FakeTransport().queue("/api/v3/account", response({"code": -1021}, 400), response({"balances": []}))
    assert asyncio.run(gateway(fake).account()) == {"balances": []}
    assert [call[1] for call in fake.calls] == ["/api/v3/time", "/api/v3/account", "/api/v3/time", "/api/v3/account"]


def test_get_transport_retry_is_limited_and_safe(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr("btc_spot.gateway.asyncio.sleep", no_wait)
    fake = FakeTransport().queue("/api/v3/account", *[TimeoutError("secret") for _ in range(4)])
    with pytest.raises(GatewayError, match="GET transport failed"):
        asyncio.run(gateway(fake).account())
    assert sum(call[1] == "/api/v3/account" for call in fake.calls) == 3


@pytest.mark.parametrize("status", [418, 429])
def test_rate_limit_with_long_retry_after_returns_without_spamming(status):
    fake = FakeTransport().queue("/api/v3/account", response({"code": -1003}, status, {"Retry-After": "60"}))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(gateway(fake).account())
    assert caught.value.retry_after == 60
    assert sum(call[1] == "/api/v3/account" for call in fake.calls) == 1


def test_get_order_none_only_for_exchange_order_not_found():
    fake = FakeTransport().queue("/api/v3/order", response({"code": -2013}, 400), response({"code": -2015}, 401))
    client = gateway(fake)
    async def scenario():
        assert await client.get_order("bsg_demo") is None
        with pytest.raises(GatewayError) as caught:
            await client.get_order("bsg_demo")
        assert caught.value.code == -2015
    asyncio.run(scenario())


def test_commission_max_includes_roles_sides_tax_special_ignores_discount():
    def group(maker, taker, buyer, seller):
        return dict(maker=maker, taker=taker, buyer=buyer, seller=seller)
    fake = FakeTransport().queue("/api/v3/account/commission", response({"symbol": "BTCUSDT",
        "standardCommission": group(".003", ".001", ".0001", ".0002"),
        "taxCommission": group(".0001", ".0003", ".0002", ".0001"),
        "specialCommission": group(".0002", ".0004", ".0001", ".0005"),
        "discount": {"enabledForAccount": True, "discount": ".25"}}))
    assert asyncio.run(gateway(fake).commission_rate()) == Decimal(".0041")


def test_commission_missing_components_fail_closed():
    fake = FakeTransport().queue("/api/v3/account/commission", response({"symbol": "BTCUSDT", "standardCommission": {}}))
    with pytest.raises(GatewayError):
        asyncio.run(gateway(fake).commission_rate())


def test_permissions_filters_and_open_orders_return_raw_validated_shapes():
    filters = {"assetFilters": [{"filterType": "MAX_ASSET", "asset": "BTC", "limit": "1"}],
               "symbolFilters": [], "exchangeFilters": [], "rateLimits": []}
    fake = FakeTransport().queue("/sapi/v1/account/apiRestrictions", response({"enableWithdrawals": False}))
    fake.queue("/api/v3/myFilters", response(filters)).queue("/api/v3/openOrders", response([]))
    client = gateway(fake)
    async def scenario():
        assert await client.permissions() == {"enableWithdrawals": False}
        assert await client.relevant_filters() == filters
        assert await client.open_orders() == []
    asyncio.run(scenario())
    assert all(call[4].startswith("https://api.binance.com/") for call in fake.calls)


def test_account_wide_open_orders_omits_symbol_and_preserves_foreign_orders():
    orders = [{"symbol": "ETHBTC", "orderId": 9}]
    fake = FakeTransport().queue("/api/v3/openOrders", response(orders))
    assert asyncio.run(gateway(fake).open_orders(all_symbols=True)) == orders
    assert "symbol" not in fake.calls[-1][2]


def test_default_open_orders_refuses_unexpected_foreign_symbol():
    fake = FakeTransport().queue("/api/v3/openOrders", response([{"symbol": "ETHBTC", "orderId": 9}]))
    with pytest.raises(GatewayError):
        asyncio.run(gateway(fake).open_orders())


def trade(trade_id, order_id=123):
    return {"symbol": "BTCUSDT", "id": trade_id, "orderId": order_id, "qty": ".000001",
            "price": "80000", "quoteQty": ".08", "commission": ".000000001", "commissionAsset": "BTC", "isBuyer": True}


def test_trade_pagination_starts_at_zero_and_crosses_1000_without_losing_early_fills():
    fake = FakeTransport().queue("/api/v3/myTrades", response([trade(i) for i in range(1000)]), response([trade(1005)]))
    result = asyncio.run(gateway(fake).trades(123))
    assert len(result) == 1001 and result[0]["id"] == 0 and result[-1]["id"] == 1005
    calls = [call for call in fake.calls if call[1] == "/api/v3/myTrades"]
    assert [call[2]["fromId"] for call in calls] == [["0"], ["1000"]]
    assert all(call[2]["orderId"] == ["123"] for call in calls)


@pytest.mark.parametrize("rows", [[trade(3), trade(3)], [trade(4), trade(3)], [trade(1, 124)]])
def test_trade_identity_and_ordering_fail_closed(rows):
    fake = FakeTransport().queue("/api/v3/myTrades", response(rows))
    with pytest.raises(GatewayError, match="pagination"):
        asyncio.run(gateway(fake).trades(123))


def test_trade_page_cap_does_not_return_partial_result(monkeypatch):
    monkeypatch.setattr("btc_spot.gateway.MAX_TRADE_PAGES", 1)
    fake = FakeTransport().queue("/api/v3/myTrades", response([trade(i) for i in range(1000)]))
    with pytest.raises(GatewayError, match="incomplete"):
        asyncio.run(gateway(fake).trades(123))


def public_market_fake(*, reference="80000", window=5):
    fake = FakeTransport()
    fake.queue("/api/v3/exchangeInfo", response({"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
        "status": "TRADING", "isSpotTradingAllowed": True, "filters": [{"filterType": "LOT_SIZE", "minQty": ".00001", "maxQty": "100", "stepSize": ".00001"},
            {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True, "applyMaxToMarket": False, "avgPriceMins": window}]}]}))
    fake.queue("/api/v3/ticker/bookTicker", response({"symbol": "BTCUSDT", "bidPrice": "79999", "askPrice": "80001", "bidQty": ".1", "askQty": ".2"}))
    fake.queue("/api/v3/klines", response([[0, "80000", "81000", "79000", "80500", "100", 86399999, "8000000", 10, "50", "4000000", "0"]]))
    fake.queue("/api/v3/referencePrice", response({"referencePrice": reference}))
    return fake


def test_public_market_has_freshness_and_raw_daily_rows_without_credentials():
    fake = public_market_fake()
    started = int(time.time() * 1000)
    result = asyncio.run(SpotGateway(transport=fake).market())
    assert result["symbol"] == "BTCUSDT" and result["spot_allowed"]
    assert result["reference_price"] == "80000"
    assert result["bid_qty"] == ".1" and result["ask_qty"] == ".2"
    assert result["received_at_ms"] >= started
    assert result["fetched_at_monotonic"] <= time.monotonic()
    assert len(result["klines"][0]) == 12
    assert all("X-MBX-APIKEY" not in call[3] and "signature" not in call[2] for call in fake.calls)


@pytest.mark.parametrize("window", [0, 5])
def test_absent_reference_uses_only_matching_average_window(window):
    fake = public_market_fake(reference=None, window=window)
    fake.queue("/api/v3/avgPrice" if window else "/api/v3/ticker/price", response({"mins": window, "price": "79950"}))
    assert asyncio.run(SpotGateway(transport=fake).market())["reference_price"] == "79950"


def test_wrong_average_window_blocks_market_snapshot():
    fake = public_market_fake(reference=None, window=15).queue("/api/v3/avgPrice", response({"mins": 5, "price": "79950"}))
    with pytest.raises(GatewayError, match="window mismatch"):
        asyncio.run(SpotGateway(transport=fake).market())


@pytest.mark.parametrize("method,path,params,signed", [
    ("DELETE", "/api/v3/order", {}, True), ("GET", "/sapi/v1/capital/withdraw/apply", {}, True),
    ("GET", "https://attacker.example", {}, False), ("GET", "/api/v3/time", {"signature": "x"}, False),
    ("GET", "/api/v3/ticker/bookTicker", {"symbol": "ETHUSDT"}, False)])
def test_endpoint_allowlist_rejects_before_network(method, path, params, signed):
    fake = FakeTransport()
    with pytest.raises(GatewayError):
        asyncio.run(gateway(fake)._request(method, path, params, signed=signed))
    assert not fake.calls


def test_http_redirect_is_not_retried_or_followed():
    fake = FakeTransport().queue("/api/v3/account", response({}, 307, {"Location": "https://attacker.example"}))
    with pytest.raises(GatewayError, match="redirect rejected"):
        asyncio.run(gateway(fake).account())
    assert len(fake.calls) == 2


def test_real_transport_disables_redirects_environment_proxies_and_cookies(monkeypatch):
    import aiohttp
    observed = {}
    class Content:
        async def iter_chunked(self, _):
            yield b'{}'
    class Reply:
        status, headers, content = 302, {"Location": "https://attacker.example"}, Content()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    class Session:
        closed = False
        def __init__(self, **kwargs):
            observed["session"] = kwargs
        def request(self, *args, **kwargs):
            observed["request"] = kwargs
            return Reply()
        async def close(self):
            self.closed = True
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    async def scenario():
        transport = AiohttpTransport()
        assert (await transport.request("GET", "https://api.binance.com/api/v3/time", {}, 10)).status == 302
        await transport.close()
    asyncio.run(scenario())
    assert observed["session"]["trust_env"] is False
    assert isinstance(observed["session"]["cookie_jar"], aiohttp.DummyCookieJar)
    assert observed["request"]["allow_redirects"] is False


def test_close_is_idempotent_and_future_requests_are_rejected():
    fake = FakeTransport()
    client = gateway(fake)
    async def scenario():
        await client.close()
        await client.close()
        with pytest.raises(GatewayError, match="closed"):
            await client.account()
    asyncio.run(scenario())
    assert fake.closed and not fake.calls
