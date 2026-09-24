"""
LiveOrderGate - 실제 계정으로 나가는 모든 변경 요청(주문·취소·레버리지·마진 타입)의 관문.

기본값은 닫힘. 실거래(real money) 주문은 아래가 '전부' 참일 때만 열린다:
  1. BINANCE_ENV=live
  2. EXECUTION_MODE=live
  3. LIVE_TRADING_ENABLED=true
  4. LIVE_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING
  5. 전송 대상이 실거래 호스트(dapi.binance.com)
  6. 바이낸스 전용 검증 게이트 통과 (validation/gate.py - 업비트 검증 결과는 무관)

하나라도 빠지면 LiveOrderBlocked 를 내고 요청은 전송되지 않는다.
REST 클라이언트의 mutation_guard 로 걸려 있으므로 상위 코드에 버그가 있어도
서명된 변경 요청은 이 관문을 우회할 수 없다.

6번(검증 게이트)은 '위험을 늘리는' 요청(신규 진입, 레버리지·마진 타입 변경)에만
적용한다. 이미 보유한 포지션을 지키거나 줄이는 요청(reduceOnly, closePosition 손절,
주문 취소)은 1~5번만 본다. 검증 리포트가 만료돼 게이트가 닫혔다고 보유 포지션의
보호 손절까지 막히면 포지션이 무방비가 되기 때문이다. 1~5번 중 하나라도 빠지면
보호 주문을 포함한 모든 요청이 거부된다 (그때는 텔레그램으로 수동 관리 필요를 알린다).

테스트넷(EXECUTION_MODE=testnet)은 가짜 돈이다. BINANCE_ENV=testnet 이고 전송
대상이 테스트넷 호스트일 때만 허용한다 (실거래 호스트로는 절대 보내지 않는다).
종이 매매(paper)는 거래소로 주문을 보내지 않으므로 이 관문을 쓸 일이 없다 -
혹시 불리면 무조건 거부한다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.settings import LIVE_CONFIRMATION_PHRASE, Settings
from ..exchange.errors import LiveOrderBlocked
from ..exchange.rest_client import LIVE_REST, TESTNET_REST

logger = logging.getLogger(__name__)

ValidationCheck = Callable[[], Tuple[bool, str]]


def _no_validation() -> Tuple[bool, str]:
    return False, "바이낸스 검증 게이트 미연결"


class LiveOrderGate:
    def __init__(self, settings: Settings, base_url: str,
                 validation_check: Optional[ValidationCheck] = None,
                 on_block: Optional[Callable[[str, List[str]], None]] = None):
        self.settings = settings
        self.base_url = base_url.rstrip("/")
        self.validation_check = validation_check or _no_validation
        self.on_block = on_block
        self.blocked_count = 0

    # ------------------------------------------------------------------
    @staticmethod
    def is_risk_increasing(method: str, path: str, params: Optional[Dict[str, Any]]) -> bool:
        p = params or {}
        if method == "DELETE":
            return False                                   # 취소
        if path in ("/dapi/v1/order", "/dapi/v1/algoOrder", "/dapi/v1/batchOrders"):
            ro = str(p.get("reduceOnly", "")).lower() in ("true", "1")
            cp = str(p.get("closePosition", "")).lower() in ("true", "1")
            return not (ro or cp)
        return True                                        # 레버리지·마진 타입 등 계정 변경

    def reasons(self, risk_increasing: bool = True) -> List[str]:
        """닫혀 있는 이유 목록 (비어 있으면 열림)."""
        s = self.settings
        out: List[str] = []
        mode = s.execution_mode
        if mode == "paper":
            return ["EXECUTION_MODE=paper (종이 매매는 거래소로 주문을 보내지 않음)"]
        if mode == "testnet":
            if s.binance_env != "testnet":
                out.append("BINANCE_ENV!=testnet")
            if self.base_url != TESTNET_REST:
                out.append("전송 대상이 테스트넷 호스트가 아님")
            return out
        if mode != "live":
            return [f"알 수 없는 EXECUTION_MODE={mode}"]
        if s.binance_env != "live":
            out.append("BINANCE_ENV!=live")
        if not s.live_trading_enabled:
            out.append("LIVE_TRADING_ENABLED!=true")
        if s.live_confirmation != LIVE_CONFIRMATION_PHRASE:
            out.append("LIVE_CONFIRMATION 불일치")
        if self.base_url != LIVE_REST:
            out.append("전송 대상이 실거래 호스트가 아님")
        if not out and risk_increasing:
            # 설정 조건이 다 맞을 때만 검증 게이트를 본다 (비용 절약 + 사유 명확화)
            try:
                ok, why = self.validation_check()
            except Exception as e:           # 검증 확인 실패 = 닫힘
                ok, why = False, f"검증 게이트 확인 실패: {e}"
            if not ok:
                out.append(f"검증 게이트: {why}")
        return out

    def is_open(self) -> bool:
        return not self.reasons()

    def status(self) -> Dict[str, Any]:
        r = self.reasons()
        return {"open": not r, "reasons": r, "execution_mode": self.settings.execution_mode,
                "binance_env": self.settings.binance_env,
                "real_money": self.settings.execution_mode == "live" and not r}

    def protective_open(self) -> bool:
        """보유 포지션 보호·축소 주문을 보낼 수 있는가 (검증 게이트 제외 조건)."""
        return not self.reasons(risk_increasing=False)

    def check(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> None:
        """REST mutation_guard. 닫혀 있으면 예외 - 요청은 전송되지 않는다."""
        r = self.reasons(self.is_risk_increasing(method.upper(), path, params))
        if r:
            self.blocked_count += 1
            logger.warning("LiveOrderGate 차단 %s %s: %s", method, path, "; ".join(r))
            if self.on_block:
                try:
                    self.on_block(f"{method} {path}", r)
                except Exception:
                    pass
            raise LiveOrderBlocked(f"주문 거부 ({method} {path}): " + "; ".join(r))
