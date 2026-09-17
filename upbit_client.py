import asyncio
import time
import logging
from collections import deque
from typing import Optional, Dict, Any, List, Tuple

from upbit import AsyncUpbit
from upbit.types.order import Order

from config import Config

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    업비트 Open API 레이트리밋 대응 토큰버킷.
    주문 API 는 초당 8회 / 분당 200회 제한이 있어 넘기면 429 가 떨어진다.
    """

    def __init__(self, per_second: int, per_minute: int):
        self.per_second = per_second
        self.per_minute = per_minute
        self._sec = deque()
        self._min = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._sec and now - self._sec[0] > 1.0:
                    self._sec.popleft()
                while self._min and now - self._min[0] > 60.0:
                    self._min.popleft()

                if len(self._sec) < self.per_second and len(self._min) < self.per_minute:
                    self._sec.append(now)
                    self._min.append(now)
                    return

                waits = []
                if len(self._sec) >= self.per_second:
                    waits.append(1.0 - (now - self._sec[0]))
                if len(self._min) >= self.per_minute:
                    waits.append(60.0 - (now - self._min[0]))
                delay = max(0.01, min(waits))
                logger.debug("Rate limit 대기 %.3fs", delay)
                await asyncio.sleep(delay)


class UpbitClientWrapper:
    """업비트 공식 SDK(upbit-sdk) 비동기 래퍼."""

    def __init__(self):
        self.access_key = Config.UPBIT_ACCESS_KEY
        self.secret_key = Config.UPBIT_SECRET_KEY

        if self.access_key and self.secret_key:
            self.client = AsyncUpbit(access_key=self.access_key, secret_key=self.secret_key)
        else:
            self.client = None

        # 업비트 공식 한도보다 보수적으로 잡는다.
        self.order_limiter = RateLimiter(per_second=6, per_minute=180)
        self.query_limiter = RateLimiter(per_second=20, per_minute=800)

    async def close(self):
        if self.client:
            try:
                await self.client.close()
            except Exception as e:
                logger.debug("클라이언트 종료 중 예외: %s", e)

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------
    async def get_accounts(self) -> Optional[List[Any]]:
        if not self.client:
            return None
        try:
            await self.query_limiter.acquire()
            return await self.client.accounts.list()
        except Exception as e:
            logger.error("잔고 조회 실패: %s", e)
            return None

    async def get_balances(self) -> Optional[Dict[str, Dict[str, float]]]:
        """
        {'KRW': {'balance':.., 'locked':.., 'avg_buy_price':..}, 'BTC': {...}} 형태로 정규화.
        SDK 가 pydantic 객체를 돌려주므로 속성 접근으로 읽는다.
        """
        accounts = await self.get_accounts()
        if accounts is None:
            return None

        out: Dict[str, Dict[str, float]] = {}
        for a in accounts:
            try:
                out[a.currency] = {
                    "balance": float(a.balance or 0.0),
                    "locked": float(a.locked or 0.0),
                    "avg_buy_price": float(a.avg_buy_price or 0.0),
                }
            except Exception as e:
                logger.warning("잔고 항목 파싱 실패 (%s): %s", getattr(a, "currency", "?"), e)
        return out

    async def get_open_orders(self, market: Optional[str] = None) -> List[Order]:
        """재시작 후 상태 재동기화(reconciliation)에 사용."""
        if not self.client:
            return []
        try:
            await self.query_limiter.acquire()
            kwargs: Dict[str, Any] = {"limit": 100}
            if market:
                kwargs["market"] = market
            page = await self.client.orders.list_open(**kwargs)
            return [o async for o in page]
        except Exception as e:
            logger.error("미체결 주문 조회 실패: %s", e)
            return []

    async def get_order(self, uuid_str: str) -> Optional[Order]:
        if not self.client:
            return None
        try:
            await self.query_limiter.acquire()
            return await self.client.orders.retrieve(uuid=uuid_str)
        except Exception as e:
            logger.error("주문 조회 실패 (%s): %s", uuid_str, e)
            return None

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    async def place_order(
        self,
        market: str,
        side: str,
        ord_type: str = "limit",
        volume: Optional[str] = None,
        price: Optional[str] = None,
    ) -> Optional[Order]:
        """
        업비트 주문 규격:
          - ord_type='price'  (시장가 매수): price 만 전송, volume 은 전송하지 않는다
          - ord_type='market' (시장가 매도): volume 만 전송, price 는 전송하지 않는다
          - ord_type='limit'  (지정가)     : volume + price 둘 다 전송

        기존 코드처럼 volume='' / price='' 를 넘기면 빈 문자열이 그대로
        body 에 실려 400 validation_error 가 난다. 여기서는 필요한 필드만
        kwargs 에 담아 SDK 가 나머지를 생략하도록 한다.
        """
        if not self.client:
            logger.error("Upbit 클라이언트 미초기화 - 주문 불가")
            return None

        kwargs: Dict[str, Any] = {"market": market, "side": side, "ord_type": ord_type}

        if ord_type == "price":
            if not price:
                logger.error("시장가 매수에는 price(주문금액)가 필요합니다.")
                return None
            kwargs["price"] = price
        elif ord_type == "market":
            if not volume:
                logger.error("시장가 매도에는 volume(수량)이 필요합니다.")
                return None
            kwargs["volume"] = volume
        else:  # limit
            if not volume or not price:
                logger.error("지정가 주문에는 volume 과 price 가 모두 필요합니다.")
                return None
            kwargs["volume"] = volume
            kwargs["price"] = price

        try:
            await self.order_limiter.acquire()
            order = await self.client.orders.create(**kwargs)
            logger.info("주문 접수됨 uuid=%s (%s %s %s)", order.uuid, market, side, ord_type)
            return order
        except Exception as e:
            logger.error("주문 실패 (%s %s %s / %s): %s", market, side, ord_type, kwargs, e)
            return None

    async def cancel_order(self, uuid_str: str) -> Optional[Order]:
        if not self.client:
            return None
        try:
            await self.order_limiter.acquire()
            return await self.client.orders.cancel(uuid=uuid_str)
        except Exception as e:
            logger.error("주문 취소 실패 (%s): %s", uuid_str, e)
            return None

    async def cancel_all_open(self, side: str = "all") -> Optional[Any]:
        """서킷브레이커 발동 시 일괄 취소."""
        if not self.client:
            return None
        try:
            await self.order_limiter.acquire()
            return await self.client.orders.cancel_open(cancel_side=side)
        except Exception as e:
            logger.error("일괄 취소 실패: %s", e)
            return None
