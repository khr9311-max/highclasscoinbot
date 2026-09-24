"""5단계: 계정·잔고·포지션·미체결(일반/조건부) 조회, 주문 라우팅, LiveOrderGate."""

from decimal import Decimal

import pytest

from binance_coinm_v1.config import LIVE_CONFIRMATION_PHRASE, Settings
from binance_coinm_v1.exchange import BinanceRestClient, LiveOrderBlocked, OrderStatusUnknown
from binance_coinm_v1.exchange.binance_gateway import BinanceGateway
from binance_coinm_v1.exchange.errors import RequestTimeout
from binance_coinm_v1.exchange.models import OrderRequest
from binance_coinm_v1.exchange.rest_client import LIVE_REST, TESTNET_REST
from binance_coinm_v1.execution.live_gate import LiveOrderGate

from .conftest import run
from .fakes import FakeTransport, err, no_sleep
from .helpers import live_settings

POS_ROW = {"symbol": "BTCUSD_PERP", "positionAmt": "-3", "entryPrice": "84000.0",
           "breakEvenPrice": "83950.0", "markPrice": "83500.00000000",
           "unRealizedProfit": "0.00002138", "liquidationPrice": "122500.5", "leverage": "3",
           "maxQty": "50", "marginType": "isolated", "isolatedMargin": "0.00121000",
           "isAutoAddMargin": "false", "positionSide": "BOTH", "notionalValue": "-0.0035",
           "isolatedWallet": "0.00119", "updateTime": 1790000000000}


def gw(tr, gate_open=True, settings=None):
    settings = settings or live_settings()
    gate = LiveOrderGate(settings, LIVE_REST,
                         validation_check=lambda: (gate_open, "ok" if gate_open else "미통과"))
    rest = BinanceRestClient(LIVE_REST, settings.api_key, settings.api_secret, transport=tr,
                             clock=lambda: 1_790_000_000.0, mutation_guard=gate.check, sleep=no_sleep)
    return BinanceGateway(rest, "live")


# ---------------------------------------------------------------- 조회
def test_balance_and_account():
    tr = FakeTransport()
    tr.add("GET", "/dapi/v1/balance", lambda p: [
        {"accountAlias": "x", "asset": "ETH", "balance": "1"},
        {"accountAlias": "x", "asset": "BTC", "balance": "0.00700000", "withdrawAvailable": "0.0065",
         "crossWalletBalance": "0.0065", "crossUnPnl": "0", "availableBalance": "0.0058",
         "updateTime": 1}])
    tr.add("GET", "/dapi/v1/account", {
        "assets": [{"asset": "BTC", "walletBalance": "0.007", "unrealizedProfit": "0.0001",
                    "marginBalance": "0.0071", "maintMargin": "0.00003", "initialMargin": "0.0012",
                    "positionInitialMargin": "0.0012", "openOrderInitialMargin": "0",
                    "maxWithdrawAmount": "0.0058", "crossWalletBalance": "0.0058",
                    "crossUnPnl": "0", "availableBalance": "0.0058", "updateTime": 1}],
        "positions": [{"symbol": "BTCUSD_PERP", "positionAmt": "3", "entryPrice": "84000",
                       "isolated": True, "positionSide": "BOTH", "leverage": "3"}],
        "canTrade": True, "feeTier": 0, "updateTime": 0})
    g = gw(tr)
    b = run(g.get_balance("BTC"))
    assert b.wallet_balance == pytest.approx(0.007) and b.available_balance == pytest.approx(0.0058)
    acc = run(g.get_account())
    btc = acc.asset("BTC")
    assert btc.margin_balance == pytest.approx(0.0071)
    assert btc.position_initial_margin == pytest.approx(0.0012)
    assert acc.positions[0].position_amt == Decimal("3")
    assert acc.positions[0].margin_type == "isolated"


