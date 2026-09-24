"""3단계: 서버 시간 · 서명 · 재시도 정책 · exchangeInfo · 심볼 해석."""

import copy
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import urlencode

import pytest

from binance_coinm_v1.exchange import (BinanceAPIError, BinanceRestClient,
                                       ContractResolutionError, NetworkError, RequestTimeout,
                                       ServerError, resolve_contract)
from binance_coinm_v1.exchange.errors import outcome_unknown
from binance_coinm_v1.exchange.rest_client import AiohttpTransport, fmt_param

from .conftest import run
from .fakes import FakeTransport, err, no_sleep

KEY, SECRET = "k" * 64, "s" * 64


def client(tr, clock=lambda: 1_000.0, **kw):
    return BinanceRestClient("https://dapi.binance.com", KEY, SECRET, transport=tr,
                             clock=clock, sleep=no_sleep, **kw)


def test_real_transport_forbidden_in_tests():
    with pytest.raises(RuntimeError):
        AiohttpTransport()


def test_server_time_offset():
    tr = FakeTransport().add("GET", "/dapi/v1/time", {"serverTime": 1_000_000 + 250})
    c = client(tr, clock=lambda: 1_000.0)
    assert run(c.sync_time()) == 1_000_250
    assert c.time_offset_ms == 250
    assert c.now_ms() == 1_000_250


def test_signature_and_headers():
    tr = FakeTransport().add("GET", "/dapi/v1/account", {"assets": [], "positions": []})
    c = client(tr)
    run(c.signed("GET", "/dapi/v1/account"))
    call = tr.calls[0]
    assert call["headers"]["X-MBX-APIKEY"] == KEY
    p = dict(call["params"])
    sig = p.pop("signature")
    query = urlencode([("recvWindow", p["recvWindow"]), ("timestamp", p["timestamp"])])
    assert sig == hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def test_timestamp_error_resyncs_once():
    tr = FakeTransport()
    tr.add("GET", "/dapi/v1/time", {"serverTime": 1_000_000})
    tr.add("POST", "/dapi/v1/order", [err(400, -1021, "Timestamp outside recvWindow"),
                                      {"orderId": 1, "status": "NEW"}])
    c = client(tr)
    res = run(c.signed("POST", "/dapi/v1/order", {"symbol": "X"}))
    assert res["orderId"] == 1
    assert len(tr.calls_to("POST", "/dapi/v1/order")) == 2     # -1021 은 미실행이라 1회 재시도 안전


def test_mutating_request_not_retried_on_unknown_outcome():
    tr = FakeTransport().add("POST", "/dapi/v1/order", [RequestTimeout("t"), {"orderId": 2}])
    c = client(tr)
    with pytest.raises(RequestTimeout) as ei:
        run(c.signed("POST", "/dapi/v1/order", {"symbol": "X"}))
    assert outcome_unknown(ei.value)
    assert len(tr.calls_to("POST", "/dapi/v1/order")) == 1      # 자동 재전송 금지 (중복 주문 방지)


def test_server_error_and_1007_are_unknown_outcome():
    tr = FakeTransport().add("POST", "/dapi/v1/order", err(503, None, "Unknown error"))
    with pytest.raises(ServerError) as ei:
        run(client(tr).signed("POST", "/dapi/v1/order", {"symbol": "X"}))
    assert outcome_unknown(ei.value)
    tr2 = FakeTransport().add("POST", "/dapi/v1/order", err(408, -1007, "Timeout waiting"))
    with pytest.raises(ServerError) as ei2:
        run(client(tr2).signed("POST", "/dapi/v1/order", {"symbol": "X"}))
    assert outcome_unknown(ei2.value)


def test_get_is_retried_on_network_error():
    tr = FakeTransport().add("GET", "/dapi/v1/positionRisk",
                             [NetworkError("x", maybe_sent=True), ServerError(502, None, "bad"),
                              lambda p: []])
    assert run(client(tr).signed("GET", "/dapi/v1/positionRisk")) == []
    assert len(tr.calls) == 3


def test_api_error_is_definite_rejection():
    tr = FakeTransport().add("POST", "/dapi/v1/order", err(400, -2019, "Margin is insufficient."))
    with pytest.raises(BinanceAPIError) as ei:
        run(client(tr).signed("POST", "/dapi/v1/order", {"symbol": "X"}))
    assert ei.value.code == -2019 and not outcome_unknown(ei.value)


def test_mutation_guard_blocks_before_sending():
    tr = FakeTransport().add("POST", "/dapi/v1/order", {"orderId": 1})

    def guard(method, path, params):
        raise PermissionError("blocked")

    c = client(tr, mutation_guard=guard)
    with pytest.raises(PermissionError):
        run(c.signed("POST", "/dapi/v1/order", {"symbol": "X"}))
    assert tr.calls == []                       # 전송 자체가 없었다
    tr.add("GET", "/dapi/v1/openOrders", lambda p: [])
    assert run(c.signed("GET", "/dapi/v1/openOrders")) == []   # 조회는 가드 대상 아님


