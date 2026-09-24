"""실거래 안전장치: 엔진 전체 경로에서 LiveOrderGate 가 주문을 막는지 (가짜 전송, 네트워크 없음)."""

import json
from decimal import Decimal

import pytest

from binance_coinm_v1.config import Settings
from binance_coinm_v1.exchange import BinanceRestClient, LiveOrderBlocked
from binance_coinm_v1.exchange.binance_gateway import BinanceGateway
from binance_coinm_v1.exchange.models import OrderRequest
from binance_coinm_v1.exchange.rest_client import LIVE_REST
from binance_coinm_v1.execution.engine import Engine
from binance_coinm_v1.execution.live_gate import LiveOrderGate
from binance_coinm_v1.notifications import RecordingNotifier
from binance_coinm_v1.storage import Database
from binance_coinm_v1.strategy.signals import TradeSignal

from .conftest import run
from .fakes import FakeTransport, no_sleep
from .harness import Clock
from .helpers import live_settings, load_spec


def live_transport(position_amt="0"):
    tr = FakeTransport()
    tr.add("GET", "/dapi/v1/account", lambda p: {
        "assets": [{"asset": "BTC", "walletBalance": "0.05", "unrealizedProfit": "0",
                    "marginBalance": "0.05", "availableBalance": "0.05"}], "positions": []})
    tr.add("GET", "/dapi/v1/positionSide/dual", lambda p: {"dualSidePosition": False})
    tr.add("GET", "/dapi/v1/positionRisk", lambda p: [{
        "symbol": "BTCUSD_PERP", "positionAmt": position_amt, "entryPrice": "0", "markPrice": "80000",
        "unRealizedProfit": "0", "liquidationPrice": "0", "leverage": "3", "marginType": "isolated",
        "isolatedMargin": "0", "positionSide": "BOTH", "updateTime": 1}])
    tr.add("GET", "/dapi/v1/openOrders", lambda p: [])
    tr.add("GET", "/dapi/v1/openAlgoOrders", lambda p: [])
    tr.add("GET", "/dapi/v1/commissionRate", lambda p: {"symbol": "BTCUSD_PERP",
                                                        "makerCommissionRate": "0.0002",
                                                        "takerCommissionRate": "0.0005"})
    tr.add("GET", "/dapi/v2/leverageBracket", lambda p: [{"symbol": "BTCUSD_PERP", "brackets": [
        {"bracket": 1, "initialLeverage": 125, "qtyCap": 5, "maintMarginRatio": 0.004, "cum": 0}]}])
    tr.add("POST", "/dapi/v1/order", lambda p: {"orderId": 1, "clientOrderId": p["newClientOrderId"],
                                                "status": "FILLED", "type": "MARKET", "side": p["side"],
                                                "symbol": p["symbol"], "origQty": p["quantity"],
                                                "executedQty": p["quantity"]})
    tr.add("POST", "/dapi/v1/algoOrder", lambda p: {"algoId": 2, "clientAlgoId": p["clientAlgoId"],
                                                    "algoStatus": "NEW", "orderType": p["type"],
                                                    "symbol": p["symbol"], "side": p["side"]})
    return tr


def build(tmp_path, settings, validation_ok):
    tr = live_transport()
    gate = LiveOrderGate(settings, LIVE_REST, lambda: (validation_ok, "ok" if validation_ok else "DSR 미달"))
    clock = Clock()
    rest = BinanceRestClient(LIVE_REST, settings.api_key, settings.api_secret, transport=tr,
                             clock=clock, mutation_guard=gate.check, sleep=no_sleep)
    gw = BinanceGateway(rest, "live")
    db = Database(str(tmp_path / "live.sqlite3"))
    eng = Engine(settings, gw, db, load_spec(), RecordingNotifier(), clock=clock, mono=clock,
                 sleep=no_sleep, live_gate=gate)
    return eng, tr, gw, clock


def test_engine_live_mode_with_closed_validation_gate_sends_no_order(tmp_path):
    async def go():
        eng, tr, gw, clock = build(tmp_path, live_settings(), validation_ok=False)
        await eng.on_market(last=80000.0, mark=80000.0, ts_ms=int(clock.t * 1000))
        rep = await eng.startup()
        assert rep["trading_allowed"]                      # 복구는 정상 (조회만)
        sig = TradeSignal("trendy_kangaroo", 1, 1, clock.t - 3600, clock.t, 80100.0, 79200.0,
                          [81000.0], 400.0, {})
        eng.trade = eng.entries.arm(sig, None)
        tid = eng.trade.trade_id
        clock.t += 1
        await eng.on_market(last=80200.0, mark=80200.0, ts_ms=int(clock.t * 1000))
        assert tr.calls_to("POST", "/dapi/v1/order") == []
        assert tr.calls_to("POST", "/dapi/v1/algoOrder") == []
        assert eng.trade is None
        reason = eng.db.get_position(tid)["close_reason"]
        assert reason.startswith("blocked:") and "LiveOrderGate" in reason
    run(go())


