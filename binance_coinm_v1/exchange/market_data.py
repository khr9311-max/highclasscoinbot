"""
시장 데이터 (공개 API, 키 불필요).

가격 종류를 섞지 않는다:
  - last/contract price : 체결가. 봉(klines)·진입 돌파 판정의 기본
  - mark price          : 미실현손익·청산·(설정 시) 손절 트리거 기준
  - index price         : 현물 지수. BTC 의 USD 평가에 쓴다

봉은 '마감된 것' 만 돌려준다. 마감 여부는 로컬 시계가 아니라 서버 시각(동기화된
오프셋)으로 판단한다. 형성 중인 봉으로는 신호를 만들지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..config.settings import INTERVAL_SECONDS
from ..strategy.price_action import Bars
from .rest_client import BinanceRestClient

logger = logging.getLogger(__name__)

KLINE_PATHS = {"contract": "/dapi/v1/klines", "mark": "/dapi/v1/markPriceKlines",
               "index": "/dapi/v1/indexPriceKlines"}
MAX_KLINE_LIMIT = 1500
MAX_KLINE_SPAN_MS = 200 * 86400 * 1000      # COIN-M: startTime~endTime 최대 200일


@dataclass(frozen=True)
class Kline:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float          # 계약 수 (mark/index 봉은 0)
    close_time_ms: int
    base_volume: float     # BTC 기준 거래량

    def is_closed(self, now_ms: int) -> bool:
        return now_ms > self.close_time_ms


@dataclass(frozen=True)
class PremiumIndex:
    symbol: str
    mark_price: float
    index_price: float
    estimated_settle_price: float
    last_funding_rate: float
    interest_rate: float
    next_funding_time_ms: int
    time_ms: int


@dataclass(frozen=True)
class FundingRecord:
    symbol: str
    funding_time_ms: int
    funding_rate: float
    mark_price: Optional[float]


def parse_klines(rows: Sequence[Sequence[Any]]) -> List[Kline]:
    out = []
    for r in rows or []:
        out.append(Kline(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                         float(r[5] or 0), int(r[6]), float(r[7] or 0)))
    out.sort(key=lambda k: k.open_time_ms)
    dedup: Dict[int, Kline] = {}
    for k in out:
        dedup[k.open_time_ms] = k
    return [dedup[t] for t in sorted(dedup)]


def closed_only(klines: Sequence[Kline], now_ms: int) -> List[Kline]:
    return [k for k in klines if k.is_closed(now_ms)]


def klines_to_bars(klines: Sequence[Kline], period_sec: int) -> Bars:
    if not klines:
        e = np.zeros(0)
        return Bars(e, e, e, e, e, e, period_sec)
    arr = np.asarray([(k.open_time_ms / 1000.0, k.open, k.high, k.low, k.close, k.volume)
                      for k in klines], dtype=float)
    return Bars(arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5], period_sec)


def find_gaps(klines: Sequence[Kline], period_sec: int) -> List[Tuple[int, int]]:
    """연속되지 않은 구간 (앞 봉 open_time, 뒤 봉 open_time)."""
    step = period_sec * 1000
    return [(a.open_time_ms, b.open_time_ms) for a, b in zip(klines, klines[1:])
            if b.open_time_ms - a.open_time_ms != step]


class MarketData:
    def __init__(self, rest: BinanceRestClient):
        self.rest = rest

    async def server_time_ms(self) -> int:
        data = await self.rest.get_public("/dapi/v1/time")
        return int(data["serverTime"])

    async def exchange_info(self) -> Dict[str, Any]:
        return await self.rest.get_public("/dapi/v1/exchangeInfo")

    async def klines(self, symbol: str, interval: str, limit: int = 500,
                     start_ms: Optional[int] = None, end_ms: Optional[int] = None,
                     kind: str = "contract", pair: Optional[str] = None) -> List[Kline]:
        path = KLINE_PATHS[kind]
        params: Dict[str, Any] = {"interval": interval, "limit": min(int(limit), MAX_KLINE_LIMIT),
                                  "startTime": start_ms, "endTime": end_ms}
        if kind == "index":
            params["pair"] = pair or symbol.split("_")[0]
        else:
            params["symbol"] = symbol
        return parse_klines(await self.rest.get_public(path, params))

    async def closed_bars(self, symbol: str, interval: str, count: int,
                          kind: str = "contract", now_ms: Optional[int] = None) -> Bars:
        """최근 count 개의 '마감된' 봉. 마지막(형성 중) 봉은 서버 시각 기준으로 버린다."""
        ks = await self.klines(symbol, interval, limit=count + 1, kind=kind)
        now = self.rest.now_ms() if now_ms is None else now_ms
        ks = closed_only(ks, now)[-count:]
        return klines_to_bars(ks, INTERVAL_SECONDS[interval])

    async def premium_index(self, symbol: str) -> PremiumIndex:
        data = await self.rest.get_public("/dapi/v1/premiumIndex", {"symbol": symbol})
        row = data[0] if isinstance(data, list) else data
        return PremiumIndex(
            symbol=row["symbol"], mark_price=float(row["markPrice"]),
            index_price=float(row["indexPrice"]),
            estimated_settle_price=float(row.get("estimatedSettlePrice") or 0),
            last_funding_rate=float(row.get("lastFundingRate") or 0),
            interest_rate=float(row.get("interestRate") or 0),
            next_funding_time_ms=int(row.get("nextFundingTime") or 0),
            time_ms=int(row.get("time") or 0))

    async def ticker_price(self, symbol: str) -> Tuple[float, int]:
        data = await self.rest.get_public("/dapi/v1/ticker/price", {"symbol": symbol})
        row = data[0] if isinstance(data, list) else data
        return float(row["price"]), int(row.get("time") or 0)

    async def open_interest(self, symbol: str) -> Dict[str, Any]:
        """V1 은 신호에 쓰지 않는다 (향후 확장용 수집만)."""
        return await self.rest.get_public("/dapi/v1/openInterest", {"symbol": symbol})

    async def funding_history(self, symbol: str, start_ms: int,
                              end_ms: Optional[int] = None) -> List[FundingRecord]:
        out: List[FundingRecord] = []
        cursor = int(start_ms)
        end = end_ms if end_ms is not None else self.rest.now_ms()
        while cursor <= end:
            rows = await self.rest.get_public("/dapi/v1/fundingRate",
                                              {"symbol": symbol, "startTime": cursor,
                                               "endTime": end, "limit": 1000})
            if not rows:
                break
            for r in rows:
                mp = r.get("markPrice")
                out.append(FundingRecord(r["symbol"], int(r["fundingTime"]),
                                         float(r["fundingRate"]),
                                         float(mp) if mp not in (None, "") else None))
            last = int(rows[-1]["fundingTime"])
            if last < cursor or len(rows) < 1000:
                break
            cursor = last + 1
        dedup = {f.funding_time_ms: f for f in out}
        return [dedup[k] for k in sorted(dedup)]

    async def history(self, symbol: str, interval: str, start_ms: int, end_ms: int,
                      kind: str = "contract") -> List[Kline]:
        """과거 봉을 앞에서부터 페이지로 모은다 (요청당 최대 1500봉, 최대 200일)."""
        step = INTERVAL_SECONDS[interval] * 1000
        span_bars = min(MAX_KLINE_LIMIT, MAX_KLINE_SPAN_MS // step)
        out: List[Kline] = []
        cursor = int(start_ms)
        while cursor <= end_ms:
            # 창 길이 = span_bars 봉이므로 limit=span_bars 한 번에 창 안의 봉이 전부 온다
            window_end = min(cursor + span_bars * step - 1, end_ms)
            ks = await self.klines(symbol, interval, limit=span_bars, start_ms=cursor,
                                   end_ms=window_end, kind=kind)
            out.extend(ks)
            cursor = window_end + 1
        return parse_klines([(k.open_time_ms, k.open, k.high, k.low, k.close, k.volume,
                              k.close_time_ms, k.base_volume) for k in out])
