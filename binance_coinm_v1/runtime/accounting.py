"""
계좌 스냅샷 (BTC 기준 회계 + 사람용 USD/KRW 환산).

  wallet_balance_btc    : 지갑 잔고 (격리 증거금 포함)
  available_balance_btc : 새 주문에 쓸 수 있는 잔고
  equity_btc            : 지갑 + 미실현손익 (= margin balance)
  used_margin_btc       : 포지션 초기증거금 + 미체결 주문 증거금
  equity_usd            : equity_btc x 지수가(index price - 현물 기준 BTC 가치)
  equity_krw            : equity_usd x USD/KRW (표시용)

원화 환산은 설정 고정값(기본) 또는 업비트 USDT/KRW 참고 시세를 쓴다.
USDT/KRW 사용 시 1 USDT ≈ 1 USD 가정이며 실제 외환 USD/KRW 고시환율은 아니다.
표시용이며 매매 판단에는 쓰지 않는다.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, Optional

from ..exchange.contract import ContractSpec
from ..exchange.models import AccountInfo, PositionInfo

logger = logging.getLogger(__name__)


class FxProvider:
    def __init__(self, source: str = "fixed", fixed_rate: float = 1390.0, ttl: float = 30.0):
        self.source = source
        self.fixed_rate = float(fixed_rate)
        self.ttl = ttl
        self._cached: Optional[float] = None
        self._at = 0.0
        self._quote_at = 0.0
        self._last_attempt = 0.0
        self.last_source = "fixed" if source == "fixed" else "fixed_fallback"

    async def _fetch_quote(self) -> tuple[float, float]:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get("https://api.upbit.com/v1/ticker",
                                   params={"markets": "KRW-USDT"}) as response:
                response.raise_for_status()
                data = await response.json()
        row = data[0]
        if row.get("market") != "KRW-USDT":
            raise ValueError("KRW-USDT ticker missing")
        return float(row["trade_price"]), int(row["timestamp"]) / 1000.0

    async def usd_krw(self) -> float:
        if self.source != "upbit_usdt":
            self.last_source = "fixed"
            return self.fixed_rate
        now = time.time()
        cached_fresh = self._cached is not None and 0 <= now - self._quote_at <= 120
        if cached_fresh and now - self._at < self.ttl:
            self.last_source = "upbit_usdt"
            return self._cached
        if now - self._last_attempt >= self.ttl and os.environ.get("COINM_V1_TEST_MODE") != "1":
            self._last_attempt = now
            try:
                rate, quote_at = await self._fetch_quote()
                if not (math.isfinite(rate) and 500 < rate < 5000 and
                        0 <= now - quote_at <= 120):
                    raise ValueError("stale or invalid KRW-USDT ticker")
                self._cached, self._at, self._quote_at = rate, now, quote_at
                self.last_source = "upbit_usdt"
                return rate
            except Exception as e:
                logger.debug("USDT/KRW 조회 실패 - 설정 환율 사용: %s", type(e).__name__)
        if cached_fresh:
            self.last_source = "upbit_usdt_cached"
            return self._cached
        self.last_source = "fixed_fallback"
        return self.fixed_rate


def build_snapshot(account: AccountInfo, position: Optional[PositionInfo], contract: ContractSpec,
                   mark: Optional[float], index: Optional[float], usd_krw: float,
                   source: str) -> Dict[str, Any]:
    btc = account.asset(contract.margin_asset)
    unreal = btc.unrealized_pnl
    equity = btc.margin_balance if btc.margin_balance else btc.wallet_balance + unreal
    used = btc.position_initial_margin + btc.open_order_initial_margin or btc.initial_margin
    ref = index or mark
    equity_usd = equity * ref if ref else None
    return {
        "ts": time.time(), "wallet_balance_btc": btc.wallet_balance,
        "available_balance_btc": btc.available_balance, "equity_btc": equity,
        "used_margin_btc": used, "unrealized_pnl_btc": unreal, "mark_price": mark,
        "index_price": index, "usd_krw": usd_krw, "equity_usd": equity_usd,
        "equity_krw": equity_usd * usd_krw if equity_usd is not None else None,
        "position_qty": float(position.position_amt) if position else 0.0, "source": source,
    }
