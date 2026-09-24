"""
거래소 공통 데이터 모델. 실거래 게이트웨이와 종이 게이트웨이가 같은 모델을 돌려준다.

수량은 '계약 수' (Decimal), 가격은 float(USD), BTC 금액은 float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

CONDITIONAL_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT",
                     "TRAILING_STOP_MARKET"}

# 정규화된 주문 상태
OPEN_STATUSES = {"NEW", "PARTIALLY_FILLED", "TRIGGERING"}
TERMINAL_STATUSES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "FINISHED", "NOT_FOUND",
                     "EXPIRED_IN_MATCH"}

ALGO_STATUS_MAP = {"NEW": "NEW", "ACTIVE": "NEW", "TRIGGERING": "TRIGGERING",
                   "TRIGGERED": "TRIGGERED", "FINISHED": "FINISHED", "CANCELED": "CANCELED",
                   "CANCELLED": "CANCELED", "REJECTED": "REJECTED", "EXPIRED": "EXPIRED"}


def dec(v: Any, default: str = "0") -> Decimal:
    if v in (None, ""):
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(repr(v))
    return Decimal(str(v))


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass
class OrderRequest:
    client_id: str
    symbol: str
    side: str                                   # BUY / SELL
    order_type: str                             # MARKET / STOP_MARKET / TAKE_PROFIT_MARKET ...
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    reduce_only: bool = False
    close_position: bool = False
    working_type: Optional[str] = None
    price_protect: bool = False
    time_in_force: Optional[str] = None
    purpose: str = ""
    trade_id: Optional[str] = None

    @property
    def is_conditional(self) -> bool:
        return self.order_type in CONDITIONAL_TYPES

    def validate(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side 오류: {self.side}")
        if not self.client_id or len(self.client_id) > 36:
            raise ValueError("clientOrderId 는 1~36자")
        if self.close_position:
            if self.order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                raise ValueError("closePosition 은 STOP_MARKET/TAKE_PROFIT_MARKET 전용")
            if self.quantity is not None or self.reduce_only:
                raise ValueError("closePosition 은 quantity/reduceOnly 와 함께 쓸 수 없음")
        elif self.quantity is None or self.quantity <= 0:
            raise ValueError("수량 필요")
        if self.is_conditional and self.trigger_price is None:
            raise ValueError("조건부 주문은 triggerPrice 필요")

    def to_dict(self) -> Dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


@dataclass
class OrderState:
    client_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    is_algo: bool = False
    exchange_id: Optional[str] = None           # orderId 또는 algoId
    orig_qty: Decimal = Decimal(0)
    executed_qty: Decimal = Decimal(0)
    avg_price: Optional[float] = None
    trigger_price: Optional[float] = None
    reduce_only: bool = False
    close_position: bool = False
    working_type: Optional[str] = None
    actual_order_id: Optional[str] = None       # 조건부 주문이 발동해 생긴 일반 주문 id
    update_time_ms: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def found(self) -> bool:
        return self.status != "NOT_FOUND"

    @classmethod
    def not_found(cls, client_id: str, symbol: str = "", is_algo: bool = False) -> "OrderState":
        return cls(client_id, symbol, "", "", "NOT_FOUND", is_algo=is_algo)


@dataclass
class PositionInfo:
    symbol: str
    position_amt: Decimal                        # 부호 있는 계약 수 (+롱, -숏)
    entry_price: float
    mark_price: float
    unrealized_pnl_btc: float
    liquidation_price: float
    leverage: int
    margin_type: str                             # isolated / cross
    isolated_margin_btc: float
    position_side: str = "BOTH"
    update_time_ms: int = 0
    break_even_price: Optional[float] = None

    @property
    def direction(self) -> int:
        return 1 if self.position_amt > 0 else (-1 if self.position_amt < 0 else 0)

    @property
    def qty(self) -> Decimal:
        return abs(self.position_amt)


@dataclass
class AssetBalance:
    asset: str
    wallet_balance: float
    unrealized_pnl: float
    margin_balance: float
    available_balance: float
    initial_margin: float = 0.0
    position_initial_margin: float = 0.0
    open_order_initial_margin: float = 0.0
    maint_margin: float = 0.0
    max_withdraw: float = 0.0
    cross_wallet_balance: float = 0.0


@dataclass
class AccountInfo:
    assets: Dict[str, AssetBalance]
    positions: List[PositionInfo]
    can_trade: bool = True
    fee_tier: int = 0
    update_time_ms: int = 0

    def asset(self, name: str) -> AssetBalance:
        return self.assets.get(name) or AssetBalance(name, 0.0, 0.0, 0.0, 0.0)


@dataclass
class Fill:
    symbol: str
    trade_id: str
    order_id: str
    side: str
    price: float
    qty: Decimal
    realized_pnl_btc: float
    commission: float
    commission_asset: str
    time_ms: int
    maker: bool = False
    client_id: Optional[str] = None
