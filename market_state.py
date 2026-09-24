import time
import math
import logging
from collections import deque
from typing import Dict, Any, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class TickerState:
    """종목 하나의 실시간 상태. 웹소켓 콜백이 갱신하고 전략이 읽는다."""

    def __init__(self, code: str, maxlen: int = 600):
        self.code = code
        self.last_price: Optional[float] = None
        self.prev_price: Optional[float] = None

        # 호가창
        self.asks: List[float] = []        # 매도호가 가격 (오름차순)
        self.bids: List[float] = []        # 매수호가 가격 (내림차순)
        self.ask_sizes: List[float] = []
        self.bid_sizes: List[float] = []
        self.total_ask_size: float = 0.0
        self.total_bid_size: float = 0.0

        # 시계열
        self.returns = deque(maxlen=maxlen)       # 로그수익률
        self.spreads = deque(maxlen=maxlen)       # 상대 스프레드
        self.signed_flow = deque(maxlen=maxlen)   # +매수체결 / -매도체결

        self.ts_ticker: float = 0.0
        self.ts_orderbook: float = 0.0
        self.ts_trade: float = 0.0

    # ---------------- 갱신 ----------------
    def update_ticker(self, d: Dict[str, Any]):
        p = d.get("trade_price")
        if p:
            self.prev_price = self.last_price
            self.last_price = float(p)
            if self.prev_price and self.prev_price > 0:
                self.returns.append(math.log(self.last_price / self.prev_price))
        self.ts_ticker = time.time()

    def update_orderbook(self, d: Dict[str, Any]):
        units = d.get("orderbook_units") or []
        if not units:
            return
        self.asks = [float(u["ask_price"]) for u in units]
        self.bids = [float(u["bid_price"]) for u in units]
        self.ask_sizes = [float(u["ask_size"]) for u in units]
        self.bid_sizes = [float(u["bid_size"]) for u in units]
        self.total_ask_size = float(d.get("total_ask_size") or sum(self.ask_sizes))
        self.total_bid_size = float(d.get("total_bid_size") or sum(self.bid_sizes))

        best_ask, best_bid = self.asks[0], self.bids[0]
        mid = (best_ask + best_bid) / 2.0
        if mid > 0:
            self.spreads.append((best_ask - best_bid) / mid)
        self.ts_orderbook = time.time()

    def update_trade(self, d: Dict[str, Any]):
        vol = float(d.get("trade_volume") or 0.0)
        # ask_bid 는 체결을 유발한 쪽. ASK = 매도 체결(하방 압력)
        sign = -1.0 if d.get("ask_bid") == "ASK" else 1.0
        self.signed_flow.append(sign * vol)
        self.ts_trade = time.time()

    # ---------------- 파생 지표 ----------------
    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0] if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0] if self.bids else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.asks and self.bids:
            return (self.asks[0] + self.bids[0]) / 2.0
        return self.last_price

    def rel_spread(self) -> float:
        if not self.asks or not self.bids:
            return 0.0
        mid = (self.asks[0] + self.bids[0]) / 2.0
        return (self.asks[0] - self.bids[0]) / mid if mid > 0 else 0.0

    def book_imbalance(self) -> float:
        """-1(매도우위) ~ +1(매수우위)"""
        tot = self.total_ask_size + self.total_bid_size
        if tot <= 0:
            return 0.0
        return (self.total_bid_size - self.total_ask_size) / tot

    def flow_imbalance(self) -> float:
        """최근 체결 주문흐름 불균형 -1 ~ +1"""
        if not self.signed_flow:
            return 0.0
        arr = np.fromiter(self.signed_flow, dtype=float)
        denom = np.abs(arr).sum()
        return float(arr.sum() / denom) if denom > 0 else 0.0

    def realized_vol(self) -> float:
        if len(self.returns) < 5:
            return 0.0
        return float(np.std(np.fromiter(self.returns, dtype=float)))

    def depth_curve(self, side: str = "both") -> np.ndarray:
        """
        서킷브레이커 Betti-0 판정용 실제 호가 가격 사다리.
        (원래 코드의 np.random.randn(20).cumsum() 을 대체)
        """
        if side == "ask":
            return np.asarray(self.asks, dtype=float)
        if side == "bid":
            return np.asarray(self.bids, dtype=float)
        if not self.asks or not self.bids:
            return np.array([])
        # 매수호가(역순) + 매도호가 = 중앙을 가로지르는 단조 증가 가격 사다리
        return np.asarray(list(reversed(self.bids)) + self.asks, dtype=float)

    def viscosity(self) -> float:
        """
        GRU 점성 게이트(z_t)의 실측 대용치. 0(원활) ~ 1(경직).
        (원래 코드의 np.random.rand() 를 대체)
        """
        if not self.asks or not self.bids:
            return 0.0

        # 상대 스프레드가 평소 대비 몇 배로 벌어졌는가
        s = self.rel_spread()
        if len(self.spreads) >= 20:
            base = float(np.median(np.fromiter(self.spreads, dtype=float)))
            spread_score = math.tanh(s / base - 1.0) if base > 0 else 0.0
        else:
            spread_score = math.tanh(s * 200.0)
        spread_score = max(0.0, spread_score)

        imb_score = abs(self.book_imbalance())
        vol_score = math.tanh(self.realized_vol() * 300.0)

        z = 0.45 * spread_score + 0.25 * imb_score + 0.30 * vol_score
        return float(min(1.0, max(0.0, z)))

    def feature_vector(self, dim: int = 20) -> np.ndarray:
        """RL 에이전트 관측치. (원래 코드의 np.random.randn(20) 을 대체)"""
        rets = np.fromiter(self.returns, dtype=float)
        last_rets = rets[-5:] if len(rets) >= 5 else np.zeros(5)
        if len(last_rets) < 5:
            last_rets = np.pad(last_rets, (5 - len(last_rets), 0))

        ask_sz = np.asarray(self.ask_sizes[:5], dtype=float)
        bid_sz = np.asarray(self.bid_sizes[:5], dtype=float)
        ask_sz = np.pad(ask_sz, (0, max(0, 5 - len(ask_sz))))[:5]
        bid_sz = np.pad(bid_sz, (0, max(0, 5 - len(bid_sz))))[:5]
        sz_tot = ask_sz.sum() + bid_sz.sum()
        if sz_tot > 0:
            ask_sz, bid_sz = ask_sz / sz_tot, bid_sz / sz_tot

        feats = np.concatenate([
            last_rets * 100.0,
            ask_sz,
            bid_sz,
            [self.rel_spread() * 100.0,
             self.book_imbalance(),
             self.flow_imbalance(),
             self.realized_vol() * 100.0,
             self.viscosity()],
        ])
        feats = np.nan_to_num(feats, nan=0.0, posinf=0.0, neginf=0.0)
        if len(feats) < dim:
            feats = np.pad(feats, (0, dim - len(feats)))
        return feats[:dim].astype(np.float32)

    def age(self) -> float:
        """가장 최근 갱신 이후 경과 시간(초)."""
        newest = max(self.ts_ticker, self.ts_orderbook, self.ts_trade)
        return float("inf") if newest == 0 else time.time() - newest


