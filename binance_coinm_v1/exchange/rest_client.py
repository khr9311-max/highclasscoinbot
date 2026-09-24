"""
Binance COIN-M (dapi) REST 클라이언트.

- 서명: HMAC-SHA256(secret, 쿼리문자열). 헤더 X-MBX-APIKEY.
- 서버 시간 동기화: 오프셋을 재서 timestamp 에 더한다. -1021 이면 재동기화 후 1회 재시도
  (이 오류는 서버가 요청을 실행하지 않았다는 뜻이라 재시도해도 중복이 없다).
- 재시도: 조회(GET)만 자동 재시도한다. 주문 등 변경 요청(POST/PUT/DELETE)은 자동
  재시도하지 않는다. 결과를 모르는 오류(시간 초과·5xx·-1007)는 그대로 올려서 호출부가
  clientOrderId 조회로 확정하게 한다 - 중복 주문 방지의 핵심.
- mutation_guard: 서명된 변경 요청을 보내기 직전에 호출된다 (LiveOrderGate).
  가드가 예외를 내면 요청은 전송되지 않는다. listenKey(USER_STREAM)는 대상이 아니다.
- 로그·예외 메시지에는 경로와 거래소 오류 코드만 담는다 (서명·키 없음).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

from .errors import (BinanceAPIError, CODE_TIMESTAMP, NetworkError, RateLimited,
                     RequestTimeout, ServerError, UNKNOWN_STATE_CODES)

logger = logging.getLogger(__name__)

LIVE_REST = "https://dapi.binance.com"
TESTNET_REST = "https://testnet.binancefuture.com"
LIVE_WS = "wss://dstream.binance.com"
TESTNET_WS = "wss://dstream.binancefuture.com"


def endpoints(binance_env: str) -> Tuple[str, str]:
    if binance_env == "testnet":
        return TESTNET_REST, TESTNET_WS
    return LIVE_REST, LIVE_WS


@dataclass
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    text: str


class Transport:
    """HTTP 전송 인터페이스. 테스트는 가짜 구현을 넣는다."""

    async def request(self, method: str, url: str, headers: Dict[str, str],
                      timeout: float) -> HttpResponse:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class AiohttpTransport(Transport):
    def __init__(self):
        if os.environ.get("COINM_V1_TEST_MODE") == "1":
            raise RuntimeError("테스트 모드: 실제 HTTP 전송 객체 생성 금지")
        self._session = None

    async def _get(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def request(self, method: str, url: str, headers: Dict[str, str],
                      timeout: float) -> HttpResponse:
        import aiohttp
        session = await self._get()
        try:
            async with session.request(method, url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                text = await resp.text()
                return HttpResponse(resp.status, dict(resp.headers), text)
        except asyncio.TimeoutError:
            raise RequestTimeout(f"{method} {_path_of(url)} 시간 초과") from None
        except aiohttp.ClientConnectorError as e:
            # 연결 자체가 안 됨 - 요청이 서버에 도달하지 않았다
            raise NetworkError(f"{method} {_path_of(url)} 연결 실패: {type(e).__name__}",
                               maybe_sent=False) from None
        except aiohttp.ClientError as e:
            raise NetworkError(f"{method} {_path_of(url)} 통신 오류: {type(e).__name__}",
                               maybe_sent=True) from None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def _path_of(url: str) -> str:
    """로그용: 쿼리(서명 포함)를 뗀 경로."""
    p = url.split("?", 1)[0]
    for base in (LIVE_REST, TESTNET_REST):
        if p.startswith(base):
            return p[len(base):]
    return p


def fmt_param(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        s = format(v, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"
    if isinstance(v, float):
        return fmt_param(Decimal(repr(v)))
    return str(v)


class BinanceRestClient:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "",
                 transport: Optional[Transport] = None, recv_window: int = 5000,
                 clock: Callable[[], float] = time.time,
                 mutation_guard: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
                 timeout: float = 10.0, max_get_retries: int = 3,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8") if api_secret else b""
        self.transport = transport if transport is not None else AiohttpTransport()
        self.recv_window = int(recv_window)
        self.clock = clock
        self.mutation_guard = mutation_guard
        self.timeout = timeout
        self.max_get_retries = max_get_retries
        self.sleep = sleep
        self.time_offset_ms = 0
        self.last_time_sync: Optional[float] = None
        self.used_weight_1m = 0
        self.order_count_10s = 0
        self.order_count_1m = 0
        self.requests_sent = 0

    @property
    def has_keys(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def now_ms(self) -> int:
        return int(self.clock() * 1000) + self.time_offset_ms

    async def close(self) -> None:
        await self.transport.close()

    # ------------------------------------------------------------------
    async def sync_time(self) -> int:
        t0 = self.clock()
        data = await self.get_public("/dapi/v1/time")
        t1 = self.clock()
        server = int(data["serverTime"])
        local_mid = int((t0 + t1) / 2 * 1000)
        self.time_offset_ms = server - local_mid
        self.last_time_sync = t1
        if abs(self.time_offset_ms) > 1000:
            logger.warning("서버 시간 차이 %dms - 오프셋 보정", self.time_offset_ms)
        return server

    # ------------------------------------------------------------------
    def _headers(self, with_key: bool) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if with_key:
            h["X-MBX-APIKEY"] = self._api_key
        return h

    def _sign(self, query: str) -> str:
        return hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _track_limits(self, headers: Mapping[str, str]) -> None:
        low = {k.lower(): v for k, v in headers.items()}
        for key, attr in (("x-mbx-used-weight-1m", "used_weight_1m"),
                          ("x-mbx-order-count-10s", "order_count_10s"),
                          ("x-mbx-order-count-1m", "order_count_1m")):
            if key in low:
                try:
                    setattr(self, attr, int(low[key]))
                except ValueError:
                    pass

    async def _throttle(self) -> None:
        # UM·CM 공용 IP 한도 2400/분. 여유를 두고 2000 을 넘으면 다음 분까지 쉰다.
        if self.used_weight_1m >= 2000:
            wait = 60.0 - (self.clock() % 60.0) + 0.5
            logger.warning("요청 가중치 %d/2400 - %.1f초 대기", self.used_weight_1m, wait)
            await self.sleep(wait)
            self.used_weight_1m = 0

    def _parse(self, method: str, path: str, resp: HttpResponse) -> Any:
        self._track_limits(resp.headers)
        body: Any = None
        try:
            body = json.loads(resp.text) if resp.text else None
        except json.JSONDecodeError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("msg", "") if isinstance(body, dict) else (resp.text or "")[:200]
        if resp.status in (418, 429):
            ra = 60.0
            for k, v in resp.headers.items():
                if k.lower() == "retry-after":
                    try:
                        ra = float(v)
                    except ValueError:
                        pass
            raise RateLimited(f"{method} {path} HTTP {resp.status} {msg}", retry_after=ra,
                              banned=resp.status == 418)
        if resp.status >= 500:
            raise ServerError(resp.status, code, msg, path)
        if resp.status >= 400:
            if code in UNKNOWN_STATE_CODES:
                raise ServerError(resp.status, code, msg, path)
            raise BinanceAPIError(resp.status, code, msg, path)
        # 일부 엔드포인트는 200 + {"code": 200, "msg": "success"} 형태
        if isinstance(body, dict) and isinstance(code, int) and code < 0:
            raise BinanceAPIError(resp.status, code, msg, path)
        return body

    async def _send(self, method: str, url: str, headers: Dict[str, str]) -> HttpResponse:
        await self._throttle()
        self.requests_sent += 1
        return await self.transport.request(method, url, headers, self.timeout)

    # ------------------------------------------------------------------
    async def get_public(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        query = urlencode([(k, fmt_param(v)) for k, v in (params or {}).items() if v is not None])
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        return await self._with_get_retries("GET", path, url, self._headers(False))

    async def _with_get_retries(self, method: str, path: str, url: str,
                                headers: Dict[str, str]) -> Any:
        delay = 0.5
        last: Optional[Exception] = None
        for attempt in range(self.max_get_retries):
            try:
                return self._parse(method, path, await self._send(method, url, headers))
            except (NetworkError, ServerError) as e:
                last = e
                logger.warning("%s %s 재시도 %d/%d: %s", method, path, attempt + 1,
                               self.max_get_retries, e)
                await self.sleep(delay)
                delay = min(delay * 2, 5.0)
            except RateLimited:
                raise
        assert last is not None
        raise last

    async def signed(self, method: str, path: str,
                     params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.has_keys:
            raise BinanceAPIError(401, None, "API 키가 설정되지 않음", path)
        method = method.upper()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        mutating = method in ("POST", "PUT", "DELETE")
        if mutating and self.mutation_guard is not None:
            self.mutation_guard(method, path, params)      # 거부 시 예외 -> 전송 안 함
        for attempt in range(2):
            query = urlencode([(k, fmt_param(v)) for k, v in params.items()] +
                              [("recvWindow", str(self.recv_window)),
                               ("timestamp", str(self.now_ms()))])
            url = f"{self.base_url}{path}?{query}&signature={self._sign(query)}"
            headers = self._headers(True)
            try:
                if mutating:
                    return self._parse(method, path, await self._send(method, url, headers))
                return await self._with_get_retries(method, path, url, headers)
            except BinanceAPIError as e:
                if e.code == CODE_TIMESTAMP and attempt == 0:
                    logger.warning("타임스탬프 오류 - 서버 시간 재동기화 후 재시도 (%s)", path)
                    await self.sync_time()
                    continue
                raise
        raise AssertionError("unreachable")

    async def api_key_only(self, method: str, path: str,
                           params: Optional[Dict[str, Any]] = None) -> Any:
        """USER_STREAM (listenKey). 서명 없이 API 키 헤더만."""
        if not self._api_key:
            raise BinanceAPIError(401, None, "API 키가 설정되지 않음", path)
        query = urlencode([(k, fmt_param(v)) for k, v in (params or {}).items() if v is not None])
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        return self._parse(method, path, await self._send(method.upper(), url, self._headers(True)))
