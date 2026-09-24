"""6단계: 웹소켓 - 재연결, stale, 수명 교체, 중복, listenKey, 메시지 순서."""

import asyncio
import json
from decimal import Decimal

import pytest

from binance_coinm_v1.exchange.errors import BinanceAPIError
from binance_coinm_v1.exchange.websocket import (EventDeduper, KlineEvent, ListenKeyManager,
                                                 MarkPriceEvent, MarketStream, ResilientStream,
                                                 TradeEvent, UserDataStream, default_connect)
from binance_coinm_v1.execution.user_events import (AccountUpdate, AlgoUpdate, OrderTracker,
                                                    OrderUpdate, parse_user_event)

from .conftest import run


class FakeWS:
    def __init__(self, messages, then="block"):
        self.messages = list(messages)
        self.then = then
        self.closed = False

    async def recv(self):
        if self.messages:
            m = self.messages.pop(0)
            return m if isinstance(m, str) else json.dumps(m)
        if self.then == "close":
            raise ConnectionError("server closed")
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True


class Connector:
    def __init__(self, plan):
        self.plan = list(plan)
        self.urls = []

    async def __call__(self, url):
        self.urls.append(url)
        item = self.plan.pop(0) if self.plan else FakeWS([])
        if isinstance(item, Exception):
            raise item
        return item


async def run_until(stream_run, cond, timeout=3.0):
    stop = asyncio.Event()
    task = asyncio.create_task(stream_run(stop))
    t0 = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t0 > timeout:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


def fast(**kw):
    kw.setdefault("backoff_initial", 0.01)
    kw.setdefault("backoff_max", 0.02)
    kw.setdefault("poll_interval", 0.02)
    return kw


def test_real_ws_connect_forbidden_in_tests():
    with pytest.raises(RuntimeError):
        run(default_connect("wss://dstream.binance.com/ws/x"))


def test_reconnect_after_connection_failure():
    got = []
    conn = Connector([ConnectionRefusedError("down"), FakeWS([{"e": "x", "n": 1}, {"e": "x", "n": 2}])])

    async def url():
        return "wss://example/ws"

    s = ResilientStream("t", url, got.append, connect=conn, stale_after=None, **fast())
    run(run_until(s.run, lambda: len(got) >= 2))
    assert [g["n"] for g in got] == [1, 2]
    assert s.failures == 1 and s.connects == 1


def test_stale_connection_triggers_reconnect():
    got, reasons = [], []

    async def url():
        return "wss://example/ws"

    async def disc(reason):
        reasons.append(reason)

    conn = Connector([FakeWS([{"n": 1}]), FakeWS([{"n": 2}])])   # 첫 연결은 1건 후 침묵
    s = ResilientStream("t", url, got.append, connect=conn, stale_after=0.15,
                        on_disconnected=disc, **fast())
    run(run_until(s.run, lambda: len(got) >= 2))
    assert [g["n"] for g in got] == [1, 2]
    assert "stale" in reasons and s.reconnects >= 1


def test_closed_connection_reconnects_and_notifies():
    connected = []

    async def url():
        return "wss://example/ws"

    async def on_conn(n):
        connected.append(n)

    conn = Connector([FakeWS([{"n": 1}], then="close"), FakeWS([{"n": 2}])])
    got = []
    s = ResilientStream("t", url, got.append, connect=conn, stale_after=None,
                        on_connected=on_conn, **fast())
    run(run_until(s.run, lambda: len(got) >= 2))
    assert connected[:2] == [1, 2]


def test_lifetime_rotation():
    reasons = []

    async def url():
        return "wss://example/ws"

    async def disc(r):
        reasons.append(r)

    conn = Connector([FakeWS([]), FakeWS([])])
    s = ResilientStream("t", url, lambda d: None, connect=conn, stale_after=None, max_lifetime=0.1,
                        on_disconnected=disc, **fast())
    run(run_until(s.run, lambda: "lifetime" in reasons))
    assert "lifetime" in reasons


