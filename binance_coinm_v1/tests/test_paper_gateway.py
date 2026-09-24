"""종이 게이트웨이가 COIN-M 격리·원웨이 계정을 제대로 흉내내는지."""

from decimal import Decimal

import pytest

from binance_coinm_v1.config import Settings
from binance_coinm_v1.exchange import BinanceAPIError, OrderStatusUnknown
from binance_coinm_v1.exchange.errors import RequestTimeout
from binance_coinm_v1.exchange.models import OrderRequest
from binance_coinm_v1.exchange.paper_gateway import Fault, PaperGateway
from binance_coinm_v1.risk import inverse_math as im

from .conftest import run
from .helpers import load_spec

SPEC = load_spec()


def paper(**kw):
    s = Settings.build(slippage_bps=0, stop_slippage_bps=0, **kw)
    g = PaperGateway(SPEC, s, clock=lambda: 1_790_000_000.0, start_equity_btc=0.01)
    events = []
    g.set_event_sink(events.append)
    g.update_market(last=80000.0, mark=80000.0, index=80000.0, ts_ms=1_790_000_000_000)
    return g, events


def mkt(cid, side, qty, reduce_only=False):
    return OrderRequest(cid, "BTCUSD_PERP", side, "MARKET", quantity=Decimal(qty),
                        reduce_only=reduce_only)


def stop(cid, side, trig, wt="MARK_PRICE"):
    return OrderRequest(cid, "BTCUSD_PERP", side, "STOP_MARKET", trigger_price=Decimal(str(trig)),
                        close_position=True, working_type=wt)


def test_market_long_position_margin_fee_in_btc():
    g, ev = paper()
    st = run(g.place_order(mkt("a-EN0", "BUY", 4)))
    assert st.status == "FILLED" and st.avg_price is None          # 응답에는 avgPrice 없음
    q = run(g.get_order("BTCUSD_PERP", "a-EN0"))
    assert q.avg_price == pytest.approx(80000.0) and q.executed_qty == Decimal(4)
    pos = run(g.get_position("BTCUSD_PERP"))
    assert pos.position_amt == Decimal(4) and pos.entry_price == 80000.0
    notional = 4 * 100 / 80000.0                                     # 0.005 BTC (contractSize=100)
    assert pos.isolated_margin_btc == pytest.approx(notional / 3)
    fee = notional * 0.0005
    acc = run(g.get_account())
    assert acc.asset("BTC").wallet_balance == pytest.approx(0.01 - fee)
    kinds = [(e["e"], e.get("o", {}).get("x")) for e in ev]
    assert ("ORDER_TRADE_UPDATE", "NEW") in kinds and ("ORDER_TRADE_UPDATE", "TRADE") in kinds
    assert any(e["e"] == "ACCOUNT_UPDATE" for e in ev)


def test_inverse_pnl_on_close():
    g, _ = paper()
    run(g.place_order(mkt("a-EN0", "SELL", 3)))                    # 숏 3계약 @80000
    g.update_market(last=76000.0, mark=76000.0)
    run(g.place_order(mkt("a-EX0", "BUY", 3, reduce_only=True)))
    fills = run(g.get_user_trades("BTCUSD_PERP"))
    exp = im.pnl_btc(-1, 3, 100, 80000.0, 76000.0)
    assert fills[-1].realized_pnl_btc == pytest.approx(exp)
    assert exp > 0
    assert run(g.get_position("BTCUSD_PERP")).position_amt == 0


def test_reduce_only_cannot_open_or_flip():
    g, _ = paper()
    with pytest.raises(BinanceAPIError) as ei:
        run(g.place_order(mkt("x", "SELL", 1, reduce_only=True)))   # 포지션 없음
    assert ei.value.code == -2022
    run(g.place_order(mkt("a-EN0", "BUY", 2)))
    with pytest.raises(BinanceAPIError):
        run(g.place_order(mkt("y", "BUY", 1, reduce_only=True)))     # 같은 방향 = 늘리기
    run(g.place_order(mkt("z", "SELL", 5, reduce_only=True)))        # 과다 수량 -> 포지션만큼만
    assert run(g.get_position("BTCUSD_PERP")).position_amt == 0


def test_stop_triggers_on_mark_not_contract_price():
    g, ev = paper()
    run(g.place_order(mkt("a-EN0", "BUY", 3)))
    run(g.place_order(stop("a-SL0", "SELL", 79000.0, "MARK_PRICE")))
    g.update_market(last=78900.0, mark=79100.0)                      # 체결가만 뚫음
    assert run(g.get_position("BTCUSD_PERP")).position_amt == Decimal(3)
    g.update_market(last=78950.0, mark=78990.0)                      # 마크가 도달
    assert run(g.get_position("BTCUSD_PERP")).position_amt == 0
    a = run(g.get_algo_order("BTCUSD_PERP", "a-SL0"))
    assert a.status == "FINISHED" and a.actual_order_id is not None
    assert [e["o"]["X"] for e in ev if e["e"] == "ALGO_UPDATE"] == ["NEW", "TRIGGERED", "FINISHED"]


