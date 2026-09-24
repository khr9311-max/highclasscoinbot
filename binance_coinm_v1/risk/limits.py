"""
계정 단위 위험 한도.

  - 동시 포지션 수 (V1 = 1). 거래소에 실제 포지션이 있으면 로컬 기록과 무관하게 막는다.
  - 일일 손실: UTC 하루 시작 equity 대비 현재 equity(BTC, 미실현 포함)가
    MAX_DAILY_LOSS_PCT 이상 줄면 그날 신규 진입 금지. 보유 포지션의 보호 주문은 그대로.
    (입출금이 있으면 equity 가 흔들린다 - V1 은 입출금 보정 없음, README 에 명시)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..config.settings import Settings


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class RiskLimits:
    def __init__(self, settings: Settings, db: Any, mode: str):
        self.settings = settings
        self.db = db
        self.mode = mode
        self._key = f"{mode}:risk:day"
        self._halted_day: Optional[str] = None

    def day_state(self, equity_btc: float, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        day = utc_day(now)
        st = self.db.kv_get(self._key) or {}
        if st.get("day") != day or not st.get("start_equity_btc"):
            st = {"day": day, "start_equity_btc": float(equity_btc), "set_at": now}
            self.db.kv_set(self._key, st)
        return st

    def daily_pnl(self, equity_btc: float, now: Optional[float] = None) -> Tuple[float, float]:
        st = self.day_state(equity_btc, now)
        start = float(st["start_equity_btc"])
        pnl = equity_btc - start
        return pnl, (pnl / start if start > 0 else 0.0)

    def check_new_entry(self, equity_btc: float, open_positions: int,
                        now: Optional[float] = None) -> Tuple[bool, str]:
        now = time.time() if now is None else now
        if open_positions >= self.settings.max_positions:
            return False, f"동시 포지션 한도 ({open_positions}/{self.settings.max_positions})"
        pnl, pct = self.daily_pnl(equity_btc, now)
        limit = self.settings.max_daily_loss_pct / 100.0
        if pct <= -limit:
            day = utc_day(now)
            if self._halted_day != day:
                self._halted_day = day
                self.db.log_risk_event(self.mode, "daily_loss_limit",
                                       {"day": day, "pnl_btc": pnl, "pnl_pct": pct * 100,
                                        "limit_pct": self.settings.max_daily_loss_pct},
                                       severity="critical")
            return False, f"일일 손실 한도 도달 ({pct * 100:.2f}% <= -{limit * 100:.2f}%)"
        return True, "ok"

    def halted_today(self, now: Optional[float] = None) -> bool:
        return self._halted_day == utc_day(time.time() if now is None else now)
