"""
업비트 캔들 수집 + 알트코인 유니버스 선별.

가격행동 전략은 웹소켓 틱이 아니라 '마감된 봉' 으로 판정한다. 캔들/티커/
마켓 목록은 전부 공개(시세) API 라 키가 필요 없고, 주문 API 와 한도가 따로
잡혀 있다(시세 API 초당 10회). 주문 경로의 레이트리미터를 공유하지 않도록
전용 클라이언트와 리미터를 둔다.

알트 유니버스는 '급등을 쫓는' 목록이 아니다. 급등 종목 대부분은 업비트가
'주의' 로 지정한 펌프(거래량 급증·가격 급변·소수계정 집중)라 오히려 뺀다.
여기서 고르는 것은 '호가가 두껍고 경고가 없는' 알트이고, 급등 뒤 첫 되돌림
(라스트 키스·추세 캥거루)을 잡는 건 패턴이 한다.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from price_action import Bars
from upbit_client import RateLimiter

logger = logging.getLogger(__name__)

# 원화 가치에 붙어 움직이는 코인. 가격행동이 의미가 없다.
STABLES = {"USDT", "USDC", "USDS", "USDE", "DAI", "TUSD", "FDUSD", "PYUSD", "USD1"}


def _parse_utc(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def candles_to_bars(candles: Sequence[Any], unit_min: int, now: Optional[float] = None,
                    grace_sec: float = 2.0) -> Bars:
    """
    SDK Candle 목록(최신순) -> 시간순 Bars. 아직 마감 안 된 봉은 버린다.
    판정을 '마감된 봉' 으로만 하는 것이 책의 전제다 - 종가가 가장 중요한
    가격이고(4장), 형성 중인 봉은 캥거루였다가 몇 분 뒤 아닐 수 있다.
    """
    period = unit_min * 60
    now = time.time() if now is None else now
    rows = []
    for c in candles:
        t0 = _parse_utc(c.candle_date_time_utc)
        if t0 + period > now - grace_sec:
            continue
        rows.append((t0, float(c.opening_price), float(c.high_price), float(c.low_price),
                     float(c.trade_price), float(c.candle_acc_trade_price)))
    return Bars.from_rows(rows, period)


class CandleFeed:
    """
    종목·시간봉별 캔들 캐시. 새 봉이 마감됐을 때만 다시 받는다.
    백테스트용 과거 수집(fetch_history)도 여기서 한다.
    """

    # 업비트 시세 API 는 IP 당 초당 10회. 라이브 스캔(6)과 자동 백테스트(3)가
    # 동시에 돌아도 합이 10 을 넘지 않게 나눈다.
    LIVE_PER_SECOND = 6
    BACKTEST_PER_SECOND = 3
    MIN_REFETCH_SEC = 30.0

    def __init__(self, client=None, per_second: int = LIVE_PER_SECOND, per_minute: int = 400):
        if client is None:
            from upbit import AsyncUpbit
            client = AsyncUpbit()          # 시세 API 는 키 불필요
        self.client = client
        self.limiter = RateLimiter(per_second=per_second, per_minute=per_minute)
        self._cache: Dict[tuple, Bars] = {}
        self._fetched_at: Dict[tuple, float] = {}
        self.requests = 0

    async def close(self):
        try:
            await self.client.close()
        except Exception:
            pass

    async def _fetch(self, ticker: str, unit_min: int, count: int = 200,
                     to: Optional[str] = None) -> List[Any]:
        await self.limiter.acquire()
        self.requests += 1
        kwargs: Dict[str, Any] = {"market": ticker, "count": count}
        if to:
            kwargs["to"] = to
        if unit_min >= 1440:
            return list(await self.client.candles.list_days(**kwargs))
        return list(await self.client.candles.list_minutes(unit_min, **kwargs))

    def _stale(self, key: tuple, unit_min: int, now: float) -> bool:
        bars = self._cache.get(key)
        if bars is None or not len(bars):
            return True
        # 마지막 마감 봉 다음 봉이 마감됐으면 새로 받는다 (+3초 여유)
        return now >= bars.close_time(len(bars) - 1) + unit_min * 60 + 3.0

    async def get_bars(self, ticker: str, unit_min: int, count: int = 200) -> Optional[Bars]:
        key = (ticker, unit_min)
        now = time.time()
        if not self._stale(key, unit_min, now):
            return self._cache[key]
        # 거래가 없던 시간대는 업비트가 봉을 만들지 않는다. 그런 종목은 '다음
        # 봉이 마감됐어야 하는데 없음' 상태가 계속돼 20초마다 재요청하게 되므로
        # 최소 간격을 둔다.
        if now - self._fetched_at.get(key, 0.0) < self.MIN_REFETCH_SEC:
            return self._cache.get(key)
        self._fetched_at[key] = now
        try:
            raw = await self._fetch(ticker, unit_min, count)
        except Exception as e:
            logger.warning("캔들 조회 실패 %s %dm: %s", ticker, unit_min, e)
            return self._cache.get(key)
        bars = candles_to_bars(raw, unit_min, now)
        if len(bars):
            self._cache[key] = bars
        return bars if len(bars) else self._cache.get(key)

    async def fetch_history(self, ticker: str, unit_min: int, n_bars: int) -> Optional[Bars]:
        """백테스트용. 200개씩 과거로 넘기며 n_bars 만큼 모은다."""
        rows: List[Any] = []
        to = None
        while len(rows) < n_bars:
            try:
                chunk = await self._fetch(ticker, unit_min, 200, to)
            except Exception as e:
                logger.warning("과거 캔들 조회 실패 %s: %s", ticker, e)
                break
            if not chunk:
                break
            rows.extend(chunk)
            oldest = min(chunk, key=lambda c: c.candle_date_time_utc)
            to = oldest.candle_date_time_utc + "Z"
            if len(chunk) < 200:
                break
        if not rows:
            return None
        return candles_to_bars(rows, unit_min)


class AltUniverse:
    """
    가격행동을 적용할 알트 목록. 기준:
      - KRW 마켓, 스테이블 제외
      - 24시간 거래대금 min_trade_krw 이상, 상위 top_n
      - 유의(market_warning=CAUTION) / 경고 / 주의 이벤트 지정 종목 제외
        (가격 급변·거래량 급증·입금량 급증·해외 괴리·소수계정 집중)
      - 스프레드 max_spread 이하 (시장가 진입 비용)
    신규 상장은 여기서 거르지 않는다 - 상위 시간봉이 부족하면 존을 그릴 수
    없으므로 전략 쪽에서 봉 수로 자연히 걸러진다.
    """

    def __init__(self, client, exclude: Sequence[str] = (), top_n: int = 20,
                 min_trade_krw: float = 5e9, max_spread: float = 0.002,
                 limiter: Optional[RateLimiter] = None):
        self.client = client
        self.exclude = set(exclude)
        self.top_n = top_n
        self.min_trade_krw = min_trade_krw
        self.max_spread = max_spread
        self.limiter = limiter or RateLimiter(per_second=8, per_minute=500)
        self.tickers: List[str] = []
        self.details: Dict[str, Dict[str, Any]] = {}
        self.refreshed_at = 0.0

    @staticmethod
    def flagged(pair: Any) -> List[str]:
        """업비트가 붙인 경고 사유 목록. 비어 있으면 깨끗한 종목."""
        why = []
        if getattr(pair, "market_warning", None) == "CAUTION":
            why.append("유의")
        ev = getattr(pair, "market_event", None)
        if ev is not None:
            if getattr(ev, "warning", False):
                why.append("경고")
            caution = getattr(ev, "caution", None)
            if caution is not None:
                for name in ("price_fluctuations", "trading_volume_soaring",
                             "deposit_amount_soaring", "global_price_differences",
                             "concentration_of_small_accounts"):
                    if getattr(caution, name, False):
                        why.append(name)
        return why

    def select(self, pairs: Sequence[Any], tickers: Sequence[Any],
               spreads: Optional[Dict[str, float]] = None) -> List[str]:
        """네트워크 없이 선별 규칙만 적용 (테스트 가능하도록 분리)."""
        clean = {}
        for p in pairs:
            m = getattr(p, "market", "")
            if not m.startswith("KRW-"):
                continue
            if m.split("-", 1)[1] in STABLES or m in self.exclude:
                continue
            flags = self.flagged(p)
            if flags:
                self.details[m] = {"excluded": flags}
                continue
            clean[m] = p

        ranked = []
        for t in tickers:
            m = getattr(t, "market", "")
            if m not in clean:
                continue
            value = float(getattr(t, "acc_trade_price_24h", 0) or 0)
            if value < self.min_trade_krw:
                continue
            ranked.append((value, m, float(getattr(t, "signed_change_rate", 0) or 0)))
        ranked.sort(reverse=True)

        out = []
        for value, m, chg in ranked:
            sp = (spreads or {}).get(m)
            if sp is not None and sp > self.max_spread:
                self.details[m] = {"excluded": [f"spread {sp*100:.2f}%"]}
                continue
            self.details[m] = {"trade_krw_24h": value, "change_24h": chg, "spread": sp}
            out.append(m)
            if len(out) >= self.top_n:
                break
        return out

    async def refresh(self) -> List[str]:
        try:
            await self.limiter.acquire()
            pairs = list(await self.client.trading_pairs.list(is_details=True))
            await self.limiter.acquire()
            tks = list(await self.client.tickers.list_by_quote_currencies(quote_currencies="KRW"))
        except Exception as e:
            logger.warning("알트 유니버스 갱신 실패 (기존 목록 유지): %s", e)
            return self.tickers

        # 스프레드는 거래대금 1차 통과분만 조회 (한 번에 여러 종목)
        # 스프레드에서 몇 개 빠져도 top_n 을 채울 수 있게 1차는 넉넉히
        saved, self.top_n = self.top_n, self.top_n * 2
        try:
            pre = self.select(pairs, tks)
        finally:
            self.top_n = saved
        spreads: Dict[str, float] = {}
        for k in range(0, len(pre), 15):
            chunk = pre[k:k + 15]
            try:
                await self.limiter.acquire()
                books = await self.client.orderbooks.list(markets=",".join(chunk), count=1)
                for b in books:
                    u = b.orderbook_units[0]
                    ask, bid = float(u.ask_price), float(u.bid_price)
                    if ask > 0 and bid > 0:
                        spreads[b.market] = (ask - bid) / ((ask + bid) / 2)
            except Exception as e:
                logger.debug("호가 조회 실패 (%s): %s", chunk, e)

        self.details = {}
        self.tickers = self.select(pairs, tks, spreads)
        self.refreshed_at = time.time()
        logger.info("알트 유니버스 %d종목: %s", len(self.tickers), ", ".join(self.tickers))
        return self.tickers
