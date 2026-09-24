import os
import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Tuple, Optional

from config import Config

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class RiskDecision:
    """거부 사유를 문자열로 들고 다니기 위한 아주 얇은 결과 객체."""

    __slots__ = ("allowed", "reason")

    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = RiskDecision(True)


class RiskManager:
    """
    주문 전 최종 관문. 원래 코드에 전혀 없던 계층이다.

    - 종목당 / 전체 노출 한도
    - 일일 손실 한도 (초과 시 당일 매매 정지)
    - 일일 주문 건수 상한
    - 동일 종목 재주문 쿨다운 (중복 주문 방지)
    - 업비트 최소 주문금액 검증
    - 매도 시 실제 보유수량 확인

    일일 상태는 디스크에 저장한다. EC2 재시작으로 손실 한도가
    리셋되면 한도 자체가 무의미해지기 때문이다.
    """

    def __init__(self, state_dir: Optional[str] = None):
        self.state_dir = state_dir or Config.STATE_DIR
        os.makedirs(self.state_dir, exist_ok=True)
        self.state_path = os.path.join(self.state_dir, "risk_state.json")

        self.trade_date: str = self._today()
        self.day_start_equity: Optional[float] = None
        self.orders_today: int = 0     # 전체 주문(관측용)
        self.buys_today: int = 0       # 일일 상한이 걸리는 대상
        self.halted: bool = False
        self.halt_reason: str = ""
        self.last_order_ts: Dict[str, float] = {}

        self._load()

    # ---------------- 영속화 ----------------
    def _today(self) -> str:
        return datetime.now(KST).strftime("%Y-%m-%d")

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("trade_date") != self._today():
                logger.info("날짜가 바뀌어 일일 리스크 상태를 초기화합니다.")
                return
            self.trade_date = d["trade_date"]
            self.day_start_equity = d.get("day_start_equity")
            self.orders_today = d.get("orders_today", 0)
            # 구버전 상태파일에는 buys_today 가 없다. 전체 주문 수로
            # 대체하면 실제보다 많게 잡혀 보수적으로 동작한다.
            self.buys_today = d.get("buys_today", self.orders_today)
            self.halted = d.get("halted", False)
            self.halt_reason = d.get("halt_reason", "")
            self.last_order_ts = d.get("last_order_ts", {})
            logger.info(
                "리스크 상태 복원: %s | 매수 %d건 / 전체 %d건 | 정지=%s",
                self.trade_date, self.buys_today, self.orders_today, self.halted,
            )
        except Exception as e:
            logger.error("리스크 상태 로딩 실패(무시하고 새로 시작): %s", e)

    def _save(self):
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "trade_date": self.trade_date,
                    "day_start_equity": self.day_start_equity,
                    "orders_today": self.orders_today,
                "buys_today": self.buys_today,
                    "halted": self.halted,
                    "halt_reason": self.halt_reason,
                    "last_order_ts": self.last_order_ts,
                }, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)   # 원자적 교체
        except Exception as e:
            logger.error("리스크 상태 저장 실패: %s", e)

    # ---------------- 일일 회계 ----------------
    def roll_day_if_needed(self):
        today = self._today()
        if today != self.trade_date:
            logger.info("거래일 전환 %s -> %s. 일일 카운터 초기화.", self.trade_date, today)
            self.trade_date = today
            self.day_start_equity = None
            self.orders_today = 0
            self.buys_today = 0
            self.halted = False
            self.halt_reason = ""
            self._save()

    def update_equity(self, equity_krw: float) -> float:
        """
        현재 총 평가액을 넣으면 당일 손익을 돌려준다.
        손실 한도 초과 시 halt 플래그를 세운다.
        """
        self.roll_day_if_needed()

        if self.day_start_equity is None:
            self.day_start_equity = equity_krw
            self._save()
            logger.info("당일 기준 평가액 설정: %s원", f"{equity_krw:,.0f}")
            return 0.0

        pnl = equity_krw - self.day_start_equity
        if not self.halted and pnl <= -Config.DAILY_LOSS_LIMIT_KRW:
            self.halted = True
            self.halt_reason = (
                f"일일 손실 한도 초과 ({pnl:,.0f}원 <= -{Config.DAILY_LOSS_LIMIT_KRW:,.0f}원)"
            )
            logger.critical("매매 정지: %s", self.halt_reason)
            self._save()
        return pnl

    # ---------------- 사전 점검 ----------------
    def check_buy(
        self,
        ticker: str,
        krw_amount: float,
        position_value_krw: float,
        total_exposure_krw: float,
        available_krw: float,
    ) -> RiskDecision:
        self.roll_day_if_needed()

        if self.halted:
            return RiskDecision(False, f"매매 정지 상태 ({self.halt_reason})")

        if krw_amount < Config.MIN_ORDER_KRW:
            return RiskDecision(
                False, f"최소 주문금액 미달 ({krw_amount:,.0f} < {Config.MIN_ORDER_KRW:,.0f})"
            )

        if krw_amount > available_krw:
            return RiskDecision(
                False, f"KRW 잔고 부족 (필요 {krw_amount:,.0f} / 보유 {available_krw:,.0f})"
            )

        if position_value_krw + krw_amount > Config.MAX_POSITION_KRW:
            return RiskDecision(
                False,
                f"{ticker} 종목 한도 초과 "
                f"({position_value_krw:,.0f}+{krw_amount:,.0f} > {Config.MAX_POSITION_KRW:,.0f})",
            )

        if total_exposure_krw + krw_amount > Config.MAX_TOTAL_EXPOSURE_KRW:
            return RiskDecision(
                False,
                f"전체 노출 한도 초과 "
                f"({total_exposure_krw:,.0f}+{krw_amount:,.0f} > {Config.MAX_TOTAL_EXPOSURE_KRW:,.0f})",
            )

        # 일일 건수 상한은 매수에만 건다. 매도(청산)는 세지도, 막지도 않는다.
        if self.buys_today >= Config.MAX_BUYS_PER_DAY:
            return RiskDecision(
                False, f"일일 매수 건수 상한 도달 ({self.buys_today}/{Config.MAX_BUYS_PER_DAY})"
            )

        return self._check_common(ticker)

    def check_sell(self, ticker: str, volume: float, held_volume: float, price: float,
                   urgent: bool = False) -> RiskDecision:
        """
        urgent=True 는 손절·목표가 청산이다. 재주문 쿨다운을 건너뛴다 -
        매수스톱으로 들어가자마자 급락하면 쿨다운(60초) 동안 손절을 못 한다.
        보유수량/최소금액 검증은 그대로 한다.
        """
        self.roll_day_if_needed()

        if self.halted:
            # 손절/청산은 정지 상태에서도 허용해야 하므로 매도는 막지 않는다.
            logger.warning("매매 정지 상태지만 매도(청산)는 허용합니다.")

        if volume <= 0:
            return RiskDecision(False, "매도 수량이 0 이하")

        if held_volume <= 0:
            return RiskDecision(False, f"{ticker} 미보유 - 매도 불가")

        if volume > held_volume:
            return RiskDecision(
                False, f"보유수량 초과 (요청 {volume:.8f} / 보유 {held_volume:.8f})"
            )

        notional = volume * price
        if notional < Config.MIN_ORDER_KRW:
            return RiskDecision(
                False, f"최소 주문금액 미달 ({notional:,.0f} < {Config.MIN_ORDER_KRW:,.0f})"
            )

        if urgent:
            return ALLOW
        return self._check_common(ticker)

    def _check_common(self, ticker: str) -> RiskDecision:
        """
        매수/매도가 공유하는 게이트. 일일 건수 상한은 여기 없다 - 매수에만
        건다(check_buy 참고). 예전에는 여기 있어서 상한을 다 쓰면 청산까지
        막혔는데, 급락 중에 팔지 못하는 상태가 되므로 위험하다.
        """
        last = self.last_order_ts.get(ticker, 0.0)
        elapsed = time.time() - last
        if elapsed < Config.ORDER_COOLDOWN_SEC:
            return RiskDecision(
                False,
                f"{ticker} 재주문 쿨다운 ({elapsed:.0f}s / {Config.ORDER_COOLDOWN_SEC:.0f}s)",
            )

        return ALLOW

    # ---------------- 주문 후 기록 ----------------
    def register_order(self, ticker: str, side: str = "bid"):
        """
        주문 접수 후 호출. side 는 "bid"(매수) 또는 "ask"(매도).
        일일 상한은 매수만 세므로 매도는 buys_today 를 올리지 않는다.
        """
        self.orders_today += 1
        if side == "bid":
            self.buys_today += 1
        self.last_order_ts[ticker] = time.time()
        self._save()

    def manual_halt(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        self._save()

    def resume(self):
        self.halted = False
        self.halt_reason = ""
        self._save()

    def summary(self) -> str:
        pnl = "미설정" if self.day_start_equity is None else f"기준 {self.day_start_equity:,.0f}원"
        return (
            f"[{self.trade_date}] 매수 {self.buys_today}/{Config.MAX_BUYS_PER_DAY} "
            f"(전체 {self.orders_today}건) · "
            f"{pnl} · 정지={self.halted}"
        )
