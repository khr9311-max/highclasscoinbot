"""
계좌 스냅샷 (BTC 기준 회계 + 사람용 USD/KRW 환산).

  wallet_balance_btc    : 지갑 잔고 (격리 증거금 포함)
  available_balance_btc : 새 주문에 쓸 수 있는 잔고
  equity_btc            : 지갑 + 미실현손익 (= margin balance)
  used_margin_btc       : 포지션 초기증거금 + 미체결 주문 증거금
  equity_usd            : equity_btc x 지수가(index price - 현물 기준 BTC 가치)
  equity_krw            : equity_usd x USD/KRW (표시용)

USD/KRW 는 설정 고정값(기본) 또는 업비트 KRW-USDT 시세(공개 API, 키 불필요)를 쓴다.
표시용이며 매매 판단에는 쓰지 않는다.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from ..exchange.contract import ContractSpec
from ..exchange.models import AccountInfo, PositionInfo

logger = logging.getLogger(__name__)


class FxProvider:
    def __init__(self, source: str = "fixed", fixed_rate: float = 1390.0, ttl: float = 600.0):
        self.source = source
        self.fixed_rate = float(fixed_rate)
        self.ttl = ttl
        self._cached: Optional[float] = None
        self._at = 0.0

    async def usd_krw(self) -> float:
        if self.source != "upbit_usdt":
            return self.fixed_rate
        if self._cached and time.time() - self._at < self.ttl:
            return self._cached
        if os.environ.get("COINM_V1_TEST_MODE") == "1":
            return self.fixed_rate
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get("https://api.upbit.com/v1/ticker", params={"markets": "KRW-USDT"}) as r:
                    data = await r.json()
            rate = float(data[0]["trade_price"])
            if 500 < rate < 5000:
                self._cached, self._at = rate, time.time()
                return rate
        except Exception as e:
            logger.debug("USD/KRW 조회 실패 - 고정값 사용: %s", type(e).__name__)
        return self._cached or self.fixed_rate


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
