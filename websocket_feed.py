import asyncio
import json
import logging
import random
import time
import uuid
from typing import List, Callable, Dict, Any

import websockets

logger = logging.getLogger(__name__)


class WebsocketFeed:
    def __init__(self, tickers: List[str]):
        self.tickers = tickers
        self.url = "wss://api.upbit.com/websocket/v1"
        self.callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._is_running = False
        self._ws = None
        self._close_tasks: set = set()

        # 관측 지표 (하트비트/모니터링용)
        self.last_message_ts: float = 0.0
        self.message_count: int = 0
        self.reconnect_count: int = 0

    def add_callback(self, callback: Callable[[Dict[str, Any]], None]):
        self.callbacks.append(callback)

    def set_tickers(self, tickers: List[str]) -> bool:
        """
        구독 종목 교체. 바뀌었으면 현재 연결을 닫아 재연결 루프가 새 목록으로
        다시 구독하게 한다 (정상 종료라 백오프 없이 바로 붙는다).
        가격행동 전략이 알트에 라이브 거래를 걸 때만 호출된다.
        """
        new = list(dict.fromkeys(tickers))
        if new == self.tickers:
            return False
        self.tickers = new
        ws = self._ws
        if ws is not None:
            try:
                # 참조를 붙잡아 두지 않으면 태스크가 GC 로 사라질 수 있다
                task = asyncio.get_running_loop().create_task(ws.close())
                self._close_tasks.add(task)
                task.add_done_callback(self._close_tasks.discard)
            except RuntimeError:
                pass
        return True

    def age(self) -> float:
        """마지막 메시지 이후 경과 시간(초)."""
        return float("inf") if self.last_message_ts == 0 else time.time() - self.last_message_ts

    async def _handle_message(self, message):
        try:
            # 업비트는 바이너리 프레임으로 보낸다. json.loads 는 bytes 를 그대로 받는다.
            data = json.loads(message)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error("웹소켓 메시지 디코딩 실패: %s", e)
            return

        self.last_message_ts = time.time()
        self.message_count += 1

        for callback in self.callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                # 콜백 하나가 터져도 피드 전체가 죽으면 안 된다.
                logger.error("콜백 처리 중 예외 (%s): %s", getattr(callback, "__qualname__", callback), e)

    def _subscribe_payload(self) -> str:
        return json.dumps([
            {"ticket": str(uuid.uuid4())},
            {"type": "ticker", "codes": self.tickers},
            {"type": "orderbook", "codes": self.tickers},
            {"type": "trade", "codes": self.tickers},
            {"format": "DEFAULT"},
        ])

    async def connect_and_listen(self):
        self._is_running = True
        backoff = 1.0

        while self._is_running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=30,     # 죽은 커넥션을 빨리 감지
                    ping_timeout=15,
                    max_queue=1024,       # 무한 버퍼링 방지
                ) as websocket:
                    self._ws = websocket
                    logger.info("웹소켓 연결됨 (%s)", ", ".join(self.tickers))
                    await websocket.send(self._subscribe_payload())
                    backoff = 1.0         # 연결 성공 시 백오프 초기화

                    async for message in websocket:
                        if not self._is_running:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                logger.info("웹소켓 태스크 취소됨.")
                raise
            except Exception as e:
                if not self._is_running:
                    break
                self.reconnect_count += 1
                # 지수 백오프 + 지터. 고정 5초 재시도는 장애 시 거래소를 계속 두드린다.
                delay = min(60.0, backoff) * (1.0 + random.random() * 0.3)
                logger.warning(
                    "웹소켓 끊김(%s회차): %s. %.1f초 후 재연결...",
                    self.reconnect_count, e, delay,
                )
                await asyncio.sleep(delay)
                backoff = min(60.0, backoff * 2.0)
            finally:
                self._ws = None

        logger.info("웹소켓 리스너 종료.")

    def stop(self):
        self._is_running = False
