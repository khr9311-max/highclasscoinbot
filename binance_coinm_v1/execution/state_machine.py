"""
거래 상태 기계.

  IDLE -> SIGNAL_DETECTED -> ENTRY_PENDING -> ENTRY_FILLED -> PROTECTING -> PROTECTED
       -> TP1 -> TP2 -> TP3 -> TRAILING -> CLOSING -> CLOSED
  어느 보유 상태에서든 -> CLOSING (손절 체결·반전 신호·기한·외부 청산)
  이상 상황 -> RECOVERY_REQUIRED (복구기가 거래소 상태를 보고 제자리로 되돌린다)
  복구 불가 -> ERROR (사람 확인 필요, 보호 주문은 거래소에 남겨 둔다)

허용되지 않은 전이는 InvalidTransition 으로 막고 기록한다. 모든 전이는
state_transitions 테이블에 남는다.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

IDLE = "IDLE"
SIGNAL_DETECTED = "SIGNAL_DETECTED"
ENTRY_PENDING = "ENTRY_PENDING"
ENTRY_FILLED = "ENTRY_FILLED"
PROTECTING = "PROTECTING"
PROTECTED = "PROTECTED"
TP1 = "TP1"
TP2 = "TP2"
TP3 = "TP3"
TRAILING = "TRAILING"
CLOSING = "CLOSING"
CLOSED = "CLOSED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
ERROR = "ERROR"

ALL_STATES = (IDLE, SIGNAL_DETECTED, ENTRY_PENDING, ENTRY_FILLED, PROTECTING, PROTECTED,
              TP1, TP2, TP3, TRAILING, CLOSING, CLOSED, RECOVERY_REQUIRED, ERROR)

HOLDING_STATES = (PROTECTED, TP1, TP2, TP3, TRAILING)
POSITION_STATES = (ENTRY_FILLED, PROTECTING) + HOLDING_STATES + (CLOSING,)
TP_STATES = {1: TP1, 2: TP2, 3: TP3}
_PROGRESS = {PROTECTED: 0, TP1: 1, TP2: 2, TP3: 3, TRAILING: 4}

_REC = {PROTECTING, PROTECTED, TP1, TP2, TP3, TRAILING, CLOSING, CLOSED, ERROR, ENTRY_FILLED}
ALLOWED: Dict[str, set] = {
    IDLE: {SIGNAL_DETECTED, RECOVERY_REQUIRED},
    SIGNAL_DETECTED: {ENTRY_PENDING, CLOSED, ERROR},
    ENTRY_PENDING: {ENTRY_FILLED, CLOSED, RECOVERY_REQUIRED, ERROR},
    ENTRY_FILLED: {PROTECTING, CLOSING, RECOVERY_REQUIRED, ERROR},
    PROTECTING: {PROTECTED, CLOSING, RECOVERY_REQUIRED, ERROR},
    PROTECTED: {TP1, TP2, TP3, TRAILING, CLOSING, RECOVERY_REQUIRED, ERROR},
    TP1: {TP2, TP3, TRAILING, CLOSING, RECOVERY_REQUIRED, ERROR},
    TP2: {TP3, TRAILING, CLOSING, RECOVERY_REQUIRED, ERROR},
    TP3: {TRAILING, CLOSING, RECOVERY_REQUIRED, ERROR},
    TRAILING: {CLOSING, RECOVERY_REQUIRED, ERROR},
    CLOSING: {CLOSED, RECOVERY_REQUIRED, ERROR},
    RECOVERY_REQUIRED: _REC,
    ERROR: {RECOVERY_REQUIRED, CLOSING, CLOSED},
    CLOSED: set(),
}


class InvalidTransition(Exception):
    pass


def new_trade_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    mode: str
    direction: int
    pattern: str = "trendy_kangaroo"
    state: str = IDLE
    signal: Optional[Dict[str, Any]] = None
    signal_id: Optional[int] = None
    entry_trigger: Optional[float] = None
    init_stop: Optional[float] = None
    stop_price: Optional[float] = None
    targets: List[float] = field(default_factory=list)
    expire_ts: Optional[float] = None
    created_ts: float = field(default_factory=time.time)
    opened_at: Optional[float] = None
    closed_at: Optional[float] = None
    entry_fill_time_ms: Optional[int] = None
    qty_initial: str = "0"
    qty_open: str = "0"
    entry_avg_price: Optional[float] = None
    tp_plan: List[List[Any]] = field(default_factory=list)       # [[level, qty, price]]
    tp_filled: List[int] = field(default_factory=list)
    logic: Dict[str, Any] = field(default_factory=dict)          # PositionLogic 상태
    last_bar_close_ts: Optional[float] = None
    orders: Dict[str, str] = field(default_factory=dict)         # 역할 -> 현재 clientOrderId
    order_seq: Dict[str, int] = field(default_factory=dict)
    entry_attempts: int = 0
    close_reason: Optional[str] = None
    accounting: Dict[str, float] = field(default_factory=dict)
    sizing: Dict[str, Any] = field(default_factory=dict)
    adopted: bool = False
    equity_at_entry_btc: Optional[float] = None
    error: Optional[str] = None

    # ---- 편의 ----
    @property
    def qty_open_d(self) -> Decimal:
        return Decimal(self.qty_open)

    @property
    def qty_initial_d(self) -> Decimal:
        return Decimal(self.qty_initial)

    @property
    def is_active(self) -> bool:
        return self.state != CLOSED

    @property
    def side_open(self) -> str:
        return "BUY" if self.direction > 0 else "SELL"

    @property
    def side_close(self) -> str:
        return "SELL" if self.direction > 0 else "BUY"

    @property
    def fill_time_s(self) -> float:
        return (self.entry_fill_time_ms or 0) / 1000.0

    def next_seq(self, role: str) -> int:
        n = self.order_seq.get(role, -1) + 1
        self.order_seq[role] = n
        return n

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TradeRecord":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def progress_state(ladder_step: int, tp_filled: List[int], trailing: bool) -> str:
    if trailing:
        return TRAILING
    lvl = max([ladder_step] + [k + 1 for k in tp_filled]) if (tp_filled or ladder_step) else 0
    return TP_STATES.get(min(lvl, 3), PROTECTED) if lvl > 0 else PROTECTED


def is_forward(current: str, target: str) -> bool:
    """보유 상태 사이에서는 앞으로만 간다 (TP2 -> TP1 금지)."""
    if current in _PROGRESS and target in _PROGRESS:
        return _PROGRESS[target] > _PROGRESS[current]
    return True


def check_transition(current: str, target: str) -> None:
    if target not in ALL_STATES:
        raise InvalidTransition(f"알 수 없는 상태: {target}")
    if target not in ALLOWED.get(current, set()):
        raise InvalidTransition(f"허용되지 않은 전이 {current} -> {target}")
