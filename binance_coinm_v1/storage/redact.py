"""
비밀값 가리기. 로그·DB·텔레그램으로 나가는 모든 문자열/객체를 한 번 통과시킨다.

가리는 것:
  - 설정에 등록된 비밀값 문자열 자체 (API 키, 시크릿, 텔레그램 토큰)
  - 서명 쿼리 (signature=...)와 API 키 헤더 값
  - 객체의 민감 키 (signature, apiKey, secret, listenKey ...)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

MASK = "***"

SENSITIVE_KEYS = {
    "signature", "apikey", "api_key", "apisecret", "api_secret", "secret", "secretkey",
    "x-mbx-apikey", "listenkey", "listen_key", "token", "telegram_token", "bot_token",
    "binance_api_key", "binance_api_secret",
}

_SIG_RE = re.compile(r"(signature=)[0-9a-fA-F]+")
_HDR_RE = re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]+", re.IGNORECASE)
_TG_RE = re.compile(r"(api\.telegram\.org/bot)[^/\s]+")
_LK_RE = re.compile(r"(listenKey['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]{8,}", re.IGNORECASE)
_WS_LK_RE = re.compile(r"(/ws/)[A-Za-z0-9]{20,}")


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets: list = []
        self.add(*secrets)

    def add(self, *secrets: str) -> None:
        # 너무 짧은 값은 일반 단어를 망가뜨릴 수 있어 제외 (실제 키는 수십 자)
        s = set(self._secrets) | {x for x in secrets if x and len(x) >= 6}
        self._secrets = sorted(s, key=len, reverse=True)

    def text(self, value: Any) -> str:
        t = value if isinstance(value, str) else str(value)
        for s in self._secrets:
            if s in t:
                t = t.replace(s, MASK)
        t = _SIG_RE.sub(r"\1" + MASK, t)
        t = _HDR_RE.sub(r"\1" + MASK, t)
        t = _TG_RE.sub(r"\1" + MASK, t)
        t = _LK_RE.sub(r"\1" + MASK, t)
        t = _WS_LK_RE.sub(r"\1" + MASK, t)
        return t

    __call__ = text

    def obj(self, value: Any) -> Any:
        """dict/list 를 재귀로 복사하며 민감 키와 비밀값 문자열을 가린다."""
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if str(k).lower() in SENSITIVE_KEYS:
                    out[k] = MASK
                else:
                    out[k] = self.obj(v)
            return out
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


# 프로세스 전역 (로깅 포매터가 참조). 설정을 읽은 뒤 add() 로 비밀값을 등록한다.
GLOBAL_REDACTOR = Redactor()


class RedactingFormatter(logging.Formatter):
    """포맷이 끝난 최종 문자열(예외 트레이스백 포함)을 가린다."""

    def __init__(self, *args, redactor: Redactor = GLOBAL_REDACTOR, **kwargs):
        super().__init__(*args, **kwargs)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.text(super().format(record))
