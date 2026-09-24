"""
웹소켓: 시장 스트림 + 사용자 데이터 스트림.

공통 (ResilientStream):
  - 끊기면 지수 백오프(+지터)로 재연결
  - stale 감지: stale_after 초 동안 메시지가 없으면 끊고 다시 붙는다 (시장 스트림은
    markPrice@1s 가 매초 오므로 30초 무소식 = 죽은 연결)
  - 바이낸스는 연결을 24시간 뒤 끊는다 -> 23시간에 스스로 갈아탄다
  - 핸들러 예외는 기록만 하고 스트림은 계속 돈다
  - 재연결되면 on_connected(n) 을 부른다 -> 사용자 스트림은 이때 REST 대사(reconcile)를
    요청한다 (끊겨 있던 동안의 이벤트를 놓쳤을 수 있으므로)

사용자 데이터 스트림:
  - listenKey 생성(POST) -> 30분마다 연장(PUT). 연장 실패/listenKeyExpired 이벤트면
    새로 만들고 다시 붙는다.
  - 중복 메시지는 EventDeduper 가 거른다. 주문 상태의 역순 도착은
    execution/user_events.OrderTracker 가 단조 규칙으로 거른다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set, Tuple

from .errors import BinanceAPIError, CODE_LISTEN_KEY_NOT_EXIST

logger = logging.getLogger(__name__)

Handler = Callable[[Dict[str, Any]], Any]


class WSConnection:
    async def recv(self) -> str:
        raise NotImplementedError

    async def send(self, data: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


async def default_connect(url: str) -> Any:
    if os.environ.get("COINM_V1_TEST_MODE") == "1":
        raise RuntimeError("테스트 모드: 실제 웹소켓 연결 금지")
    from websockets.asyncio.client import connect
    return await connect(url, ping_interval=20, ping_timeout=20, close_timeout=5,
                         max_size=2 ** 22, open_timeout=10)


class _Rotate(Exception):
    pass


class ResilientStream:
    def __init__(self, name: str, url_provider: Callable[[], Awaitable[str]], on_message: Handler,
                 connect: Callable[[str], Awaitable[Any]] = default_connect,
                 stale_after: Optional[float] = 30.0, max_lifetime: float = 23 * 3600,
                 backoff_initial: float = 1.0, backoff_max: float = 60.0,
                 on_connected: Optional[Callable[[int], Awaitable[None]]] = None,
                 on_disconnected: Optional[Callable[[str], Awaitable[None]]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 poll_interval: float = 5.0):
        self.name = name
        self.poll_interval = poll_interval
        self.url_provider = url_provider
        self.on_message = on_message
        self.connect = connect
        self.stale_after = stale_after
        self.max_lifetime = max_lifetime
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.clock = clock
        self.sleep = sleep
        self.state = "disconnected"
        self.connects = 0
        self.failures = 0
        self.messages = 0
        self.handler_errors = 0
        self.last_message_at: Optional[float] = None
        self.last_disconnect_reason = ""
        self._ws: Any = None
        self._reconnect_requested = False

    @property
    def reconnects(self) -> int:
        return max(0, self.connects - 1)

    def is_fresh(self, max_age: float) -> bool:
        return (self.state == "connected" and self.last_message_at is not None
                and self.clock() - self.last_message_at <= max_age)

    def request_reconnect(self) -> None:
        self._reconnect_requested = True

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self.backoff_initial
        while not stop.is_set():
            self.state = "connecting"
            try:
                url = await self.url_provider()
                ws = await self.connect(url)
            except Exception as e:
                self.failures += 1
                self.state = "disconnected"
                self.last_disconnect_reason = f"연결 실패: {type(e).__name__}"
                logger.warning("[%s] 연결 실패 (%s) - %.1f초 뒤 재시도", self.name, type(e).__name__, backoff)
                await self._notify_disconnect(self.last_disconnect_reason)
                await self._sleep_or_stop(stop, backoff * (0.8 + 0.4 * random.random()))
                backoff = min(backoff * 2, self.backoff_max)
                continue
            self._ws = ws
            self.connects += 1
            self.state = "connected"
            self._reconnect_requested = False
            self.last_message_at = self.clock()
            opened = self.clock()
            backoff = self.backoff_initial
            if self.connects > 1:
                logger.info("[%s] 재연결 성공 (%d회째)", self.name, self.connects)
            if self.on_connected:
                try:
                    await self.on_connected(self.connects)
                except Exception:
                    logger.exception("[%s] on_connected 처리 실패", self.name)
            reason = "closed"
            try:
                while not stop.is_set():
                    if self._reconnect_requested:
                        reason = "reconnect_requested"
                        break
                    left = self.max_lifetime - (self.clock() - opened)
                    if left <= 0:
                        raise _Rotate()
                    # 짧게 끊어 기다리며 재연결 요청·stale·수명을 주기적으로 확인한다.
                    # (websockets 의 recv() 는 취소해도 메시지를 잃지 않는다)
                    wait = min(self.poll_interval, left)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                    except asyncio.TimeoutError:
                        if self.stale_after is not None and \
                                self.clock() - (self.last_message_at or opened) >= self.stale_after:
                            reason = "stale"
                            logger.warning("[%s] %.0f초 무소식 - 재연결", self.name, self.stale_after)
                            break
                        continue
                    self.last_message_at = self.clock()
                    self.messages += 1
                    await self._dispatch(raw)
            except _Rotate:
                reason = "lifetime"
                logger.info("[%s] 연결 수명 도달 - 선제 재연결", self.name)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = f"끊김: {type(e).__name__}"
                logger.warning("[%s] %s", self.name, reason)
            finally:
                self.state = "disconnected"
                self._ws = None
                try:
                    await ws.close()
                except Exception:
                    pass
            self.last_disconnect_reason = reason
            await self._notify_disconnect(reason)
            if not stop.is_set() and reason not in ("lifetime", "reconnect_requested"):
                await self._sleep_or_stop(stop, backoff * (0.8 + 0.4 * random.random()))
                backoff = min(backoff * 2, self.backoff_max)

    async def _dispatch(self, raw: Any) -> None:
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except json.JSONDecodeError:
            self.handler_errors += 1
            return
        if isinstance(data, dict) and "stream" in data and "data" in data:
            data = data["data"]                               # 결합 스트림 포장 벗기기
        try:
            res = self.on_message(data)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            self.handler_errors += 1
            logger.exception("[%s] 메시지 처리 실패", self.name)

    async def _notify_disconnect(self, reason: str) -> None:
        if self.on_disconnected:
            try:
                await self.on_disconnected(reason)
            except Exception:
                logger.exception("[%s] on_disconnected 처리 실패", self.name)

    async def _sleep_or_stop(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# 시장 스트림
# ---------------------------------------------------------------------------
@dataclass
class KlineEvent:
    event_time_ms: int
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool


@dataclass
class MarkPriceEvent:
    event_time_ms: int
    mark_price: float
    index_price: Optional[float]
    funding_rate: Optional[float]
    next_funding_ms: Optional[int]


@dataclass
class TradeEvent:
    event_time_ms: int
    trade_time_ms: int
    price: float
    qty: float
    agg_id: int


def parse_market_event(d: Dict[str, Any]) -> Optional[Any]:
    e = d.get("e")
    if e == "kline":
        k = d["k"]
        return KlineEvent(int(d.get("E", 0)), int(k["t"]), int(k["T"]), float(k["o"]), float(k["h"]),
                          float(k["l"]), float(k["c"]), float(k.get("v", 0)), bool(k.get("x")))
    if e == "markPriceUpdate":
        def f(key):
            v = d.get(key)
            return float(v) if v not in (None, "") else None
        nf = d.get("T")
        return MarkPriceEvent(int(d.get("E", 0)), float(d["p"]), f("i"), f("r"),
                              int(nf) if nf not in (None, "", 0) else None)
    if e == "aggTrade":
        return TradeEvent(int(d.get("E", 0)), int(d.get("T", 0)), float(d["p"]), float(d.get("q", 0)),
                          int(d.get("a", 0)))
    return None


def market_stream_url(ws_base: str, symbol: str, interval: str) -> str:
    s = symbol.lower()
    streams = [f"{s}@kline_{interval}", f"{s}@markPrice@1s", f"{s}@aggTrade"]
    return f"{ws_base}/stream?streams=" + "/".join(streams)


class MarketStream:
    """시장 스트림. 오래된(역순) 시세는 버린다 - 가격 종류별 마지막 이벤트 시각 기준."""

    def __init__(self, ws_base: str, symbol: str, interval: str,
                 on_event: Callable[[Any], Any], connect=default_connect,
                 stale_after: float = 30.0, **kw):
        self.url = market_stream_url(ws_base, symbol, interval)
        self.on_event = on_event
        self._last: Dict[str, int] = {}
        self._last_agg_id = -1
        self.dropped_out_of_order = 0

        async def url_provider() -> str:
            return self.url

        self.stream = ResilientStream("market", url_provider, self._handle, connect=connect,
                                      stale_after=stale_after, **kw)

    async def _handle(self, d: Dict[str, Any]) -> None:
        ev = parse_market_event(d)
        if ev is None:
            return
        if isinstance(ev, TradeEvent):
            if ev.agg_id and ev.agg_id <= self._last_agg_id:
                self.dropped_out_of_order += 1
                return
            self._last_agg_id = max(self._last_agg_id, ev.agg_id)
        elif isinstance(ev, MarkPriceEvent):
            if ev.event_time_ms < self._last.get("mark", -1):
                self.dropped_out_of_order += 1
                return
            self._last["mark"] = ev.event_time_ms
        res = self.on_event(ev)
        if asyncio.iscoroutine(res):
            await res

    async def run(self, stop: asyncio.Event) -> None:
        await self.stream.run(stop)


# ---------------------------------------------------------------------------
# 사용자 데이터 스트림
# ---------------------------------------------------------------------------
class EventDeduper:
    """같은 메시지가 두 번 오면(재연결 직후 등) 한 번만 통과시킨다."""

    def __init__(self, maxlen: int = 5000):
        self.maxlen = maxlen
        self._order: Deque[Tuple] = deque()
        self._set: Set[Tuple] = set()
        self.duplicates = 0

    @staticmethod
    def key(evt: Dict[str, Any]) -> Tuple:
        e = evt.get("e")
        if e == "ORDER_TRADE_UPDATE":
            o = evt.get("o", {})
            return (e, o.get("i"), o.get("c"), o.get("x"), o.get("X"), o.get("z"), o.get("t"), o.get("T"))
        if e == "ALGO_UPDATE":
            o = evt.get("o", {})
            return (e, o.get("aid"), o.get("caid"), o.get("X"), o.get("aq"), o.get("ai"), evt.get("T"))
        return (e, evt.get("E"), evt.get("T"), json.dumps(evt, sort_keys=True, default=str))

    def seen(self, evt: Dict[str, Any]) -> bool:
        k = self.key(evt)
        if k in self._set:
            self.duplicates += 1
            return True
        self._set.add(k)
        self._order.append(k)
        if len(self._order) > self.maxlen:
            self._set.discard(self._order.popleft())
        return False


class ListenKeyManager:
    """listenKey 생성·연장·재생성. 키 값은 로그에 남기지 않는다."""

    def __init__(self, gateway: Any, keepalive_interval: float = 1800.0,
                 clock: Callable[[], float] = time.monotonic):
        self.gateway = gateway
        self.keepalive_interval = keepalive_interval
        self.clock = clock
        self.key: Optional[str] = None
        self.created = 0
        self.last_keepalive: Optional[float] = None

    async def ensure(self) -> str:
        if self.key is None:
            self.key = await self.gateway.create_listen_key()
            self.created += 1
            self.last_keepalive = self.clock()
            logger.info("listenKey 생성 (%d회째)", self.created)
        return self.key

    def invalidate(self) -> None:
        self.key = None

    def due(self) -> bool:
        return self.key is not None and (self.last_keepalive is None or
                                         self.clock() - self.last_keepalive >= self.keepalive_interval)

    async def keepalive(self) -> bool:
        """연장 성공이면 True. 키가 없어졌으면 무효화하고 False (호출부가 재연결)."""
        if self.key is None:
            return False
        try:
            await self.gateway.keepalive_listen_key()
            self.last_keepalive = self.clock()
            return True
        except BinanceAPIError as e:
            if e.code == CODE_LISTEN_KEY_NOT_EXIST:
                logger.warning("listenKey 소멸 - 재생성 예정")
                self.invalidate()
                return False
            raise


class UserDataStream:
    def __init__(self, ws_base: str, listen_keys: ListenKeyManager,
                 on_event: Callable[[Dict[str, Any]], Any],
                 on_reconnected: Optional[Callable[[str], Awaitable[None]]] = None,
                 connect=default_connect, **kw):
        self.ws_base = ws_base
        self.listen_keys = listen_keys
        self.on_event = on_event
        self.on_reconnected = on_reconnected
        self.deduper = EventDeduper()
        self.expired_events = 0

        async def url_provider() -> str:
            return f"{self.ws_base}/ws/{await self.listen_keys.ensure()}"

        async def connected(n: int) -> None:
            # 첫 연결이든 재연결이든: 끊겨 있던 사이의 이벤트는 REST 로 대사해야 한다
            if self.on_reconnected:
                await self.on_reconnected("connected" if n == 1 else "reconnected")

        kw.setdefault("stale_after", None)       # 사용자 이벤트는 드물다 - 무소식은 정상
        self.stream = ResilientStream("user", url_provider, self._handle, connect=connect,
                                      on_connected=connected, **kw)

    async def _handle(self, d: Dict[str, Any]) -> None:
        if d.get("e") == "listenKeyExpired":
            self.expired_events += 1
            logger.warning("listenKeyExpired 수신 - 새 키로 재연결")
            self.listen_keys.invalidate()
            self.stream.request_reconnect()
            return
        if self.deduper.seen(d):
            return
        res = self.on_event(d)
        if asyncio.iscoroutine(res):
            await res

    async def keepalive_loop(self, stop: asyncio.Event, check_every: float = 60.0) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=check_every)
                return
            except asyncio.TimeoutError:
                pass
            if self.listen_keys.due():
                try:
                    ok = await self.listen_keys.keepalive()
                except Exception as e:
                    logger.warning("listenKey 연장 실패: %s", type(e).__name__)
                    ok = True        # 일시 오류 - 다음 주기에 다시
                if not ok:
                    self.stream.request_reconnect()

    async def run(self, stop: asyncio.Event) -> None:
        await self.stream.run(stop)
