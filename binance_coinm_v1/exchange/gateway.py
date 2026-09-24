"""
거래소 게이트웨이 인터페이스.

실거래(BinanceGateway)와 종이 매매(PaperGateway)가 같은 인터페이스를 구현한다.
실행 계층(진입·보호·청산·복구)은 어느 쪽인지 모른 채 같은 코드로 동작한다 -
종이 매매가 실거래 코드 경로를 그대로 검증하게 하기 위함.

사용자 데이터 이벤트(ORDER_TRADE_UPDATE / ACCOUNT_UPDATE / ALGO_UPDATE)는 바이낸스
웹소켓 원문 형식(dict)으로 event_sink 에 넣는다. 실거래는 UserDataStream 이,
종이 매매는 PaperGateway 가 넣는다. sink 는 동기 함수(큐에 넣기만)여야 한다 -
주문 처리 중 이벤트를 바로 처리하면 재진입 교착이 생긴다.
"""

from __future__ import annotations

import abc
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import AccountInfo, Fill, OrderRequest, OrderState, PositionInfo

EventSink = Callable[[Dict[str, Any]], None]


class ExchangeGateway(abc.ABC):
    mode: str = "paper"

    def __init__(self):
        self.event_sink: Optional[EventSink] = None

    def set_event_sink(self, sink: Optional[EventSink]) -> None:
        self.event_sink = sink

    # ---- 계정 ----
    @abc.abstractmethod
    async def get_account(self) -> AccountInfo: ...

    @abc.abstractmethod
    async def get_positions(self, symbol: str) -> List[PositionInfo]: ...

    async def get_position(self, symbol: str) -> Optional[PositionInfo]:
        for p in await self.get_positions(symbol):
            if p.symbol == symbol and p.position_side in ("BOTH", ""):
                return p
        return None

    @abc.abstractmethod
    async def get_position_mode(self) -> bool:
        """True = 헤지 모드(dualSidePosition)."""

    @abc.abstractmethod
    async def get_commission_rate(self, symbol: str) -> Tuple[float, float]:
        """(maker, taker)"""

    @abc.abstractmethod
    async def get_leverage_brackets(self, symbol: str) -> List[Dict[str, Any]]: ...

    # ---- 주문 조회 ----
    @abc.abstractmethod
    async def get_open_orders(self, symbol: str) -> List[OrderState]: ...

    @abc.abstractmethod
    async def get_open_algo_orders(self, symbol: str) -> List[OrderState]: ...

    @abc.abstractmethod
    async def get_order(self, symbol: str, client_id: str) -> OrderState:
        """없으면 status=NOT_FOUND (예외 아님)."""

    @abc.abstractmethod
    async def get_algo_order(self, symbol: str, client_id: str) -> OrderState: ...

    @abc.abstractmethod
    async def get_user_trades(self, symbol: str, start_ms: Optional[int] = None,
                              order_id: Optional[str] = None) -> List[Fill]: ...

    @abc.abstractmethod
    async def get_income(self, symbol: str, income_type: Optional[str] = None,
                         start_ms: Optional[int] = None) -> List[Dict[str, Any]]: ...

    # ---- 주문 변경 (실거래는 LiveOrderGate 통과 필수) ----
    @abc.abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderState:
        """
        결과를 모르면 OrderStatusUnknown 을 낸다. 호출부는 get_order/get_algo_order 로
        확정하기 전에는 같은 주문을 다시 보내면 안 된다.
        """

    @abc.abstractmethod
    async def cancel_order(self, symbol: str, client_id: str, is_algo: bool) -> OrderState: ...

    @abc.abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...

    @abc.abstractmethod
    async def set_margin_type(self, symbol: str, margin_type: str) -> None: ...

    async def close(self) -> None:
        pass

    def emit(self, event: Dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(event)