def test_positions_from_position_risk():
    tr = FakeTransport().add("GET", "/dapi/v1/positionRisk", lambda p: [
        dict(POS_ROW), dict(POS_ROW, symbol="BTCUSD_261225", positionAmt="0")])
    g = gw(tr)
    pos = run(g.get_position("BTCUSD_PERP"))
    assert tr.calls[0]["params"]["pair"] == "BTCUSD"          # dapi positionRisk 는 pair 로 조회
    assert pos.position_amt == Decimal("-3") and pos.direction == -1 and pos.qty == Decimal("3")
    assert pos.entry_price == 84000.0 and pos.mark_price == 83500.0
    assert pos.liquidation_price == pytest.approx(122500.5)
    assert pos.isolated_margin_btc == pytest.approx(0.00121)


def test_open_orders_and_algo_orders():
    tr = FakeTransport()
    tr.add("GET", "/dapi/v1/openOrders", lambda p: [{
        "avgPrice": "0", "clientOrderId": "manual1", "executedQty": "0", "orderId": 11,
        "origQty": "2", "origType": "LIMIT", "price": "80000", "reduceOnly": False, "side": "BUY",
        "status": "NEW", "stopPrice": "0", "symbol": "BTCUSD_PERP", "type": "LIMIT",
        "updateTime": 5}])
    tr.add("GET", "/dapi/v1/openAlgoOrders", lambda p: [{
        "algoId": 77, "clientAlgoId": "cm1abc-SL0", "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET", "symbol": "BTCUSD_PERP", "side": "SELL",
        "positionSide": "BOTH", "quantity": "", "algoStatus": "ACTIVE", "triggerPrice": "82000.0",
        "closePosition": True, "workingType": "MARK_PRICE"}])
    g = gw(tr)
    oo = run(g.get_open_orders("BTCUSD_PERP"))
    assert oo[0].client_id == "manual1" and oo[0].status == "NEW" and not oo[0].is_algo
    ao = run(g.get_open_algo_orders("BTCUSD_PERP"))
    assert ao[0].is_algo and ao[0].status == "NEW"            # ACTIVE -> NEW 로 정규화
    assert ao[0].close_position and ao[0].trigger_price == 82000.0
    assert ao[0].is_open


def test_get_order_not_found_is_state_not_exception():
    tr = FakeTransport().add("GET", "/dapi/v1/order", err(400, -2013, "Order does not exist."))
    st = run(gw(tr).get_order("BTCUSD_PERP", "cm1x-EN0"))
    assert st.status == "NOT_FOUND" and not st.found


# ---------------------------------------------------------------- 주문 라우팅
def test_stop_goes_to_algo_endpoint_with_close_position():
    tr = FakeTransport().add("POST", "/dapi/v1/algoOrder", lambda p: {
        "algoId": 1, "clientAlgoId": p["clientAlgoId"], "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET", "symbol": "BTCUSD_PERP", "side": "SELL", "algoStatus": "NEW",
        "triggerPrice": p["triggerPrice"], "closePosition": True, "workingType": p["workingType"]})
    g = gw(tr)
    st = run(g.place_order(OrderRequest("cm1abc-SL0", "BTCUSD_PERP", "SELL", "STOP_MARKET",
                                        trigger_price=Decimal("82000.1"), close_position=True,
                                        working_type="MARK_PRICE")))
    p = tr.calls[0]["params"]
    assert tr.calls[0]["path"] == "/dapi/v1/algoOrder"         # /dapi/v1/order 로 보내면 -4120
    assert p["algoType"] == "CONDITIONAL" and p["type"] == "STOP_MARKET"
    assert p["triggerPrice"] == "82000.1" and p["closePosition"] == "true"
    assert "quantity" not in p and "reduceOnly" not in p
    assert p["workingType"] == "MARK_PRICE" and p["clientAlgoId"] == "cm1abc-SL0"
    assert st.is_algo and st.status == "NEW"


def test_market_order_params_and_reduce_only():
    tr = FakeTransport().add("POST", "/dapi/v1/order", lambda p: {
        "clientOrderId": p["newClientOrderId"], "orderId": 5, "status": "FILLED", "type": "MARKET",
        "side": p["side"], "symbol": p["symbol"], "origQty": p["quantity"],
        "executedQty": p["quantity"], "reduceOnly": p.get("reduceOnly") == "true"})
    g = gw(tr)
    st = run(g.place_order(OrderRequest("cm1abc-EX0", "BTCUSD_PERP", "SELL", "MARKET",
                                        quantity=Decimal("2"), reduce_only=True)))
    p = tr.calls[0]["params"]
    assert p["type"] == "MARKET" and p["quantity"] == "2" and p["reduceOnly"] == "true"
    assert p["newClientOrderId"] == "cm1abc-EX0" and p["positionSide"] == "BOTH"
    assert st.status == "FILLED" and st.avg_price is None     # 통합 후 응답엔 avgPrice 없음