def test_handler_exception_does_not_kill_stream():
    got = []

    def handler(d):
        if d.get("boom"):
            raise ValueError("bad")
        got.append(d)

    async def url():
        return "wss://example/ws"

    s = ResilientStream("t", url, handler, connect=Connector([FakeWS([{"boom": 1}, {"ok": 1}])]),
                        stale_after=None, **fast())
    run(run_until(s.run, lambda: len(got) >= 1))
    assert got == [{"ok": 1}] and s.handler_errors == 1


def test_market_stream_parses_and_drops_out_of_order():
    evs = []
    msgs = [
        {"stream": "btcusd_perp@markPrice@1s", "data": {"e": "markPriceUpdate", "E": 2000,
                                                       "s": "BTCUSD_PERP", "p": "84000.1",
                                                       "i": "84010.0", "r": "0.0001", "T": 9}},
        {"stream": "btcusd_perp@markPrice@1s", "data": {"e": "markPriceUpdate", "E": 1000,
                                                       "s": "BTCUSD_PERP", "p": "83000.0"}},
        {"stream": "btcusd_perp@aggTrade", "data": {"e": "aggTrade", "E": 5, "a": 10, "p": "84001",
                                                   "q": "3", "T": 5}},
        {"stream": "btcusd_perp@aggTrade", "data": {"e": "aggTrade", "E": 5, "a": 10, "p": "84001",
                                                   "q": "3", "T": 5}},
        {"stream": "btcusd_perp@kline_1h", "data": {"e": "kline", "E": 7, "s": "BTCUSD_PERP", "k": {
            "t": 0, "T": 3599999, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "9", "x": True}}},
    ]
    ms = MarketStream("wss://dstream.binance.com", "BTCUSD_PERP", "1h", evs.append,
                      connect=Connector([FakeWS(msgs)]), **fast())
    assert "btcusd_perp@markPrice@1s" in ms.url and "btcusd_perp@kline_1h" in ms.url
    run(run_until(ms.run, lambda: len(evs) >= 3))
    assert isinstance(evs[0], MarkPriceEvent) and evs[0].index_price == 84010.0
    assert isinstance(evs[1], TradeEvent)
    assert isinstance(evs[2], KlineEvent) and evs[2].closed
    assert ms.dropped_out_of_order == 2                # 오래된 마크가 1 + 중복 체결 1


# ---------------------------------------------------------------- 사용자 스트림
class FakeKeyGateway:
    def __init__(self):
        self.created = 0
        self.keepalive_fail = False

    async def create_listen_key(self):
        self.created += 1
        return f"key{self.created:030d}"

    async def keepalive_listen_key(self):
        if self.keepalive_fail:
            raise BinanceAPIError(400, -1125, "This listenKey does not exist.")


ORDER_EVT = {"e": "ORDER_TRADE_UPDATE", "E": 10, "T": 10, "o": {
    "s": "BTCUSD_PERP", "c": "cm1a-EN0", "S": "BUY", "o": "MARKET", "q": "3", "x": "TRADE",
    "X": "FILLED", "i": 55, "l": "3", "z": "3", "L": "84000", "ap": "84000", "n": "0.0000018",
    "N": "BTC", "t": 777, "rp": "0", "R": False, "m": False, "wt": "CONTRACT_PRICE", "cp": False}}


def test_user_stream_dedup_and_listen_key_expiry_reconnect():
    kg = FakeKeyGateway()
    lk = ListenKeyManager(kg)
    events, reconciles = [], []

    async def on_rec(why):
        reconciles.append(why)

    conn = Connector([FakeWS([ORDER_EVT, ORDER_EVT, {"e": "listenKeyExpired", "E": 11}]),
                      FakeWS([dict(ORDER_EVT, E=12, o=dict(ORDER_EVT["o"], t=778, z="3"))])])
    us = UserDataStream("wss://dstream.binance.com", lk, events.append, on_reconnected=on_rec,
                        connect=conn, **fast())
    run(run_until(us.run, lambda: len(events) >= 2))
    assert len(events) == 2                                    # 중복 1건 제거
    assert us.deduper.duplicates == 1 and us.expired_events == 1
    assert kg.created == 2 and conn.urls[0] != conn.urls[1]    # 새 listenKey 로 재연결
    assert reconciles[:2] == ["connected", "reconnected"]      # 재연결마다 REST 대사 요청