class MarketState:
    """
    웹소켓 피드의 콜백으로 등록되어 모든 종목 상태를 유지한다.
    add_callback 이 한 번도 호출되지 않아 수신 데이터가 전부 버려지던
    문제를 해결하는 지점.
    """

    def __init__(self, tickers: List[str]):
        self.states: Dict[str, TickerState] = {t: TickerState(t) for t in tickers}
        self.message_count = 0

    def on_message(self, data: Dict[str, Any]):
        code = data.get("code")
        if not code:
            return
        st = self.states.get(code)
        if st is None:
            return

        mtype = data.get("type")
        if mtype == "ticker":
            st.update_ticker(data)
        elif mtype == "orderbook":
            st.update_orderbook(data)
        elif mtype == "trade":
            st.update_trade(data)
        self.message_count += 1

    def add(self, ticker: str) -> TickerState:
        if ticker not in self.states:
            self.states[ticker] = TickerState(ticker)
        return self.states[ticker]

    def get(self, ticker: str) -> Optional[TickerState]:
        return self.states.get(ticker)

    def is_ready(self, ticker: str) -> bool:
        st = self.states.get(ticker)
        return bool(st and st.last_price and st.asks and st.bids)

    def is_stale(self, ticker: str, max_age: float) -> bool:
        st = self.states.get(ticker)
        return st is None or st.age() > max_age
