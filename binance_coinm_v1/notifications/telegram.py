"""
텔레그램 알림 (알림 전용 - 명령 수신 없음).

notify() 는 큐에 넣고 바로 돌아온다. 전송은 별도 작업자 태스크가 한다. 그래서
텔레그램이 느리거나 죽어도 가격 감시·주문 감시·보호 주문·비상 청산이 멈추지 않는다.
큐가 넘치면 오래된 알림부터 버린다(치명 알림은 우선 보존). 전송 실패는 기록만 한다.

모든 메시지는 Redactor 를 거친다 (API 키·시크릿·토큰이 새지 않게).
시각은 내부적으로 UTC, 표시만 KST(+09:00, 서머타임 없음).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Deque, List, Optional, Tuple

from ..storage.redact import GLOBAL_REDACTOR, Redactor

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9), "KST")

KIND_ICONS = {
    "startup": "🤖", "shutdown": "🔌", "connection_error": "📡", "signal": "📐", "entry": "🟢",
    "fill": "✅", "stop": "🛑", "tp": "🎯", "close": "🏁", "recovery": "🛠", "daily_loss": "⛔",
    "api_error": "⚠️", "ws_reconnect": "🔁", "validation_gate": "🚦", "critical": "🚨",
    "trailing": "📈", "funding": "💱",
}


def kst(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, KST).strftime("%Y-%m-%d %H:%M:%S KST")


class Notifier:
    def notify(self, kind: str, text: str, critical: bool = False) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        pass

    async def stop(self, flush_timeout: float = 3.0) -> None:
        pass


class NullNotifier(Notifier):
    """텔레그램 미설정 시: 알림 내용을 로그로만 남긴다 (종이 매매 화면에서 보이도록 INFO)."""

    def __init__(self, redactor: Redactor = GLOBAL_REDACTOR):
        self.redactor = redactor

    def notify(self, kind: str, text: str, critical: bool = False) -> None:
        msg = self.redactor.text(text).replace(chr(10), " | ")
        (logger.error if critical else logger.info)("[알림:%s] %s", kind, msg)


class RecordingNotifier(Notifier):
    """테스트용."""

    def __init__(self):
        self.messages: List[Tuple[str, str, bool]] = []

    def notify(self, kind: str, text: str, critical: bool = False) -> None:
        self.messages.append((kind, text, critical))

    def kinds(self) -> List[str]:
        return [k for k, _, _ in self.messages]


Sender = Callable[[str], Awaitable[None]]


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, redactor: Redactor = GLOBAL_REDACTOR,
                 sender: Optional[Sender] = None, max_queue: int = 200,
                 min_interval: float = 1.0, timeout: float = 10.0, prefix: str = "[COIN-M V1]"):
        self._token = token
        self.chat_id = chat_id
        self.redactor = redactor
        self.redactor.add(token)
        self.sender = sender or self._http_send
        self.max_queue = max_queue
        self.min_interval = min_interval
        self.timeout = timeout
        self.prefix = prefix
        self._q: Deque[Tuple[str, bool]] = deque()
        self._event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._session = None
        self.sent = 0
        self.failed = 0
        self.dropped = 0

    # ---- 호출부 (동기, 절대 예외를 내지 않는다) ----
    def notify(self, kind: str, text: str, critical: bool = False) -> None:
        try:
            icon = KIND_ICONS.get("critical" if critical else kind, "•")
            body = self.redactor.text(text)
            msg = f"{icon} <b>{html.escape(self.prefix)}</b> {html.escape(kind)}\n{html.escape(body)}\n<i>{kst()}</i>"
            if len(self._q) >= self.max_queue:
                # 비치명 알림부터 버린다
                for idx, (_, crit) in enumerate(self._q):
                    if not crit:
                        del self._q[idx]
                        break
                else:
                    self._q.popleft()
                self.dropped += 1
            self._q.append((msg, critical))
            if self._event is not None:
                self._event.set()
        except Exception:                      # 알림이 매매를 멈추게 해서는 안 된다
            logger.exception("알림 큐 적재 실패")

    # ---- 작업자 ----
    async def start(self) -> None:
        if self._task is None:
            self._event = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(self._worker())

    async def stop(self, flush_timeout: float = 3.0) -> None:
        if self._task is None:
            return
        try:
            deadline = time.monotonic() + flush_timeout
            while self._q and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        finally:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:
                    pass

    async def _worker(self) -> None:
        assert self._event is not None
        while True:
            if not self._q:
                self._event.clear()
                await self._event.wait()
                continue
            msg, _ = self._q.popleft()
            try:
                await asyncio.wait_for(self.sender(msg), timeout=self.timeout)
                self.sent += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failed += 1
                logger.warning("텔레그램 전송 실패 (%s) - 매매는 계속", self.redactor.text(type(e).__name__))
            await asyncio.sleep(self.min_interval)

    async def _http_send(self, msg: str) -> None:
        if os.environ.get("COINM_V1_TEST_MODE") == "1":
            raise RuntimeError("테스트 모드: 실제 텔레그램 전송 금지")
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        async with self._session.post(url, json={"chat_id": self.chat_id, "text": msg,
                                                 "parse_mode": "HTML",
                                                 "disable_web_page_preview": True}) as r:
            if r.status != 200:
                raise RuntimeError(f"telegram HTTP {r.status}")


def build_notifier(settings: Any, redactor: Redactor = GLOBAL_REDACTOR) -> Notifier:
    if getattr(settings, "telegram_enabled", False):
        return TelegramNotifier(settings.telegram_token, settings.telegram_chat_id, redactor)
    return NullNotifier()