def test_unknown_outcome_raises_order_status_unknown():
    tr = FakeTransport().add("POST", "/dapi/v1/order", RequestTimeout("t"))
    with pytest.raises(OrderStatusUnknown) as ei:
        run(gw(tr).place_order(OrderRequest("cm1abc-EN0", "BTCUSD_PERP", "BUY", "MARKET",
                                            quantity=Decimal("1"))))
    assert ei.value.client_id == "cm1abc-EN0"


def test_cancel_rejected_returns_actual_state():
    tr = FakeTransport()
    tr.add("DELETE", "/dapi/v1/algoOrder", err(400, -2011, "Unknown order sent."))
    tr.add("GET", "/dapi/v1/algoOrder", lambda p: {
        "algoId": 3, "clientAlgoId": p["clientAlgoId"], "orderType": "STOP_MARKET",
        "symbol": "BTCUSD_PERP", "side": "SELL", "algoStatus": "FINISHED", "actualOrderId": "991",
        "actualQty": "3", "actualPrice": "81990.0", "triggerPrice": "82000"})
    st = run(gw(tr).cancel_order("BTCUSD_PERP", "cm1abc-SL0", is_algo=True))
    assert st.status == "FINISHED" and st.actual_order_id == "991"   # 이미 발동·체결됨


def test_closed_gate_blocks_orders_and_account_changes_before_sending():
    tr = FakeTransport().add("POST", "/dapi/v1/order", {"orderId": 1})
    tr.add("POST", "/dapi/v1/leverage", {"leverage": 3})
    g = gw(tr, gate_open=False)                                # 검증 게이트 미통과
    with pytest.raises(LiveOrderBlocked):
        run(g.place_order(OrderRequest("cm1abc-EN0", "BTCUSD_PERP", "BUY", "MARKET",
                                       quantity=Decimal("1"))))
    with pytest.raises(LiveOrderBlocked):
        run(g.set_leverage("BTCUSD_PERP", 3))
    assert tr.calls == []


# ---------------------------------------------------------------- LiveOrderGate
def test_live_gate_default_closed():
    gate = LiveOrderGate(Settings.build(), LIVE_REST, validation_check=lambda: (True, "ok"))
    assert not gate.is_open()
    with pytest.raises(LiveOrderBlocked):
        gate.check("POST", "/dapi/v1/order", {})


def test_live_gate_requires_all_conditions():
    ok = lambda: (True, "ok")
    assert LiveOrderGate(live_settings(), LIVE_REST, ok).is_open()
    for over in ({"binance_env": "testnet"}, {"live_trading_enabled": False},
                 {"live_confirmation": ""}, {"live_confirmation": "yes"},
                 {"execution_mode": "paper"}):
        s = live_settings(**over) if over.get("binance_env") != "testnet" else \
            live_settings(binance_env="testnet")
        gate = LiveOrderGate(s, LIVE_REST, ok)
        assert not gate.is_open(), over
    # 설정이 다 맞아도 검증 게이트가 닫히면 거부
    g2 = LiveOrderGate(live_settings(), LIVE_REST, lambda: (False, "DSR 미달"))
    assert not g2.is_open() and any("DSR" in r for r in g2.reasons())
    # 검증 확인 자체가 예외면 닫힘
    def boom():
        raise RuntimeError("db")
    assert not LiveOrderGate(live_settings(), LIVE_REST, boom).is_open()
    # 실거래 설정인데 전송 대상이 테스트넷 호스트면 거부
    assert not LiveOrderGate(live_settings(), TESTNET_REST, ok).is_open()


def test_testnet_gate_never_targets_live_host():
    s = Settings.build(binance_env="testnet", execution_mode="testnet")
    assert LiveOrderGate(s, TESTNET_REST).is_open()
    assert not LiveOrderGate(s, LIVE_REST).is_open()