def test_risk_increasing_blocked_but_protective_allowed_when_only_validation_fails(tmp_path):
    async def go():
        eng, tr, gw, clock = build(tmp_path, live_settings(), validation_ok=False)
        with pytest.raises(LiveOrderBlocked):
            await gw.place_order(OrderRequest("cm1aaaaaaaaaa-EN0", "BTCUSD_PERP", "BUY", "MARKET",
                                              quantity=Decimal(1)))
        with pytest.raises(LiveOrderBlocked):
            await gw.set_leverage("BTCUSD_PERP", 3)
        # 보유 포지션 보호·축소는 허용 (검증 리포트 만료로 손절이 막히면 안 된다)
        await gw.place_order(OrderRequest("cm1aaaaaaaaaa-SL0", "BTCUSD_PERP", "SELL", "STOP_MARKET",
                                          trigger_price=Decimal("79000"), close_position=True,
                                          working_type="MARK_PRICE"))
        await gw.place_order(OrderRequest("cm1aaaaaaaaaa-EX0", "BTCUSD_PERP", "SELL", "MARKET",
                                          quantity=Decimal(1), reduce_only=True))
        assert len(tr.calls_to("POST", "/dapi/v1/algoOrder")) == 1
        assert len(tr.calls_to("POST", "/dapi/v1/order")) == 1
        assert tr.calls_to("POST", "/dapi/v1/order")[0]["params"]["reduceOnly"] == "true"
    run(go())


@pytest.mark.parametrize("over", [{"live_trading_enabled": False}, {"live_confirmation": "nope"},
                                  {"execution_mode": "paper"}])
def test_any_missing_env_condition_blocks_everything(tmp_path, over):
    async def go():
        s = live_settings(**over)
        eng, tr, gw, clock = build(tmp_path, s, validation_ok=True)
        for req in (OrderRequest("cm1aaaaaaaaaa-EN0", "BTCUSD_PERP", "BUY", "MARKET", quantity=Decimal(1)),
                    OrderRequest("cm1aaaaaaaaaa-EX0", "BTCUSD_PERP", "SELL", "MARKET", quantity=Decimal(1),
                                 reduce_only=True),
                    OrderRequest("cm1aaaaaaaaaa-SL0", "BTCUSD_PERP", "SELL", "STOP_MARKET",
                                 trigger_price=Decimal("79000"), close_position=True)):
            with pytest.raises(LiveOrderBlocked):
                await gw.place_order(req)
        with pytest.raises(LiveOrderBlocked):
            await gw.cancel_order("BTCUSD_PERP", "cm1aaaaaaaaaa-SL0", True)
        assert [c for c in tr.calls if c["method"] != "GET"] == []
    run(go())


def test_live_gate_open_only_with_all_conditions_and_validation(tmp_path):
    async def go():
        eng, tr, gw, clock = build(tmp_path, live_settings(), validation_ok=True)
        st = await gw.place_order(OrderRequest("cm1aaaaaaaaaa-EN0", "BTCUSD_PERP", "BUY", "MARKET",
                                               quantity=Decimal(1)))
        assert st.status == "FILLED"
        assert len(tr.calls_to("POST", "/dapi/v1/order")) == 1       # 가짜 전송으로만 나감
    run(go())


@pytest.mark.parametrize("exchange_lev,expect_post", [(2, False), (3, False), (20, True)])
def test_leverage_is_never_raised_automatically(tmp_path, exchange_lev, expect_post):
    from binance_coinm_v1.runtime.bot import Bot
    from .test_runtime import fake_exchange

    async def go():
        s = live_settings(state_dir=str(tmp_path / "state"))
        tr = fake_exchange()
        tr.add("GET", "/dapi/v1/positionSide/dual", lambda p: {"dualSidePosition": False})
        tr.add("GET", "/dapi/v1/positionRisk", lambda p: [{
            "symbol": "BTCUSD_PERP", "positionAmt": "0", "entryPrice": "0", "markPrice": "80000",
            "unRealizedProfit": "0", "liquidationPrice": "0", "leverage": str(exchange_lev),
            "marginType": "isolated", "isolatedMargin": "0", "positionSide": "BOTH"}])
        tr.add("POST", "/dapi/v1/leverage", lambda p: {"leverage": int(p["leverage"]),
                                                       "symbol": p["symbol"]})
        bot = Bot(s, transport=tr, notifier=RecordingNotifier())
        # 계정 설정 단계만 검증한다 (검증 게이트 통과 상태로 가정, 가짜 전송)
        gate = LiveOrderGate(s, LIVE_REST, lambda: (True, "ok"))
        rest = BinanceRestClient(LIVE_REST, s.api_key, s.api_secret, transport=tr,
                                 mutation_guard=gate.check, sleep=no_sleep)
        bot.gateway = BinanceGateway(rest, "live")
        bot.engine = type("E", (), {"halts": {}})()
        await bot._ensure_account_config()
        posts = tr.calls_to("POST", "/dapi/v1/leverage")
        assert bool(posts) == expect_post
        if posts:
            assert posts[0]["params"]["leverage"] == "3"          # 낮추기만
    run(go())