def test_error_messages_do_not_leak_signature():
    tr = FakeTransport().add("GET", "/dapi/v1/account", err(401, -2015, "Invalid API-key"))
    with pytest.raises(BinanceAPIError) as ei:
        run(client(tr).signed("GET", "/dapi/v1/account"))
    assert "signature" not in str(ei.value) and SECRET not in str(ei.value) and KEY not in str(ei.value)


def test_fmt_param():
    assert fmt_param(True) == "true"
    assert fmt_param(Decimal("84000.10")) == "84000.1"
    assert fmt_param(Decimal("1E+2")) == "100"
    assert fmt_param(0.1) == "0.1"


# ---------------------------------------------------------------- exchangeInfo / 심볼 해석
def test_resolve_btcusd_perp_from_exchange_info(exchange_info):
    spec = resolve_contract(exchange_info, "BTCUSD_PERP")
    assert spec.contract_type == "PERPETUAL" and spec.contract_status == "TRADING"
    assert spec.pair == "BTCUSD" and spec.margin_asset == "BTC"
    assert spec.base_asset == "BTC" and spec.quote_asset == "USD"
    assert spec.contract_size == Decimal("100")        # 거래소 값 그대로 (1 BTC 가정 아님)
    assert spec.tick_size == Decimal("0.1") and spec.step_size == Decimal("1")
    assert spec.min_qty == Decimal("1") and spec.market_max_qty == Decimal("60000")
    assert "STOP_MARKET" in spec.order_types and spec.max_num_algo_orders == 20
    assert spec.maint_margin_rate == pytest.approx(0.025)


def test_contract_size_is_not_hardcoded(exchange_info):
    xi = copy.deepcopy(exchange_info)
    for s in xi["symbols"]:
        if s["symbol"] == "BTCUSD_PERP":
            s["contractSize"] = 10
    assert resolve_contract(xi, "BTCUSD_PERP").contract_size == Decimal("10")


@pytest.mark.parametrize("mutate,msg", [
    (lambda s: s.update(contractStatus="SETTLING"), "거래 불가"),
    (lambda s: s.update(contractType="CURRENT_QUARTER"), "무기한"),
    (lambda s: s.update(marginAsset="USDT"), "증거금"),
    (lambda s: s.update(orderTypes=["LIMIT", "MARKET"]), "미지원"),
    (lambda s: s.update(filters=[f for f in s["filters"] if f["filterType"] != "LOT_SIZE"]), "LOT_SIZE"),
    (lambda s: s.pop("contractSize"), "contractSize"),
    (lambda s: s.update(contractSize=0), "contractSize"),
])
def test_resolution_fails_closed(exchange_info, mutate, msg):
    xi = copy.deepcopy(exchange_info)
    for s in xi["symbols"]:
        if s["symbol"] == "BTCUSD_PERP":
            mutate(s)
    with pytest.raises(ContractResolutionError) as ei:
        resolve_contract(xi, "BTCUSD_PERP")
    assert msg in str(ei.value)


def test_resolution_fails_without_exchange_info_or_symbol(exchange_info):
    with pytest.raises(ContractResolutionError):
        resolve_contract(None, "BTCUSD_PERP")
    with pytest.raises(ContractResolutionError):
        resolve_contract({"symbols": []}, "BTCUSD_PERP")
    with pytest.raises(ContractResolutionError) as ei:
        resolve_contract(exchange_info, "BTCUSD_260925")      # 분기물은 무기한이 아님
    assert "무기한" in str(ei.value)


def test_tick_and_step_rounding(exchange_info):
    spec = resolve_contract(exchange_info, "BTCUSD_PERP")
    assert spec.round_price("84000.16") == Decimal("84000.2")
    assert spec.round_price_away("84000.16", +1, is_stop=True) == Decimal("84000.1")   # 롱 손절 내림
    assert spec.round_price_away("84000.11", -1, is_stop=True) == Decimal("84000.2")   # 숏 손절 올림
    assert spec.round_price_away("84000.11", +1, is_stop=False) == Decimal("84000.2")  # 롱 트리거 올림
    assert spec.round_qty_down("3.9") == Decimal("3")
    assert spec.check_qty(Decimal("0"))[0] is False
    assert spec.check_qty(Decimal("2"))[0] is True
    assert spec.check_qty(Decimal("60001"))[0] is False
    eth = resolve_contract(exchange_info, "ETHUSD_PERP", base_asset="ETH", margin_asset="ETH")
    assert eth.contract_size == Decimal("10")
