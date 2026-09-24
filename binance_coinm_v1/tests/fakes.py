"""테스트용 가짜 HTTP 전송. 실제 네트워크 없이 Binance 응답을 흉내낸다."""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from binance_coinm_v1.exchange.rest_client import HttpResponse, Transport


class FakeTransport(Transport):
    """
    routes[(METHOD, path)] = 핸들러. 핸들러는 (params: dict) -> 응답.
    응답: dict/list (200 JSON), HttpResponse, 또는 예외 인스턴스(raise).
    같은 키에 list 를 주면 호출마다 차례로 소비한다.
    """

    def __init__(self):
        self.routes: Dict[Tuple[str, str], Any] = {}
        self.calls: List[Dict[str, Any]] = []

    def add(self, method: str, path: str, handler: Any) -> "FakeTransport":
        self.routes[(method.upper(), path)] = handler
        return self

    async def request(self, method, url, headers, timeout):
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.calls.append({"method": method, "path": parts.path, "params": params,
                           "headers": dict(headers), "url": url})
        key = (method.upper(), parts.path)
        if key not in self.routes:
            return HttpResponse(404, {}, json.dumps({"code": -5000, "msg": f"no route {key}"}))
        h = self.routes[key]
        if isinstance(h, list):
            if not h:
                raise AssertionError(f"응답 소진: {key}")
            h = h.pop(0)
        res = h(params) if callable(h) else h
        if isinstance(res, BaseException):
            raise res
        if isinstance(res, HttpResponse):
            return res
        return HttpResponse(200, {"x-mbx-used-weight-1m": "5"}, json.dumps(res))

    def calls_to(self, method: str, path: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method.upper() and c["path"] == path]


def err(status: int, code: int, msg: str = "err") -> HttpResponse:
    return HttpResponse(status, {}, json.dumps({"code": code, "msg": msg}))


async def no_sleep(_s: float) -> None:
    return None
