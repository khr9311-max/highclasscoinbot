"""
10단계: 복구. '재시작' 은 같은 DB·같은 (종이) 거래소 상태로 새 Engine 을 만드는 것으로 흉내낸다.
"""

from decimal import Decimal

import pytest

from binance_coinm_v1.exchange.errors import BinanceAPIError, NetworkError, RequestTimeout
from binance_coinm_v1.exchange.models import OrderRequest, OrderState
from binance_coinm_v1.exchange.paper_gateway import Fault
from binance_coinm_v1.execution import order_ids as ids
from binance_coinm_v1.execution.state_machine import TradeRecord

from .conftest import run
from .harness import Harness
from .test_execution import TGT, long_open


def restart(h, tmp_path, **over):
    """같은 DB·같은 종이 거래소로 프로세스 재시작."""
    return Harness(tmp_path, db=h.db, paper=h.paper, clock=h.clock, **over)


def entry_orders(h):
    return [o for o in h.paper.orders.values() if "-EN" in o["client_id"]]


# ---------------------------------------------------------------- 고아 포지션
@pytest.mark.parametrize("policy", ["protect", "close"])
def test_orphan_position_is_recovered_not_duplicated(tmp_path, policy):
    async def go():
        h = Harness(tmp_path, orphan_position_policy=policy)
        h.paper.update_market(last=80000.0, mark=80000.0, ts_ms=int(h.clock.t * 1000))
        await h.paper.place_order(OrderRequest("manual-1", "BTCUSD_PERP", "BUY", "MARKET",
                                               quantity=Decimal(5)))      # 봇 밖에서 생긴 포지션
        h.paper.set_event_sink(h.engine.enqueue_user_event)
        rep = await h.start(80000.0)
        assert any(a.startswith("orphan_adopted") for a in rep["actions"])
        assert h.db.risk_events("paper", "orphan_position")
        assert entry_orders(h) == []                     # 로컬 기록이 없다고 새로 진입하지 않음
        if policy == "protect":
            t = h.trade
            assert t.adopted and t.state == "PROTECTED" and h.position() == Decimal(5)
            stop = [a for a in h.algos() if a["type"] == "STOP_MARKET"][0]
            assert stop["trigger"] == pytest.approx(80000.0 * 0.97, abs=0.2)
            ok, why = h.engine._entry_gate()
            assert not ok and "고아" in why                 # 사람 확인 전 신규 진입 차단
        else:
            assert h.position() == 0 and h.trade is None
    run(go())


# ---------------------------------------------------------------- 고아/외부 주문
def test_orphan_bot_orders_cancelled_foreign_orders_block(tmp_path):
    async def go():
        h = Harness(tmp_path)
        h.paper.update_market(last=80000.0, mark=80000.0, ts_ms=int(h.clock.t * 1000))
        await h.paper.place_order(OrderRequest("cm1deadbeef00-SL0", "BTCUSD_PERP", "SELL",
                                               "STOP_MARKET", trigger_price=Decimal("79000"),
                                               close_position=True, working_type="MARK_PRICE"))
        await h.paper.place_order(OrderRequest("manual-stop", "BTCUSD_PERP", "SELL", "STOP_MARKET",
                                               trigger_price=Decimal("78000"), close_position=True))
        rep = await h.start(80000.0)
        assert h.paper.algos["cm1deadbeef00-SL0"]["status"] == "CANCELED"   # 이 봇의 고아 주문
        assert h.paper.algos["manual-stop"]["status"] == "NEW"              # 남의 주문은 그대로
        assert rep["foreign_orders"] == ["manual-stop"]
        ok, why = h.engine._entry_gate()
        assert not ok and "만들지 않은 주문" in why
    run(go())


