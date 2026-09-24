"""
사용자 데이터 이벤트 파싱 + 주문 상태 추적 (메시지 순서·중복 처리).

웹소켓은 같은 메시지를 두 번 보내거나 순서를 바꿔 보낼 수 있다. 규칙:
  - 체결(TRADE)은 거래소 체결 id(t) 로 한 번만 센다. 순서가 바뀌어 늦게 와도 체결
    자체는 사실이므로 기록한다.
  - 주문 '상태' 는 되돌리지 않는다: 누적 체결량(z)이 줄어드는 갱신, 종료 상태
    (FILLED/CANCELED/EXPIRED/REJECTED) 이후의 비종료 갱신, 더 오래된 시각(T)의
    갱신은 버린다.
  - 조건부(algo) 주문도 NEW -> TRIGGERING -> TRIGGERED -> FINISHED 순서로만 전진한다.
    발동 후 생긴 일반 주문 id(ai)를 기억해 그 체결을 원래 주문(손절/목표)에 연결한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from ..exchange.models import ALGO_STATUS_MAP, dec, fnum


@dataclass
class OrderUpdate:
    event_time_ms: int
    transaction_time_ms: int
    symbol: str
    client_id: str
    side: str
    order_type: str
    orig_type: str
    exec_type: str
    status: str
    order_id: str
    orig_qty: Decimal
    last_qty: Decimal
    cum_qty: Decimal
    last_price: float
    avg_price: float
    commission: float
    commission_asset: str
    trade_id: str
    realized_pnl: float
    reduce_only: bool
    close_position: bool
    maker: bool
    working_type: str
    stop_price: float

    @property
    def is_trade(self) -> bool:
        return self.exec_type == "TRADE" and self.last_qty > 0


@dataclass
class AlgoUpdate:
    event_time_ms: int
    transaction_time_ms: int
    client_algo_id: str
    algo_id: str
    order_type: str
    symbol: str
    side: str
    status: str
    actual_order_id: Optional[str]
    avg_price: float
    actual_qty: Decimal
    trigger_price: float
    close_position: bool
    reduce_only: bool
    working_type: str
    reject_reason: str


@dataclass
class PositionUpdate:
    symbol: str
    position_amt: Decimal
    entry_price: float
    unrealized_pnl: float
    margin_type: str
    isolated_wallet: float
    position_side: str


@dataclass
class AccountUpdate:
    event_time_ms: int
    transaction_time_ms: int
    reason: str
    balances: Dict[str, Tuple[float, float, float]]      # asset -> (wallet, cross_wallet, change)
    positions: List[PositionUpdate]
    symbol: Optional[str] = None                         # FUNDING_FEE 이벤트의 S 필드


@dataclass
class OtherEvent:
    name: str
    raw: Dict[str, Any]


def _b(v: Any) -> bool:
    return v if isinstance(v, bool) else str(v).lower() == "true"


def parse_user_event(d: Dict[str, Any]) -> Any:
    e = d.get("e")
    if e == "ORDER_TRADE_UPDATE":
        o = d.get("o", {})
        return OrderUpdate(
            event_time_ms=int(d.get("E", 0)), transaction_time_ms=int(d.get("T", 0) or o.get("T", 0)),
            symbol=str(o.get("s", "")), client_id=str(o.get("c", "")), side=str(o.get("S", "")),
            order_type=str(o.get("o", "")), orig_type=str(o.get("ot", o.get("o", ""))),
            exec_type=str(o.get("x", "")), status=str(o.get("X", "")), order_id=str(o.get("i", "")),
            orig_qty=dec(o.get("q")), last_qty=dec(o.get("l")), cum_qty=dec(o.get("z")),
            last_price=fnum(o.get("L")), avg_price=fnum(o.get("ap")), commission=fnum(o.get("n")),
            commission_asset=str(o.get("N") or ""), trade_id=str(o.get("t", "")),
            realized_pnl=fnum(o.get("rp")), reduce_only=_b(o.get("R", False)),
            close_position=_b(o.get("cp", False)), maker=_b(o.get("m", False)),
            working_type=str(o.get("wt", "")), stop_price=fnum(o.get("sp")))
    if e == "ALGO_UPDATE":
        o = d.get("o", {})
        ai = o.get("ai")
        return AlgoUpdate(
            event_time_ms=int(d.get("E", 0)), transaction_time_ms=int(d.get("T", 0)),
            client_algo_id=str(o.get("caid", "")), algo_id=str(o.get("aid", "")),
            order_type=str(o.get("o", "")), symbol=str(o.get("s", "")), side=str(o.get("S", "")),
            status=ALGO_STATUS_MAP.get(str(o.get("X", "")).upper(), str(o.get("X", ""))),
            actual_order_id=str(ai) if ai not in (None, "", 0, "0") else None,
            avg_price=fnum(o.get("ap")), actual_qty=dec(o.get("aq")),
            trigger_price=fnum(o.get("tp")), close_position=_b(o.get("cp", False)),
            reduce_only=_b(o.get("R", False)), working_type=str(o.get("wt", "")),
            reject_reason=str(o.get("rm", "")))
    if e == "ACCOUNT_UPDATE":
        a = d.get("a", {})
        bal = {b.get("a"): (fnum(b.get("wb")), fnum(b.get("cw")), fnum(b.get("bc")))
               for b in a.get("B", [])}
        pos = [PositionUpdate(str(p.get("s", "")), dec(p.get("pa")), fnum(p.get("ep")),
                              fnum(p.get("up")), str(p.get("mt", "")), fnum(p.get("iw")),
                              str(p.get("ps", "BOTH"))) for p in a.get("P", [])]
        return AccountUpdate(int(d.get("E", 0)), int(d.get("T", 0)), str(a.get("m", "")), bal, pos,
                             a.get("S"))
    return OtherEvent(str(e), d)


# ---------------------------------------------------------------------------
_ORDER_RANK = {"NEW": 0, "PARTIALLY_FILLED": 1, "FILLED": 2, "CANCELED": 2, "EXPIRED": 2,
               "REJECTED": 2, "EXPIRED_IN_MATCH": 2, "NEW_INSURANCE": 1, "NEW_ADL": 1}
_ALGO_RANK = {"NEW": 0, "TRIGGERING": 1, "TRIGGERED": 2, "FINISHED": 3, "CANCELED": 3,
              "EXPIRED": 3, "REJECTED": 3}


@dataclass
class TrackedOrder:
    client_id: str
    status: str = "NEW"
    cum_qty: Decimal = Decimal(0)
    update_ms: int = 0
    order_id: Optional[str] = None


@dataclass
class TrackedAlgo:
    client_id: str
    status: str = "NEW"
    update_ms: int = 0
    actual_order_id: Optional[str] = None


class OrderTracker:
    def __init__(self):
        self.orders: Dict[str, TrackedOrder] = {}
        self.algos: Dict[str, TrackedAlgo] = {}
        self.actual_to_algo: Dict[str, str] = {}
        self.seen_trades: Set[Tuple[str, str]] = set()
        self.stale_dropped = 0
        self.dup_trades = 0

    def apply_order(self, u: OrderUpdate) -> Tuple[bool, bool]:
        """(상태 갱신 채택 여부, 새 체결 여부)."""
        new_fill = False
        if u.is_trade:
            key = (u.symbol, u.trade_id)
            if u.trade_id and key in self.seen_trades:
                self.dup_trades += 1
            else:
                if u.trade_id:
                    self.seen_trades.add(key)
                new_fill = True
        t = self.orders.get(u.client_id)
        if t is None:
            t = TrackedOrder(u.client_id, order_id=u.order_id)
            self.orders[u.client_id] = t
            t.status, t.cum_qty, t.update_ms = u.status, u.cum_qty, u.transaction_time_ms
            return True, new_fill
        if u.status == t.status and u.cum_qty == t.cum_qty:
            return False, new_fill                        # 같은 상태의 반복 = 변경 없음
        r_old, r_new = _ORDER_RANK.get(t.status, 0), _ORDER_RANK.get(u.status, 0)
        if (u.cum_qty < t.cum_qty or r_new < r_old or
                (r_old == 2 and r_new == 2 and u.status != t.status) or
                (u.transaction_time_ms and u.transaction_time_ms < t.update_ms and u.cum_qty <= t.cum_qty
                 and r_new <= r_old)):
            self.stale_dropped += 1
            return False, new_fill
        t.status, t.cum_qty = u.status, max(t.cum_qty, u.cum_qty)
        t.update_ms = max(t.update_ms, u.transaction_time_ms)
        return True, new_fill

    def apply_algo(self, u: AlgoUpdate) -> bool:
        if u.actual_order_id:
            self.actual_to_algo[u.actual_order_id] = u.client_algo_id
        t = self.algos.get(u.client_algo_id)
        if t is None:
            self.algos[u.client_algo_id] = TrackedAlgo(u.client_algo_id, u.status,
                                                       u.transaction_time_ms, u.actual_order_id)
            return True
        if _ALGO_RANK.get(u.status, 0) < _ALGO_RANK.get(t.status, 0):
            self.stale_dropped += 1
            return False
        if _ALGO_RANK.get(u.status, 0) == _ALGO_RANK.get(t.status, 0) and u.status == t.status:
            if u.actual_order_id and not t.actual_order_id:
                t.actual_order_id = u.actual_order_id
            return False
        t.status = u.status
        t.update_ms = max(t.update_ms, u.transaction_time_ms)
        t.actual_order_id = t.actual_order_id or u.actual_order_id
        return True

    def algo_for_order(self, order_id: str) -> Optional[str]:
        return self.actual_to_algo.get(str(order_id))

    def status_of(self, client_id: str) -> Optional[str]:
        if client_id in self.orders:
            return self.orders[client_id].status
        if client_id in self.algos:
            return self.algos[client_id].status
        return None