def test_immediate_trigger_rejected():
    g, _ = paper()
    run(g.place_order(mkt("a-EN0", "BUY", 3)))
    with pytest.raises(BinanceAPIError) as ei:
        run(g.place_order(stop("a-SL0", "SELL", 80500.0)))           # 손절이 현재가 위
    assert ei.value.code == -2021


def test_take_profit_partial_then_close_position_stop_closes_rest():
    g, _ = paper()
    run(g.place_order(mkt("a-EN0", "BUY", 4)))
    run(g.place_order(stop("a-SL0", "SELL", 79000.0)))
    run(g.place_order(OrderRequest("a-TP1", "BTCUSD_PERP", "SELL", "TAKE_PROFIT_MARKET",
                                   quantity=Decimal(1), reduce_only=True,
                                   trigger_price=Decimal("81000"), working_type="CONTRACT_PRICE")))
    g.update_market(last=81000.0, mark=81000.0)
    assert run(g.get_position("BTCUSD_PERP")).position_amt == Decimal(3)
    g.update_market(last=78000.0, mark=78000.0)
    assert run(g.get_position("BTCUSD_PERP")).position_amt == 0     # 남은 3계약 전부 청산


def test_close_position_stop_with_no_position_does_nothing():
    g, _ = paper()
    run(g.place_order(stop("a-SL0", "SELL", 79000.0)))
    g.update_market(last=78000.0, mark=78000.0)
    assert run(g.get_position("BTCUSD_PERP")).position_amt == 0     # 새 포지션을 만들지 않음
    assert run(g.get_algo_order("BTCUSD_PERP", "a-SL0")).status == "FINISHED"


def test_funding_paid_by_long_when_rate_positive():
    g, _ = paper()
    g.update_market(ts_ms=1_790_000_000_000, funding_rate=0.0001, next_funding_ms=1_790_000_600_000)
    run(g.place_order(mkt("a-EN0", "BUY", 4)))
    w0 = run(g.get_account()).asset("BTC").wallet_balance
    g.update_market(ts_ms=1_790_000_600_001, mark=80000.0)
    w1 = run(g.get_account()).asset("BTC").wallet_balance
    assert w1 - w0 == pytest.approx(-4 * 100 / 80000.0 * 0.0001)
    inc = run(g.get_income("BTCUSD_PERP", "FUNDING_FEE"))
    assert len(inc) == 1 and float(inc[0]["income"]) < 0


def test_partial_fill_injection_and_lost_response():
    g, _ = paper()
    g.partial_next = 0.5
    run(g.place_order(mkt("a-EN0", "BUY", 4)))
    q = run(g.get_order("BTCUSD_PERP", "a-EN0"))
    assert q.status == "EXPIRED" and q.executed_qty == Decimal(2)
    g.inject(Fault("place", RequestTimeout("lost"), when="after", match="a-EX0"))
    with pytest.raises(OrderStatusUnknown):
        run(g.place_order(mkt("a-EX0", "SELL", 2, reduce_only=True)))
    # 응답은 잃었지만 실제로는 실행됐다
    assert run(g.get_order("BTCUSD_PERP", "a-EX0")).status == "FILLED"
    assert run(g.get_position("BTCUSD_PERP")).position_amt == 0


def test_margin_insufficient_and_liquidation():
    g, _ = paper()
    with pytest.raises(BinanceAPIError) as ei:
        run(g.place_order(mkt("big", "BUY", 30)))                    # 증거금 부족
    assert ei.value.code == -2019
    run(g.place_order(mkt("a-EN0", "BUY", 4)))
    lp = run(g.get_position("BTCUSD_PERP")).liquidation_price
    assert 55000 < lp < 65000                                        # 3배 롱 청산가 약 -23%
    g.update_market(last=lp - 10, mark=lp - 10)
    assert run(g.get_position("BTCUSD_PERP")).position_amt == 0


def test_state_roundtrip():
    g, _ = paper()
    run(g.place_order(mkt("a-EN0", "BUY", 3)))
    run(g.place_order(stop("a-SL0", "SELL", 79000.0)))
    st = g.to_state()
    g2, _ = paper()
    g2.load_state(st)
    assert run(g2.get_position("BTCUSD_PERP")).position_amt == Decimal(3)
    assert run(g2.get_algo_order("BTCUSD_PERP", "a-SL0")).status == "NEW"