# ---------------------------------------------------------------- 손절 누락
def test_missing_stop_recreated_on_restart(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        stop_cid = t.orders["stop"]
        h.paper.algos[stop_cid]["status"] = "CANCELED"   # 이벤트 없이 사라짐 (프로세스 다운 중)
        h2 = restart(h, tmp_path)
        rep = await h2.engine.startup()
        assert f"missing_stop:{t.trade_id}" in rep["issues"]
        t2 = h2.trade
        assert t2.state == "PROTECTED" and t2.orders["stop"] != stop_cid
        stops = [a for a in h2.algos() if a["type"] == "STOP_MARKET"]
        assert len(stops) == 1 and stops[0]["trigger"] == 79200.0
    run(go())


def test_stop_cancelled_externally_is_replaced_immediately(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        old = t.orders["stop"]
        await h.paper.cancel_order("BTCUSD_PERP", old, True)       # 누군가 손절을 지웠다
        await h.engine.drain_user_events()
        t = h.trade
        assert t.orders["stop"] != old and h.db.risk_events("paper", "stop_disappeared")
        assert len([a for a in h.algos() if a["type"] == "STOP_MARKET"]) == 1
    run(go())


# ---------------------------------------------------------------- 중복 주문 방지 (타임아웃)
def test_entry_response_lost_is_resolved_without_duplicate(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        h.paper.inject(Fault("place", RequestTimeout("lost"), when="after", purpose="ENTRY"))
        h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(80150.0)
        assert len(entry_orders(h)) == 1                           # 재전송 없음
        assert h.position() == Decimal(15) and h.trade.state == "PROTECTED"
        assert h.db.risk_events("paper", "order_status_unknown")
    run(go())


def test_entry_not_reached_exchange_retries_with_new_id_once(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        h.paper.inject(Fault("place", NetworkError("refused", maybe_sent=True), when="before",
                             purpose="ENTRY"))
        t = h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(80150.0)                                      # 불명 -> 조회 -> 없음 확정
        assert h.position() == 0 and t.state == "ENTRY_PENDING"
        assert h.db.get_order(t.orders["entry"])["status"] == "NOT_PLACED"
        await h.tick(80160.0)                                      # 새 순번으로 1회 재시도
        assert h.position() == Decimal(15)
        assert [o["client_id"][-3:] for o in entry_orders(h)] == ["EN1"]
    run(go())


def test_slow_order_visibility_is_waited_not_resent(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        real_get = h.paper.get_order
        calls = {"n": 0}

        async def slow_get(symbol, cid):
            calls["n"] += 1
            if calls["n"] == 1:
                return OrderState.not_found(cid, symbol)          # 아직 조회에 안 잡힘
            return await real_get(symbol, cid)

        h.paper.get_order = slow_get
        h.paper.inject(Fault("place", RequestTimeout("slow"), when="after", purpose="ENTRY"))
        h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(80150.0)
        assert len(entry_orders(h)) == 1 and h.position() == Decimal(15)
    run(go())


def test_stop_response_lost_resolved_by_query(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        h.paper.inject(Fault("place", RequestTimeout("lost"), when="after", purpose="STOP"))
        h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(80150.0)
        assert h.trade.state == "PROTECTED"
        assert len([a for a in h.algos() if a["type"] == "STOP_MARKET"]) == 1
    run(go())


# ---------------------------------------------------------------- 웹소켓 재연결
def test_reconnect_reconciliation_catches_missed_stop_fill(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        h.paper.set_event_sink(lambda e: None)                    # 연결 끊김: 이벤트 유실
        h.paper.update_market(last=79100.0, mark=79100.0, ts_ms=int((h.clock.t + 5) * 1000))
        assert h.position() == 0 and h.trade.state == "PROTECTED"   # 엔진은 아직 모름
        h.paper.set_event_sink(h.engine.enqueue_user_event)
        rep = await h.engine.reconcile("reconnected")
        rec = h.db.get_position(t.trade_id)
        assert rec["state"] == "CLOSED" and rec["close_reason"] == "closed_while_offline"
        assert rec["accounting"]["realized_pnl_btc"] < 0          # REST 로 체결 복원
        assert any(a.startswith("closed_while_offline") for a in rep["actions"])
        assert h.algos() == []
    run(go())


# ---------------------------------------------------------------- 재시작
def _pending_trade_with_entry_order(h, cid_seq=0):
    t = h.arm(1, 80100.0, 79200.0, TGT)
    cid = ids.make(t.trade_id, "EN", t.next_seq("EN"))
    t.orders["entry"] = cid
    h.engine.ctx.save(t)
    h.db.upsert_order({"client_order_id": cid, "trade_id": t.trade_id, "purpose": "ENTRY",
                       "symbol": "BTCUSD_PERP", "side": "BUY", "order_type": "MARKET",
                       "quantity": "15", "status": "PENDING_SUBMIT", "mode": "paper"})
    return t, cid


def test_restart_during_entry_order_was_executed(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        t, cid = _pending_trade_with_entry_order(h)
        # 주문은 거래소에서 체결됐는데 결과를 기록하기 전에 프로세스가 죽었다
        h.paper.set_event_sink(lambda e: None)
        await h.paper.place_order(OrderRequest(cid, "BTCUSD_PERP", "BUY", "MARKET", quantity=Decimal(15)))
        h2 = restart(h, tmp_path)
        rep = await h2.start(80000.0)
        t2 = h2.trade
        assert t2.trade_id == t.trade_id and t2.state == "PROTECTED"
        assert t2.qty_initial_d == Decimal(15) and t2.entry_avg_price == 80000.0
        assert len(entry_orders(h2)) == 1                        # 다시 사지 않음
        assert any(a.startswith("entry_filled_while_offline") for a in rep["actions"])
    run(go())


def test_restart_during_entry_order_never_sent(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        t, cid = _pending_trade_with_entry_order(h)
        h2 = restart(h, tmp_path)
        await h2.start(80000.0)
        assert h2.trade is None and h2.position() == 0
        assert h2.db.get_order(cid)["status"] == "NOT_PLACED"
        assert h2.db.get_position(t.trade_id)["close_reason"].startswith("stale_entry")
    run(go())


def test_restart_after_fill_before_protection(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        t, cid = _pending_trade_with_entry_order(h)
        await h.paper.place_order(OrderRequest(cid, "BTCUSD_PERP", "BUY", "MARKET", quantity=Decimal(15)))
        h.db.upsert_order({"client_order_id": cid, "status": "FILLED", "executed_qty": "15"})
        t.qty_initial = t.qty_open = "15"
        t.entry_avg_price = 80000.0
        t.opened_at = h.clock.t
        t.entry_fill_time_ms = int(h.clock.t * 1000)
        h.engine.ctx.transition(t, "ENTRY_FILLED", "체결 (여기서 프로세스 종료)")
        assert h.algos() == []                                     # 손절 없이 죽었다
        h2 = restart(h, tmp_path)
        await h2.start(80000.0)
        t2 = h2.trade
        assert t2.state == "PROTECTED" and len([a for a in h2.algos() if a["type"] == "STOP_MARKET"]) == 1
        assert h2.position() == Decimal(15)
    run(go())


def test_restart_during_exit_position_still_open(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        h.engine.ctx.transition(t, "CLOSING", "청산 시작 (주문 전 종료)")
        t.close_reason = "time"
        h.engine.ctx.save(t)
        h2 = restart(h, tmp_path)
        rep = await h2.start(80100.0)
        assert h2.position() == 0 and h2.trade is None
        rec = h2.db.get_position(t.trade_id)
        assert rec["state"] == "CLOSED" and rec["close_reason"] == "time"
        assert any(a.startswith("resumed_close") for a in rep["actions"])
    run(go())


def test_restart_during_exit_already_flat(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        h.engine.ctx.transition(t, "CLOSING", "청산 주문 체결 후 정리 전 종료")
        t.close_reason = "reversal_signal"
        h.engine.ctx.save(t)
        h.paper.set_event_sink(lambda e: None)
        await h.paper.place_order(OrderRequest(ids.make(t.trade_id, "EX", 0), "BTCUSD_PERP", "SELL",
                                               "MARKET", quantity=Decimal(15), reduce_only=True))
        h2 = restart(h, tmp_path)
        await h2.start(80100.0)
        rec = h2.db.get_position(t.trade_id)
        assert rec["state"] == "CLOSED" and rec["close_reason"] == "reversal_signal"
        assert h2.algos() == []                                    # 남은 손절·TP 정리
        assert rec["accounting"]["trading_fee_btc"] > 0
    run(go())


def test_stale_pending_signal_after_long_downtime(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        h.db.kv_set("paper:heartbeat", h.clock.t - 600)            # 10분 다운
        t = h.arm(1, 80100.0, 79200.0, TGT)
        t.expire_ts = h.clock.t + 3600
        h.engine.ctx.save(t)
        h2 = restart(h, tmp_path)
        await h2.start(80000.0)
        assert h2.trade is None
        assert h2.db.get_position(t.trade_id)["close_reason"] == "stale_after_restart"
        # 짧은 재시작(30초)은 대기 신호 유지
        h2.db.kv_set("paper:heartbeat", h2.clock.t - 30)
        t3 = h2.arm(1, 80100.0, 79200.0, TGT)
        t3.expire_ts = h2.clock.t + 3600
        h2.engine.ctx.save(t3)
        h3 = restart(h2, tmp_path)
        await h3.start(80000.0)
        assert h3.trade is not None and h3.trade.trade_id == t3.trade_id
    run(go())


def test_exchange_query_failure_keeps_trading_blocked(tmp_path):
    async def go():
        h = Harness(tmp_path)
        h.paper.inject(Fault("get_account", NetworkError("down"), times=1))
        rep = await h.start(80000.0)
        assert rep["trading_allowed"] is False
        ok, why = h.engine._entry_gate()
        assert not ok
        rep2 = await h.engine.reconcile("retry")
        assert rep2["trading_allowed"] is True
    run(go())


def test_failed_close_in_error_state_is_resumed_not_held(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        # 청산 주문이 3번 다 거부돼 ERROR 가 된 상황
        h.paper.inject(Fault("place", BinanceAPIError(400, -1001, "Internal error"), purpose="EXIT",
                             times=3))
        await h.engine.exits.close(t, "time")
        assert t.state == "ERROR" and h.position() == Decimal(15)
        assert [a for a in h.algos() if a["type"] == "STOP_MARKET"]        # 손절은 남아 있다
        rep = await h.engine.reconcile("periodic")
        rec = h.db.get_position(t.trade_id)
        assert rec["state"] == "CLOSED" and rec["close_reason"] == "time" and h.position() == 0
        assert any(a.startswith("resumed_close") for a in rep["actions"])
    run(go())