def test_listen_key_keepalive_failure_invalidates():
    kg = FakeKeyGateway()
    lk = ListenKeyManager(kg, keepalive_interval=0)
    run(lk.ensure())
    assert run(lk.keepalive()) is True
    kg.keepalive_fail = True
    assert run(lk.keepalive()) is False and lk.key is None
    run(lk.ensure())
    assert kg.created == 2


# ---------------------------------------------------------------- 파싱·순서
def test_parse_user_events():
    u = parse_user_event(ORDER_EVT)
    assert isinstance(u, OrderUpdate) and u.is_trade and u.cum_qty == Decimal(3)
    a = parse_user_event({"e": "ALGO_UPDATE", "T": 1, "E": 1, "o": {
        "caid": "cm1a-SL0", "aid": 9, "o": "STOP_MARKET", "s": "BTCUSD_PERP", "S": "SELL",
        "X": "TRIGGERED", "ai": "991", "ap": "0", "aq": "0", "tp": "82000", "cp": True}})
    assert isinstance(a, AlgoUpdate) and a.actual_order_id == "991" and a.close_position
    acc = parse_user_event({"e": "ACCOUNT_UPDATE", "E": 3, "T": 3, "a": {
        "m": "FUNDING_FEE", "S": "BTCUSD_PERP", "B": [{"a": "BTC", "wb": "0.007", "cw": "0.006", "bc": "0"}],
        "P": [{"s": "BTCUSD_PERP", "pa": "-3", "ep": "84000", "up": "0.00001", "mt": "isolated",
               "iw": "0.0012", "ps": "BOTH"}]}})
    assert isinstance(acc, AccountUpdate) and acc.reason == "FUNDING_FEE"
    assert acc.positions[0].position_amt == Decimal(-3) and acc.symbol == "BTCUSD_PERP"


def _ou(status, z, t, trade=None, x=None):
    o = dict(ORDER_EVT["o"], X=status, z=str(z), x=x or ("TRADE" if trade else "NEW"),
             l=str(1 if trade else 0), t=trade or 0)
    return parse_user_event({"e": "ORDER_TRADE_UPDATE", "E": t, "T": t, "o": o})


def test_order_tracker_rejects_out_of_order_and_duplicate():
    tr = OrderTracker()
    assert tr.apply_order(_ou("NEW", 0, 1)) == (True, False)
    assert tr.apply_order(_ou("FILLED", 3, 5, trade=2)) == (True, True)
    # 늦게 도착한 부분체결(이전 체결) - 체결은 새 것이지만 상태는 되돌리지 않는다
    acc, fill = tr.apply_order(_ou("PARTIALLY_FILLED", 1, 3, trade=1))
    assert acc is False and fill is True
    assert tr.status_of("cm1a-EN0") == "FILLED"
    # 같은 체결 중복
    assert tr.apply_order(_ou("FILLED", 3, 5, trade=2)) == (False, False)
    assert tr.dup_trades == 1


def test_algo_tracker_monotonic_and_links_actual_order():
    tr = OrderTracker()
    mk = lambda st, ai=None: parse_user_event({"e": "ALGO_UPDATE", "T": 1, "E": 1, "o": {
        "caid": "cm1a-SL0", "aid": 9, "o": "STOP_MARKET", "s": "BTCUSD_PERP", "S": "SELL",
        "X": st, "ai": ai or "", "aq": "0"}})
    assert tr.apply_algo(mk("NEW"))
    assert tr.apply_algo(mk("FINISHED", "991"))
    assert not tr.apply_algo(mk("TRIGGERED", "991"))           # 역순 도착 무시
    assert tr.algo_for_order("991") == "cm1a-SL0"


def test_event_deduper_bounded():
    d = EventDeduper(maxlen=3)
    evs = [{"e": "X", "E": i} for i in range(5)]
    assert not any(d.seen(e) for e in evs)
    assert d.seen(evs[-1]) and not d.seen(evs[0])              # 오래된 키는 밀려남
