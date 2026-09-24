"""
9단계: 모의 실행 (종이 게이트웨이, 실제 네트워크 없음).
LONG / SHORT / 부분 체결 / 보호 손절 / TP1·TP2·TP3 / 추적 / reduce-only 청산 / 취소 /
즉시 반전 금지 / 중복 이벤트 / 보호 실패 시 비상 청산.
"""

from decimal import Decimal

import pytest

from binance_coinm_v1.exchange.errors import BinanceAPIError
from binance_coinm_v1.exchange.paper_gateway import Fault
from binance_coinm_v1.execution.state_machine import InvalidTransition
from binance_coinm_v1.strategy.price_action import atr

from .conftest import run
from .harness import Harness, flat_bars
from .synth import bars_from, bullish_tk_rows, mirror_rows

TGT = [81000.0, 82000.0, 83000.0]


async def long_open(h, qty_equity=0.05):
    await h.start(80000.0)
    t = h.arm(1, 80100.0, 79200.0, TGT)
    await h.tick(80050.0)
    assert t.state == "ENTRY_PENDING"
    await h.tick(80150.0)
    return h.trade


def test_long_entry_uses_actual_fill_and_protects(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        assert t.state == "PROTECTED"
        assert t.entry_avg_price == 80150.0            # 실제 체결가 (트리거 80100 이 아님)
        assert t.qty_initial_d == Decimal(15) and h.position() == Decimal(15)
        stops = [a for a in h.algos() if a["type"] == "STOP_MARKET"]
        assert len(stops) == 1 and stops[0]["close_position"]
        assert stops[0]["trigger"] == 79200.0 and stops[0]["working_type"] == "MARK_PRICE"
        assert stops[0]["side"] == "SELL"
        tps = sorted((a for a in h.algos() if a["type"] == "TAKE_PROFIT_MARKET"),
                     key=lambda a: a["trigger"])
        assert [a["qty"] for a in tps] == [Decimal(3), Decimal(4), Decimal(4)]   # 누적 내림
        assert all(a["reduce_only"] for a in tps) and [a["trigger"] for a in tps] == TGT
        assert h.states(t.trade_id)[:6] == ["IDLE", "SIGNAL_DETECTED", "ENTRY_PENDING",
                                            "ENTRY_FILLED", "PROTECTING", "PROTECTED"]
        steps = t.sizing
        assert steps["2_risk_budget_btc"] == pytest.approx(0.05 * 0.005)
        assert steps["loss_at_stop_btc"] <= steps["2_risk_budget_btc"]
        assert "entry" in h.notes.kinds() and "signal" in h.notes.kinds()
    run(go())


def test_short_entry_is_symmetric(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        t = h.arm(-1, 79900.0, 80800.0, [79000.0, 78000.0, 77000.0])
        await h.tick(79950.0)
        assert t.state == "ENTRY_PENDING"
        await h.tick(79850.0)
        t = h.trade
        assert t.state == "PROTECTED" and h.position() < 0
        assert t.entry_avg_price == 79850.0
        stop = [a for a in h.algos() if a["type"] == "STOP_MARKET"][0]
        assert stop["side"] == "BUY" and stop["trigger"] == 80800.0 and stop["close_position"]
        tps = [a for a in h.algos() if a["type"] == "TAKE_PROFIT_MARKET"]
        assert all(a["side"] == "BUY" and a["reduce_only"] for a in tps)
        await h.tick(80900.0)                           # 숏 손절 (마크 기준)
        assert h.trade is None and h.position() == 0
        rows = h.db.closed_positions("paper")
        assert rows[-1]["close_reason"] == "stop" and rows[-1]["accounting"]["realized_pnl_btc"] < 0
    run(go())


def test_stop_hit_closes_and_accounts_in_btc(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        tid = t.trade_id
        await h.tick(79150.0)                           # 마크가 손절 79200 아래
        assert h.trade is None and h.position() == 0
        rec = h.db.get_position(tid)
        assert rec["state"] == "CLOSED" and rec["close_reason"] == "stop"
        acc = rec["accounting"]
        assert acc["realized_pnl_btc"] < 0 and acc["trading_fee_btc"] > 0
        assert acc["net_pnl_btc"] == pytest.approx(acc["realized_pnl_btc"] - acc["trading_fee_btc"]
                                                   + acc["funding_fee_btc"])
        assert acc["net_pnl_usd"] < 0 and acc["net_pnl_krw"] == pytest.approx(acc["net_pnl_usd"] * 1390.0)
        assert acc["local_realized_pnl_btc"] == pytest.approx(acc["realized_pnl_btc"], rel=1e-6)
        assert h.algos() == []                          # 남은 TP 전부 취소
        assert "CLOSING" in h.states(tid) and h.states(tid)[-1] == "CLOSED"
    run(go())


def test_tp1_tp2_tp3_then_trailing(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        tid = t.trade_id
        rows = [(80500.0, 80580.0, 80420.0, 80500.0)] * 66      # 진입 전 봉들

        async def bar(high, low):
            # 새 봉 하나를 이어 붙인다 (종가가 고가 근처인 양봉 - 반전 신호가 아님)
            rows.append((low + (high - low) * 0.1, high, low, high - (high - low) * 0.05))
            h.clock.t += 3600
            bars = bars_from(rows, 3600, t0=h.clock.t - 1 - len(rows) * 3600)
            htf = flat_bars(80, 80500.0, h.clock.t - 1, period=14400)
            await h.engine.on_bar_close(bars, htf)
            return bars

        await h.tick(81000.0)                           # TP1 (3계약)
        assert h.position() == Decimal(12) and h.trade.state == "TP1"
        await bar(81050.0, 80400.0)                     # 사다리: 손절 -> 본전
        assert h.trade.stop_price == 80150.0
        stops = [a for a in h.algos() if a["type"] == "STOP_MARKET"]
        assert len(stops) == 1 and stops[0]["trigger"] == 80150.0   # 새 손절 확인 후 옛것 취소
        await h.tick(82000.0)                           # TP2 (4계약)
        assert h.position() == Decimal(8) and h.trade.state == "TP2"
        await bar(82050.0, 81500.0)
        assert h.trade.stop_price == 81000.0
        await h.tick(83000.0)                           # TP3 (4계약)
        assert h.position() == Decimal(4) and h.trade.state == "TP3"
        await bar(83100.0, 82900.0)                     # 목표 소진 + 보유 3봉째 -> 추적 시작
        t = h.trade
        assert t.state == "TRAILING"
        assert t.stop_price == 82000.0                  # 최근 3봉 저가(80400)-버퍼 < 사다리 손절
        await bar(83300.0, 83000.0)
        bars = await bar(83500.0, 83100.0)              # 최근 3봉 저가 82900 -> 손절 상향
        trail = h.trade.stop_price
        assert trail == pytest.approx(82900.0 - 0.05 * atr(bars, len(bars) - 1), abs=0.2)
        await h.tick(trail - 20.0)                      # 추적 손절 체결
        assert h.trade is None and h.position() == 0
        rec = h.db.get_position(tid)
        assert rec["close_reason"] == "trailing_stop"
        st = h.states(tid)
        for s in ("TP1", "TP2", "TP3", "TRAILING", "CLOSING", "CLOSED"):
            assert s in st
        assert rec["accounting"]["realized_pnl_btc"] > 0
    run(go())


def test_partial_entry_fill_uses_executed_quantity(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        h.paper.partial_next = 0.5
        h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(80150.0)
        t = h.trade
        assert t.qty_initial_d == Decimal(7) and h.position() == Decimal(7)   # 15 중 7 체결
        tps = sorted(a["qty"] for a in h.algos() if a["type"] == "TAKE_PROFIT_MARKET")
        assert sum(tps) <= 7 and t.state == "PROTECTED"
        assert any("부분" in r["reason"] for r in h.db.transitions(t.trade_id))
    run(go())


def test_time_exit_is_reduce_only_market(tmp_path):
    async def go():
        h = Harness(tmp_path, max_hold_bars=2)
        t = await long_open(h)
        for _ in range(2):
            h.clock.t += 3600
            await h.engine.on_bar_close(flat_bars(70, 80300.0, h.clock.t - 1),
                                        flat_bars(80, 80300.0, h.clock.t - 1, period=14400))
        assert h.trade is None and h.position() == 0
        rec = h.db.get_position(t.trade_id)
        assert rec["close_reason"] == "time"
        exits = [o for o in h.db.list_orders("paper", trade_id=t.trade_id) if o["purpose"] == "EXIT"]
        assert exits and all(o["reduce_only"] for o in exits) and exits[0]["status"] == "FILLED"
    run(go())


def test_cancel_on_stop_before_entry_and_expiry(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        t = h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(79190.0)                           # 돌파 전 손절선부터
        assert h.trade is None and h.db.get_position(t.trade_id)["close_reason"] == "stop_before_entry"
        t2 = h.arm(1, 80100.0, 79200.0, TGT)
        h.clock.t = t2.expire_ts + 1
        await h.tick(80000.0)
        assert h.db.get_position(t2.trade_id)["close_reason"] == "expired"
        assert h.position() == 0 and not h.paper.orders
    run(go())


def test_protective_stop_failure_triggers_emergency_close(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        h.paper.inject(Fault("place", BinanceAPIError(400, -1111, "Precision is over the maximum"),
                             purpose="STOP", times=3))
        t = h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(80150.0)
        assert h.position() == 0 and h.trade is None
        rec = h.db.get_position(t.trade_id)
        assert rec["close_reason"] == "protect_failed" and "PROTECTED" not in h.states(t.trade_id)
        em = [o for o in h.db.list_orders("paper", trade_id=t.trade_id) if o["purpose"] == "EMERGENCY"]
        assert em and em[0]["reduce_only"]
        assert any(c for k, _, c in h.notes.messages if c)          # 치명 알림
        assert h.db.risk_events("paper", "protection_failed")
    run(go())


def test_stop_that_would_immediately_trigger_closes_now(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        h.paper.inject(Fault("place", BinanceAPIError(400, -2021, "Order would immediately trigger."),
                             purpose="STOP"))
        t = h.arm(1, 80100.0, 79200.0, TGT)
        await h.tick(80150.0)
        assert h.position() == 0 and h.db.get_position(t.trade_id)["close_reason"] == "protect_failed"
    run(go())


def _scaled(rows, k):
    return [(o * k, hh * k, ll * k, c * k) for o, hh, ll, c in rows]


def test_signal_from_bar_close_is_armed_with_tick_rounding(tmp_path):
    async def go():
        h = Harness(tmp_path)
        rows = _scaled(bullish_tk_rows(), 500.0)
        n = len(rows)
        await h.start(rows[-1][3])
        t0 = h.clock.t - 60 - n * 3600                    # 마지막 봉이 1분 전에 마감
        bars = bars_from(rows, 3600, t0=t0)
        htf = flat_bars(80, 70000.0, bars.close_time(n - 1) - 3600, period=14400)
        ev = await h.engine.on_bar_close(bars, htf)
        sig = ev.entry_for(1)
        assert sig is not None
        t = h.trade
        assert t.state == "ENTRY_PENDING" and t.direction == 1
        a = atr(bars, n - 1)
        assert t.entry_trigger >= bars.h[-1] + 0.05 * a and \
            t.entry_trigger - (bars.h[-1] + 0.05 * a) < 0.1          # 틱 올림
        assert t.stop_price <= bars.l[-1] - 0.05 * a                  # 손절 내림
        sigs = h.db.list_signals("paper")
        assert sigs[0]["action"] == "armed"
        # 같은 봉을 다시 받아도 중복 등록 없음
        assert await h.engine.on_bar_close(bars, htf) is None
    run(go())


def test_no_instant_reversal_close_first_then_wait_new_signal(tmp_path):
    async def go():
        h = Harness(tmp_path)
        bear = _scaled(mirror_rows(bullish_tk_rows(), 400.0), 330.0)
        n = len(bear)
        px = bear[-1][3]
        await h.start(px)
        t = h.arm(1, px + 30.0, px * 0.97, [px * 1.05])
        await h.tick(px + 40.0)
        assert h.trade.state == "PROTECTED"
        long_id = h.trade.trade_id
        h.clock.t += 7200
        bars = bars_from(bear, 3600, t0=h.clock.t - 60 - n * 3600)
        htf = flat_bars(80, px, bars.close_time(n - 1) - 3600, period=14400)
        ev = await h.engine.on_bar_close(bars, htf)
        assert ev.entry_for(-1) is not None                   # 약세 추세 캥거루
        rec = h.db.get_position(long_id)
        assert rec["state"] == "CLOSED" and rec["close_reason"] in ("opposite_signal", "reversal_signal")
        assert h.trade is None and h.position() == 0          # 숏을 바로 잡지 않았다
        sig = h.db.list_signals("paper")[0]
        assert sig["direction"] == -1 and sig["action"] in ("consumed_for_exit", "ignored_busy")
        # 다음 봉에서 '새' 약세 신호가 나오면 그때 대기 등록
        h.clock.t += 3600
        bars2 = bars_from(bear, 3600, t0=h.clock.t - 60 - n * 3600)
        await h.engine.on_bar_close(bars2, htf)
        assert h.trade is not None and h.trade.direction == -1 and h.trade.state == "ENTRY_PENDING"
    run(go())


def test_duplicate_ws_events_do_not_double_count(tmp_path):
    async def go():
        h = Harness(tmp_path)
        captured = []
        sink = h.engine.enqueue_user_event
        h.paper.set_event_sink(lambda e: (captured.append(e), sink(e)))
        t = await long_open(h)
        await h.tick(79150.0)
        rec1 = h.db.get_position(t.trade_id)
        n_fills = len(h.db.fills_for_trade(t.trade_id))
        for e in captured:                                   # 재연결 직후 전부 다시 옴
            h.engine.enqueue_user_event(e)
        await h.engine.drain_user_events()
        rec2 = h.db.get_position(t.trade_id)
        assert len(h.db.fills_for_trade(t.trade_id)) == n_fills
        assert rec2["accounting"]["net_pnl_btc"] == pytest.approx(rec1["accounting"]["net_pnl_btc"])
        assert rec2["state"] == "CLOSED" and h.position() == 0
    run(go())


def test_invalid_transition_is_blocked_and_logged(tmp_path):
    async def go():
        h = Harness(tmp_path)
        await h.start(80000.0)
        t = h.arm(1, 80100.0, 79200.0, TGT)
        with pytest.raises(InvalidTransition):
            h.engine.ctx.transition(t, "TP1", "잘못된 점프")
        assert t.state == "ENTRY_PENDING"
        assert h.db.risk_events("paper", "invalid_transition")
    run(go())


def test_partially_filled_exit_is_retried_until_flat(tmp_path):
    async def go():
        h = Harness(tmp_path, max_hold_bars=1)
        t = await long_open(h)
        h.paper.partial_next = 0.5                       # 첫 청산 주문은 절반만 체결
        h.clock.t += 3600
        await h.engine.on_bar_close(flat_bars(70, 80300.0, h.clock.t - 1),
                                    flat_bars(80, 80300.0, h.clock.t - 1, period=14400))
        assert h.position() == 0 and h.trade is None
        exits = [o for o in h.db.list_orders("paper", trade_id=t.trade_id) if o["purpose"] == "EXIT"]
        assert len(exits) == 2 and exits[0]["status"] == "EXPIRED"      # 잔량은 새 순번으로
        assert all(o["reduce_only"] for o in exits)
        assert h.db.get_position(t.trade_id)["close_reason"] == "time"
    run(go())


def test_tp_confirmed_even_when_fill_event_arrives_before_algo_update(tmp_path):
    async def go():
        h = Harness(tmp_path)
        t = await long_open(h)
        captured = []
        h.paper.set_event_sink(captured.append)
        h.clock.t += 1
        h.paper.update_market(last=81000.0, mark=81000.0, ts_ms=int(h.clock.t * 1000))   # TP1 발동
        fills = [e for e in captured if e["e"] == "ORDER_TRADE_UPDATE"]
        rest = [e for e in captured if e["e"] != "ORDER_TRADE_UPDATE"]
        for e in fills + rest:                           # 체결 이벤트가 먼저 도착
            h.engine.enqueue_user_event(e)
        await h.engine.drain_user_events()
        t = h.trade
        assert 0 in t.tp_filled and t.state == "TP1" and t.qty_open == "12"
        assert len(h.db.fills_for_trade(t.trade_id)) == 2        # 진입 + TP1, 중복 없음
    run(go())
