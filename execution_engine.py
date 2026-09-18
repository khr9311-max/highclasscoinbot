import asyncio
import logging
import time
import uuid as uuidlib
from typing import Callable, List, Dict, Any, Optional, Tuple

from config import Config
from upbit_client import UpbitClientWrapper
from websocket_feed import WebsocketFeed
from market_state import MarketState
from risk_manager import RiskManager
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)

# 주문이 '끝난' 상태들. 업비트 주문 state 값.
TERMINAL_STATES = {"done", "cancel"}


class Clock:
    """
    Hummingbot 스타일 중앙 클럭.

    기존 구현 대비 변경점:
      - iterator 를 순차 await 하지 않고 동시 실행한다. LLM 호출 하나가
        주문 타임아웃 체크까지 멈춰 세우던 문제를 없앤다.
      - sleep(interval - elapsed) 누적 드리프트 대신 절대 데드라인을 쓴다.
      - iterator 당 타임아웃을 걸어 한 개가 영구히 멈춰도 클럭이 산다.
    """

    def __init__(self, tick_interval: float, iterator_timeout: float = 30.0):
        self.tick_interval = tick_interval
        self.iterator_timeout = iterator_timeout
        self._is_running = False
        self.iterators: List[Callable] = []
        self.tick_count = 0

    def add_iterator(self, iterator: Callable):
        self.iterators.append(iterator)

    async def _safe_call(self, iterator: Callable):
        name = getattr(iterator, "__qualname__", repr(iterator))
        try:
            if asyncio.iscoroutinefunction(iterator):
                await asyncio.wait_for(iterator(), timeout=self.iterator_timeout)
            else:
                iterator()
        except asyncio.TimeoutError:
            logger.error("iterator 타임아웃(%.0fs): %s", self.iterator_timeout, name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("iterator 예외 [%s]: %s", name, e)

    async def start(self):
        self._is_running = True
        logger.info("Clock 시작 (tick=%.2fs)", self.tick_interval)
        next_tick = time.monotonic()

        while self._is_running:
            next_tick += self.tick_interval
            await asyncio.gather(*(self._safe_call(it) for it in self.iterators))
            self.tick_count += 1

            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                # 한 틱이 밀렸으면 지난 틱을 몰아서 따라잡지 않고 기준선을 재설정한다.
                next_tick = time.monotonic()

        logger.info("Clock 정지.")

    def stop(self):
        self._is_running = False


class ExecutionEngine:
    def __init__(self):
        self.config = Config
        self.clock = Clock(self.config.UPDATE_INTERVAL)
        self.client = UpbitClientWrapper()
        self.ws_feed = WebsocketFeed(self.config.TARGET_TICKERS)
        self.notifier = TelegramNotifier()
        self.risk = RiskManager()

        # --- 웹소켓 데이터를 실제로 소비하는 지점 ---
        # 기존 코드는 add_callback 을 한 번도 호출하지 않아 수신 데이터를
        # 전부 버리고 있었다. 전략이 보는 시장 상태가 여기서 채워진다.
        self.market = MarketState(self.config.TARGET_TICKERS)
        self.ws_feed.add_callback(self.market.on_message)

        self.active_orders: Dict[str, Dict[str, Any]] = {}
        self.balances: Dict[str, Dict[str, float]] = {}
        self._balances_ts: float = 0.0

        # main.py 가 DataRecorder 생성 후 주입한다(구성 순서상 여기서는
        # 아직 없다). place_market_buy/sell, _sync_active_orders 가 이걸
        # 통해 orders/*.jsonl 에 남긴다 - 기존에는 record_order() 자체가
        # 정의만 있고 어디서도 호출되지 않아, 실제 체결이 나도 감사/학습용
        # 주문 로그가 하나도 안 쌓였다.
        self.recorder = None

        # DRY-RUN 모의 장부 (실주문 없이 리스크 로직을 그대로 태우기 위함)
        self.sim_krw: float = 0.0
        self.sim_positions: Dict[str, float] = {}

        self.clock.add_iterator(self._sync_active_orders)
        self.clock.add_iterator(self._refresh_balances)

    # ------------------------------------------------------------------
    # 잔고 / 포지션
    # ------------------------------------------------------------------
    @staticmethod
    def _currency_of(ticker: str) -> str:
        return ticker.split("-", 1)[1] if "-" in ticker else ticker

    BALANCE_REFRESH_SEC = 15.0

    async def _refresh_balances(self, force: bool = False):
        """주기적 잔고 갱신. 매 틱 호출하면 조회 레이트리밋을 먹는다."""
        if not force and time.time() - self._balances_ts < self.BALANCE_REFRESH_SEC:
            return
        balances = await self.client.get_balances()
        if balances is not None:
            self.balances = balances
            self._balances_ts = time.time()

    def available_krw(self) -> float:
        if self.config.DRY_RUN:
            return self.sim_krw
        krw = self.balances.get("KRW")
        return krw["balance"] if krw else 0.0

    def held_volume(self, ticker: str) -> float:
        cur = self._currency_of(ticker)
        if self.config.DRY_RUN:
            return self.sim_positions.get(cur, 0.0)
        pos = self.balances.get(cur)
        return pos["balance"] if pos else 0.0

    def _price_for(self, ticker: str) -> Optional[float]:
        st = self.market.get(ticker)
        if st and st.mid_price:
            return st.mid_price
        cur = self._currency_of(ticker)
        pos = self.balances.get(cur)
        # 시세가 없으면 매수평균가로라도 평가한다 (0으로 평가해 한도가 풀리는 것 방지)
        return pos["avg_buy_price"] if pos and pos["avg_buy_price"] else None

    def position_value_krw(self, ticker: str) -> float:
        vol = self.held_volume(ticker)
        if vol <= 0:
            return 0.0
        price = self._price_for(ticker)
        return vol * price if price else 0.0

    def total_exposure_krw(self) -> float:
        return sum(self.position_value_krw(t) for t in self.config.TARGET_TICKERS)

    def total_equity_krw(self) -> float:
        return self.available_krw() + self.total_exposure_krw()

    # ------------------------------------------------------------------
    # 시장 데이터 건전성
    # ------------------------------------------------------------------
    def market_ready(self, ticker: str) -> Tuple[bool, str]:
        if not self.market.is_ready(ticker):
            return False, f"{ticker} 시장 데이터 미수신"
        if self.market.is_stale(ticker, self.config.MARKET_DATA_STALE_SEC):
            age = self.market.get(ticker).age()
            return False, f"{ticker} 시세 정체 ({age:.0f}s > {self.config.MARKET_DATA_STALE_SEC:.0f}s)"
        return True, ""

    def _slippage_ok(self, ticker: str) -> Tuple[bool, str]:
        """스프레드가 허용 슬리피지를 넘으면 시장가 주문을 내지 않는다."""
        st = self.market.get(ticker)
        if not st:
            return False, f"{ticker} 상태 없음"
        spread = st.rel_spread()
        if spread > self.config.MAX_SLIPPAGE_RATE:
            return False, (
                f"{ticker} 스프레드 과다 ({spread*100:.3f}% > "
                f"{self.config.MAX_SLIPPAGE_RATE*100:.3f}%)"
            )
        return True, ""

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    async def place_market_buy(self, ticker: str, amount_krw: Optional[float] = None) -> bool:
        amount_krw = amount_krw if amount_krw is not None else self.config.ORDER_SIZE_KRW

        ok, why = self.market_ready(ticker)
        if not ok:
            logger.warning("매수 취소: %s", why)
            return False

        ok, why = self._slippage_ok(ticker)
        if not ok:
            logger.warning("매수 취소: %s", why)
            return False

        await self._refresh_balances()
        decision = self.risk.check_buy(
            ticker=ticker,
            krw_amount=amount_krw,
            position_value_krw=self.position_value_krw(ticker),
            total_exposure_krw=self.total_exposure_krw(),
            available_krw=self.available_krw(),
        )
        if not decision:
            logger.warning("매수 거부 [%s]: %s", ticker, decision.reason)
            return False

        price = self._price_for(ticker) or 0.0

        if self.config.DRY_RUN:
            self.sim_krw -= amount_krw
            if price > 0:
                self.sim_positions[self._currency_of(ticker)] = (
                    self.sim_positions.get(self._currency_of(ticker), 0.0) + amount_krw / price
                )
            self.risk.register_order(ticker)
            logger.info("[DRY-RUN] 시장가 매수 %s %s원 (기준가 %s)",
                        ticker, f"{amount_krw:,.0f}", f"{price:,.2f}")
            if self.recorder:
                self.recorder.record_order(
                    order_uuid=f"dryrun-{uuidlib.uuid4()}", ticker=ticker, side="bid",
                    price=price, amount=amount_krw, state="dry_run",
                )
            await self.notifier.notify_trade("BID", ticker, price, amount_krw, dry_run=True)
            return True

        order = await self.client.place_order(
            market=ticker, side="bid", ord_type="price", price=str(amount_krw)
        )
        if order is None:
            await self.notifier.notify_error(f"{ticker} 시장가 매수 주문 실패")
            return False

        # order 는 pydantic Order 객체다. 기존 코드의 `'uuid' in res` 는
        # 항상 False 라 실제 체결된 주문이 추적에서 통째로 누락됐다.
        self.active_orders[order.uuid] = {
            "ticker": ticker,
            "side": "bid",
            "ord_type": "price",
            "timestamp": time.time(),
            "amount_krw": amount_krw,
        }
        self.risk.register_order(ticker)
        logger.info("시장가 매수 접수 uuid=%s %s %s원", order.uuid, ticker, f"{amount_krw:,.0f}")
        if self.recorder:
            self.recorder.record_order(
                order_uuid=order.uuid, ticker=ticker, side="bid",
                price=price, amount=amount_krw, state="submitted",
            )
        await self.notifier.notify_trade("BID", ticker, price, amount_krw)
        return True

    async def place_market_sell(self, ticker: str, volume: Optional[float] = None) -> bool:
        ok, why = self.market_ready(ticker)
        if not ok:
            logger.warning("매도 취소: %s", why)
            return False

        await self._refresh_balances()
        held = self.held_volume(ticker)
        volume = held if volume is None else min(volume, held)   # 보유량 초과 매도 방지
        price = self._price_for(ticker) or 0.0

        decision = self.risk.check_sell(ticker, volume, held, price)
        if not decision:
            logger.warning("매도 거부 [%s]: %s", ticker, decision.reason)
            return False

        if self.config.DRY_RUN:
            cur = self._currency_of(ticker)
            self.sim_positions[cur] = max(0.0, self.sim_positions.get(cur, 0.0) - volume)
            self.sim_krw += volume * price
            self.risk.register_order(ticker)
            logger.info("[DRY-RUN] 시장가 매도 %s %.8f (기준가 %s)", ticker, volume, f"{price:,.2f}")
            if self.recorder:
                self.recorder.record_order(
                    order_uuid=f"dryrun-{uuidlib.uuid4()}", ticker=ticker, side="ask",
                    price=price, amount=volume, state="dry_run",
                )
            await self.notifier.notify_trade("ASK", ticker, price, volume, dry_run=True)
            return True

        order = await self.client.place_order(
            market=ticker, side="ask", ord_type="market", volume=f"{volume:.8f}"
        )
        if order is None:
            await self.notifier.notify_error(f"{ticker} 시장가 매도 주문 실패")
            return False

        self.active_orders[order.uuid] = {
            "ticker": ticker,
            "side": "ask",
            "ord_type": "market",
            "timestamp": time.time(),
            "volume": volume,
        }
        self.risk.register_order(ticker)
        logger.info("시장가 매도 접수 uuid=%s %s %.8f", order.uuid, ticker, volume)
        if self.recorder:
            self.recorder.record_order(
                order_uuid=order.uuid, ticker=ticker, side="ask",
                price=price, amount=volume, state="submitted",
            )
        await self.notifier.notify_trade("ASK", ticker, price, volume)
        return True

    # ------------------------------------------------------------------
    # 주문 추적
    # ------------------------------------------------------------------
    async def _sync_active_orders(self):
        """
        체결/취소된 주문은 목록에서 지우고, 아직 열려 있는 주문만
        타임아웃 시 취소한다.

        기존 구현은 체결 여부를 보지 않고 60초가 지나면 무조건 취소를 걸어,
        이미 체결된 시장가 주문에 대고 취소 API 를 호출했다.
        """
        if not self.active_orders:
            return

        for order_uuid, info in list(self.active_orders.items()):
            order = await self.client.get_order(order_uuid)

            if order is None:
                # 조회 실패가 오래 지속되면 방치하지 말고 떨군다.
                if time.time() - info["timestamp"] > self.config.ORDER_TIMEOUT_SEC * 5:
                    logger.warning("주문 조회 불가 지속 - 추적 해제: %s", order_uuid)
                    self.active_orders.pop(order_uuid, None)
                continue

            if order.state in TERMINAL_STATES:
                logger.info("주문 종료 uuid=%s state=%s 체결량=%s",
                            order_uuid, order.state, order.executed_volume)
                if self.recorder:
                    self.recorder.record_order(
                        order_uuid=order_uuid, ticker=info["ticker"], side=info["side"],
                        price=self._price_for(info["ticker"]) or 0.0,
                        amount=info.get("amount_krw") or info.get("volume") or 0.0,
                        state=order.state,
                        extra={"executed_volume": float(order.executed_volume or 0.0)},
                    )
                self.active_orders.pop(order_uuid, None)
                await self._refresh_balances(force=True)
                continue

            if time.time() - info["timestamp"] > self.config.ORDER_TIMEOUT_SEC:
                logger.warning("미체결 주문 타임아웃 - 취소: %s (%s)", order_uuid, info["ticker"])
                await self.client.cancel_order(order_uuid)
                if self.recorder:
                    self.recorder.record_order(
                        order_uuid=order_uuid, ticker=info["ticker"], side=info["side"],
                        price=self._price_for(info["ticker"]) or 0.0,
                        amount=info.get("amount_krw") or info.get("volume") or 0.0,
                        state="timeout_cancel",
                    )
                self.active_orders.pop(order_uuid, None)
                await self._refresh_balances(force=True)

    async def cancel_all(self):
        """서킷브레이커 발동 시 호출."""
        if self.config.DRY_RUN:
            logger.info("[DRY-RUN] 전체 주문 취소 (모의)")
            self.active_orders.clear()
            return
        await self.client.cancel_all_open("all")
        self.active_orders.clear()
        logger.warning("미체결 주문 전체 취소 요청 완료.")

    # ------------------------------------------------------------------
    # 기동 / 종료
    # ------------------------------------------------------------------
    async def reconcile_on_startup(self):
        """
        재시작 후 상태 재동기화.
        active_orders 가 메모리에만 있어서 EC2 재부팅 시 미체결 주문을
        통째로 잃어버리던 문제를 해결한다.
        """
        logger.info("기동 상태 재동기화 시작...")
        await self._refresh_balances(force=True)

        if self.config.DRY_RUN:
            krw = self.balances.get("KRW")
            self.sim_krw = krw["balance"] if krw else self.config.MAX_TOTAL_EXPOSURE_KRW
            for t in self.config.TARGET_TICKERS:
                cur = self._currency_of(t)
                pos = self.balances.get(cur)
                self.sim_positions[cur] = pos["balance"] if pos else 0.0
            logger.info("[DRY-RUN] 모의 장부 초기화: KRW %s", f"{self.sim_krw:,.0f}")

        recovered = 0
        for order in await self.client.get_open_orders():
            if order.market not in self.config.TARGET_TICKERS:
                continue
            self.active_orders[order.uuid] = {
                "ticker": order.market,
                "side": order.side,
                "ord_type": order.ord_type,
                "timestamp": time.time(),   # 복구분은 지금부터 타임아웃 계산
                "recovered": True,
            }
            recovered += 1

        equity = self.total_equity_krw()
        self.risk.update_equity(equity)

        logger.info(
            "재동기화 완료 | 미체결 복구 %d건 | 평가액 %s원 | %s",
            recovered, f"{equity:,.0f}", self.risk.summary(),
        )
        return recovered

    async def run(self):
        ws_task = asyncio.create_task(self.ws_feed.connect_and_listen(), name="ws_feed")
        try:
            await self.clock.start()
        finally:
            self.ws_feed.stop()
            ws_task.cancel()
            # 기존 코드는 cancel 만 하고 await 하지 않아 정리가 끝나기 전에 빠져나갔다.
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            await self.client.close()
            await self.notifier.close()

    def stop(self):
        self.clock.stop()
        self.ws_feed.stop()
