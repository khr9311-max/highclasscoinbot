"""
종이 매매 게이트웨이 - 바이낸스 COIN-M 격리·원웨이 계정을 흉내낸다.

실거래 게이트웨이와 같은 인터페이스, 같은 사용자 이벤트 형식(웹소켓 원문 dict)을
쓴다. 그래서 진입·보호·청산·복구 코드가 종이 매매에서 실거래와 같은 경로로 돈다.

흉내내는 것:
  - 시장가: 최신 체결가 ± 슬리피지, 테이커 수수료(BTC), 인버스 손익, 격리 증거금
  - 조건부(algo) 주문: STOP_MARKET/TAKE_PROFIT_MARKET, MARK/CONTRACT 트리거,
    closePosition, reduceOnly, 즉시 발동 가격이면 -2021 거부
  - reduceOnly 가 포지션을 늘리거나 뒤집는 주문이면 -2022 거부
  - 펀딩(8시간마다 마크가·펀딩비율로), 청산가 도달 시 강제청산
  - 주문 응답에 avgPrice 없음 (2026-06 통합 이후 실거래와 같게) -> 조회로 확인해야 함
  - 장애 주입: 타임아웃·응답 유실(실행은 됐는데 응답만 잃음)·거부·부분 체결

거래소로는 아무것도 보내지 않는다.
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.settings import Settings
from ..risk import inverse_math as im
from .contract import ContractSpec
from .errors import BinanceAPIError, OrderStatusUnknown, outcome_unknown
from .gateway import ExchangeGateway
from .models import (AccountInfo, AssetBalance, Fill, OrderRequest, OrderState, PositionInfo,
                     dec)

logger = logging.getLogger(__name__)

FUNDING_INTERVAL_MS = 8 * 3600 * 1000


@dataclass
class Fault:
    """장애 주입. when='before': 효과 없이 예외. when='after': 실행 후 응답만 유실."""
    op: str
    exc: Exception
    when: str = "before"
    match: Optional[str] = None          # clientOrderId 부분 문자열
    purpose: Optional[str] = None
    times: int = 1


def _sgn(x: Decimal) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


class PaperGateway(ExchangeGateway):
    mode = "paper"

    def __init__(self, spec: ContractSpec, settings: Settings,
                 clock: Callable[[], float] = time.time,
                 start_equity_btc: Optional[float] = None):
        super().__init__()
        self.spec = spec
        self.settings = settings
        self.clock = clock
        self.wallet = float(start_equity_btc if start_equity_btc is not None
                            else settings.paper_start_equity_btc)
        self.pos_qty = Decimal(0)
        self.pos_entry = 0.0
        self.pos_margin = 0.0
        self.leverage = int(settings.leverage)
        self.margin_type = "isolated"
        self.maker_rate = settings.maker_fee_rate
        self.taker_rate = settings.taker_fee_rate
        self.slip_bps = settings.slippage_bps
        self.stop_slip_bps = settings.stop_slippage_bps
        self.last: Optional[float] = None
        self.mark: Optional[float] = None
        self.index: Optional[float] = None
        self.market_ts_ms = 0
        self.funding_rate = 0.0
        self.next_funding_ms: Optional[int] = None
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.algos: Dict[str, Dict[str, Any]] = {}
        self.fills: List[Fill] = []
        self.income: List[Dict[str, Any]] = []
        self._oid = 1_000
        self._tid = 50_000
        self._aid = 900_000
        self.faults: List[Fault] = []
        self.partial_next: Optional[float] = None
        self.api_calls: List[str] = []

    # ------------------------------------------------------------------ 시각·주입
    def now_ms(self) -> int:
        return self.market_ts_ms or int(self.clock() * 1000)

    def inject(self, fault: Fault) -> None:
        self.faults.append(fault)

    def _fault(self, op: str, when: str, client_id: str = "", purpose: str = "") -> Optional[Exception]:
        for f in self.faults:
            if f.op != op or f.when != when or f.times <= 0:
                continue
            if f.match and f.match not in client_id:
                continue
            if f.purpose and f.purpose != purpose:
                continue
            f.times -= 1
            return f.exc
        return None

    def _read_fault(self, op: str, client_id: str = "") -> None:
        self.api_calls.append(op)
        exc = self._fault(op, "before", client_id)
        if exc is not None:
            raise exc

    # ------------------------------------------------------------------ 시세
    def update_market(self, last: Optional[float] = None, mark: Optional[float] = None,
                      index: Optional[float] = None, ts_ms: Optional[int] = None,
                      funding_rate: Optional[float] = None,
                      next_funding_ms: Optional[int] = None) -> None:
        if last is not None:
            self.last = float(last)
        if mark is not None:
            self.mark = float(mark)
        if index is not None:
            self.index = float(index)
        if self.mark is None and self.last is not None:
            self.mark = self.last
        if ts_ms is not None:
            self.market_ts_ms = max(self.market_ts_ms, int(ts_ms))
        if funding_rate is not None:
            self.funding_rate = float(funding_rate)
        if next_funding_ms is not None and self.next_funding_ms is None:
            self.next_funding_ms = int(next_funding_ms)     # 이후는 8시간씩 스스로 진행
        self._process_funding()
        self._check_algos()
        self._check_liquidation()

    def _price_for(self, working_type: Optional[str]) -> Optional[float]:
        return self.mark if working_type == "MARK_PRICE" else self.last

    # ------------------------------------------------------------------ 계정 조회
    def _unrealized(self) -> float:
        if self.pos_qty == 0 or not self.mark:
            return 0.0
        return im.pnl_btc(_sgn(self.pos_qty), abs(self.pos_qty), self.spec.contract_size,
                          self.pos_entry, self.mark)

    def _liq_price(self) -> float:
        if self.pos_qty == 0:
            return 0.0
        return im.liquidation_price(_sgn(self.pos_qty), abs(self.pos_qty), self.spec.contract_size,
                                    self.pos_entry, self.pos_margin, self.spec.maint_margin_rate)

    def _position(self) -> PositionInfo:
        return PositionInfo(
            symbol=self.spec.symbol, position_amt=self.pos_qty, entry_price=self.pos_entry,
            mark_price=self.mark or 0.0, unrealized_pnl_btc=self._unrealized(),
            liquidation_price=self._liq_price(), leverage=self.leverage,
            margin_type=self.margin_type, isolated_margin_btc=self.pos_margin,
            position_side="BOTH", update_time_ms=self.now_ms())

    def _asset(self) -> AssetBalance:
        up = self._unrealized()
        return AssetBalance(
            asset="BTC", wallet_balance=self.wallet, unrealized_pnl=up,
            margin_balance=self.wallet + up, available_balance=max(0.0, self.wallet - self.pos_margin),
            initial_margin=self.pos_margin, position_initial_margin=self.pos_margin,
            open_order_initial_margin=0.0,
            maint_margin=(im.notional_btc(abs(self.pos_qty), self.spec.contract_size, self.mark)
                          * self.spec.maint_margin_rate if self.pos_qty and self.mark else 0.0),
            max_withdraw=max(0.0, self.wallet - self.pos_margin),
            cross_wallet_balance=self.wallet - self.pos_margin)

    async def get_account(self) -> AccountInfo:
        self._read_fault("get_account")
        return AccountInfo(assets={"BTC": self._asset()}, positions=[self._position()],
                           can_trade=True, update_time_ms=self.now_ms())

    async def get_balance(self, asset: str = "BTC") -> AssetBalance:
        self._read_fault("get_balance")
        return self._asset() if asset == "BTC" else AssetBalance(asset, 0.0, 0.0, 0.0, 0.0)

    async def get_positions(self, symbol: str) -> List[PositionInfo]:
        self._read_fault("get_positions")
        return [self._position()] if symbol == self.spec.symbol else []

    async def get_position_mode(self) -> bool:
        self._read_fault("get_position_mode")
        return False

    async def get_commission_rate(self, symbol: str) -> Tuple[float, float]:
        return self.maker_rate, self.taker_rate

    async def get_leverage_brackets(self, symbol: str) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------------ 주문 조회
    def _order_state(self, o: Dict[str, Any]) -> OrderState:
        return OrderState(
            client_id=o["client_id"], symbol=o["symbol"], side=o["side"], order_type=o["type"],
            status=o["status"], is_algo=False, exchange_id=str(o["order_id"]),
            orig_qty=o["qty"], executed_qty=o["executed"],
            avg_price=(o["notional_inv_qty"] and float(o["executed"]) / o["notional_inv_qty"]) or None,
            reduce_only=o["reduce_only"], close_position=o.get("close_position", False),
            update_time_ms=o["update_ms"], raw={"paper": True})

    def _algo_state(self, a: Dict[str, Any]) -> OrderState:
        return OrderState(
            client_id=a["client_id"], symbol=a["symbol"], side=a["side"], order_type=a["type"],
            status=a["status"], is_algo=True, exchange_id=str(a["algo_id"]),
            orig_qty=a["qty"] if a["qty"] is not None else Decimal(0),
            executed_qty=a["actual_qty"], avg_price=a["avg_price"] or None,
            trigger_price=float(a["trigger"]), reduce_only=a["reduce_only"],
            close_position=a["close_position"], working_type=a["working_type"],
            actual_order_id=a["actual_order_id"], update_time_ms=a["update_ms"],
            raw={"paper": True})

    async def get_open_orders(self, symbol: str) -> List[OrderState]:
        self._read_fault("get_open_orders")
        return [self._order_state(o) for o in self.orders.values()
                if o["symbol"] == symbol and o["status"] in ("NEW", "PARTIALLY_FILLED")]

    async def get_open_algo_orders(self, symbol: str) -> List[OrderState]:
        self._read_fault("get_open_algo_orders")
        return [self._algo_state(a) for a in self.algos.values()
                if a["symbol"] == symbol and a["status"] in ("NEW", "TRIGGERING")]

    async def get_order(self, symbol: str, client_id: str) -> OrderState:
        self._read_fault("get_order", client_id)
        o = self.orders.get(client_id)
        return self._order_state(o) if o else OrderState.not_found(client_id, symbol)

    async def get_algo_order(self, symbol: str, client_id: str) -> OrderState:
        self._read_fault("get_algo_order", client_id)
        a = self.algos.get(client_id)
        return self._algo_state(a) if a else OrderState.not_found(client_id, symbol, is_algo=True)

    async def get_user_trades(self, symbol: str, start_ms: Optional[int] = None,
                              order_id: Optional[str] = None) -> List[Fill]:
        self._read_fault("get_user_trades")
        out = [f for f in self.fills if f.symbol == symbol]
        if order_id is not None:
            out = [f for f in out if f.order_id == str(order_id)]
        if start_ms is not None:
            out = [f for f in out if f.time_ms >= start_ms]
        return out

    async def get_income(self, symbol: str, income_type: Optional[str] = None,
                         start_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        self._read_fault("get_income")
        out = [r for r in self.income if r["symbol"] == symbol]
        if income_type:
            out = [r for r in out if r["incomeType"] == income_type]
        if start_ms is not None:
            out = [r for r in out if r["time"] >= start_ms]
        return out

    # ------------------------------------------------------------------ 주문 변경
    async def place_order(self, req: OrderRequest) -> OrderState:
        req.validate()
        self.api_calls.append(f"place:{req.client_id}")
        exc = self._fault("place", "before", req.client_id, req.purpose)
        if exc is not None:
            if outcome_unknown(exc):
                raise OrderStatusUnknown(req.client_id, exc)
            raise exc
        if req.is_conditional:
            existing = self.algos.get(req.client_id)
            if existing and existing["status"] in ("NEW", "TRIGGERING"):
                raise BinanceAPIError(400, -4116, "ClientOrderId is duplicated.", "/dapi/v1/algoOrder")
            state = self._place_algo(req)
        else:
            existing = self.orders.get(req.client_id)
            if existing and existing["status"] in ("NEW", "PARTIALLY_FILLED"):
                raise BinanceAPIError(400, -4116, "ClientOrderId is duplicated.", "/dapi/v1/order")
            state = self._place_market(req)
        exc = self._fault("place", "after", req.client_id, req.purpose)
        if exc is not None:
            raise OrderStatusUnknown(req.client_id, exc)      # 실행됐지만 응답 유실
        return state

    async def cancel_order(self, symbol: str, client_id: str, is_algo: bool) -> OrderState:
        self.api_calls.append(f"cancel:{client_id}")
        exc = self._fault("cancel", "before", client_id)
        if exc is not None:
            if outcome_unknown(exc):
                raise OrderStatusUnknown(client_id, exc)
            raise exc
        if is_algo:
            a = self.algos.get(client_id)
            if a is None:
                return OrderState.not_found(client_id, symbol, is_algo=True)
            if a["status"] == "NEW":
                a["status"] = "CANCELED"
                a["update_ms"] = self.now_ms()
                self._emit_algo(a)
            state = self._algo_state(a)
        else:
            o = self.orders.get(client_id)
            if o is None:
                return OrderState.not_found(client_id, symbol)
            if o["status"] in ("NEW", "PARTIALLY_FILLED"):
                o["status"] = "CANCELED"
                o["update_ms"] = self.now_ms()
                self._emit_order(o, "CANCELED")
            state = self._order_state(o)
        exc = self._fault("cancel", "after", client_id)
        if exc is not None:
            raise OrderStatusUnknown(client_id, exc)
        return state

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        if self.pos_qty != 0 and leverage > self.leverage:
            raise BinanceAPIError(400, -2027, "Exceeded the maximum allowable position at current leverage.")
        self.leverage = int(leverage)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        if self.pos_qty != 0:
            raise BinanceAPIError(400, -4048, "Margin type cannot be changed if there exists position.")
        self.margin_type = margin_type.lower().replace("crossed", "cross")

    # ------------------------------------------------------------------ 체결 엔진
    def _next(self, attr: str) -> int:
        v = getattr(self, attr) + 1
        setattr(self, attr, v)
        return v

    def _place_market(self, req: OrderRequest) -> OrderState:
        if req.order_type != "MARKET":
            raise BinanceAPIError(400, -1116, f"paper: 지원하지 않는 주문 유형 {req.order_type}")
        if not self.last:
            raise BinanceAPIError(400, -1000, "paper: 시세 없음")
        ok, why = self.spec.check_qty(req.quantity, market=True)
        if not ok:
            raise BinanceAPIError(400, -4003, f"Quantity invalid: {why}")
        side_dir = 1 if req.side == "BUY" else -1
        qty = Decimal(req.quantity)
        if req.reduce_only:
            if self.pos_qty == 0 or _sgn(self.pos_qty) == side_dir:
                raise BinanceAPIError(400, -2022, "ReduceOnly Order is rejected.", "/dapi/v1/order")
            qty = min(qty, abs(self.pos_qty))
        else:
            opening = qty if _sgn(self.pos_qty) in (0, side_dir) else max(Decimal(0), qty - abs(self.pos_qty))
            if opening > 0:
                need = im.initial_margin_btc(opening, self.spec.contract_size, self.last, self.leverage)
                need += im.fee_btc(opening, self.spec.contract_size, self.last, self.taker_rate)
                if need > self.wallet - self.pos_margin + 1e-12:
                    raise BinanceAPIError(400, -2019, "Margin is insufficient.", "/dapi/v1/order")
        # reduceOnly 수량이 포지션보다 크면 포지션 크기로 줄어든다
        o = {"client_id": req.client_id, "symbol": req.symbol, "side": req.side, "type": "MARKET",
             "order_id": self._next("_oid"), "qty": qty, "executed": Decimal(0),
             "notional_inv_qty": 0.0, "status": "NEW", "reduce_only": req.reduce_only,
             "close_position": False, "update_ms": self.now_ms(), "working_type": None,
             "orig_type": "MARKET", "stop_price": 0.0}
        self.orders[req.client_id] = o
        self._emit_order(o, "NEW")
        fill_qty = qty
        if self.partial_next is not None:
            fill_qty = (qty * Decimal(repr(self.partial_next))).to_integral_value(rounding=ROUND_DOWN)
            fill_qty = self.spec.round_qty_down(fill_qty)
            self.partial_next = None
        px = float(self.spec.round_price(im.price_with_slippage(self.last, req.side, self.slip_bps)))
        if fill_qty > 0:
            self._apply_trade(o, side_dir, fill_qty, px, maker=False)
        if o["executed"] < o["qty"]:
            o["status"] = "EXPIRED"
            o["update_ms"] = self.now_ms()
            self._emit_order(o, "EXPIRED")
        # 2026-06 통합 이후 응답에는 avgPrice 가 없다 -> 조회로 확인하게 만든다
        st = self._order_state(o)
        st.avg_price = None
        return st

    def _apply_trade(self, o: Dict[str, Any], side_dir: int, qty: Decimal, px: float,
                     maker: bool, liquidation: bool = False) -> None:
        cs = self.spec.contract_size
        pos = self.pos_qty
        realized = 0.0
        rate = self.maker_rate if maker else self.taker_rate
        fee = 0.0 if liquidation else im.fee_btc(qty, cs, px, rate)
        remaining = qty
        if pos != 0 and _sgn(pos) != side_dir:
            close_qty = min(remaining, abs(pos))
            d = _sgn(pos)
            realized = im.pnl_btc(d, close_qty, cs, self.pos_entry, px)
            released = self.pos_margin * float(close_qty / abs(pos))
            self.pos_margin -= released
            self.pos_qty = pos + side_dir * close_qty
            self.wallet += realized
            remaining -= close_qty
            if self.pos_qty == 0:
                self.pos_entry = 0.0
                self.pos_margin = 0.0
        if remaining > 0:
            self.pos_entry = im.avg_entry_price(abs(self.pos_qty), self.pos_entry, remaining, px) \
                if self.pos_qty != 0 else px
            self.pos_qty = self.pos_qty + side_dir * remaining
            self.pos_margin += im.initial_margin_btc(remaining, cs, px, self.leverage)
        self.wallet -= fee
        o["executed"] += qty
        o["notional_inv_qty"] += float(qty) / px
        o["status"] = "FILLED" if o["executed"] >= o["qty"] else "PARTIALLY_FILLED"
        o["update_ms"] = self.now_ms()
        tid = self._next("_tid")
        fill = Fill(symbol=o["symbol"], trade_id=str(tid), order_id=str(o["order_id"]),
                    side=o["side"], price=px, qty=qty, realized_pnl_btc=realized,
                    commission=fee, commission_asset="BTC", time_ms=self.now_ms(), maker=maker,
                    client_id=o["client_id"])
        self.fills.append(fill)
        if realized:
            self.income.append({"symbol": o["symbol"], "incomeType": "REALIZED_PNL",
                                "income": f"{realized:.8f}", "asset": "BTC",
                                "time": self.now_ms(), "tranId": tid, "tradeId": str(tid)})
        if fee:
            self.income.append({"symbol": o["symbol"], "incomeType": "COMMISSION",
                                "income": f"{-fee:.8f}", "asset": "BTC",
                                "time": self.now_ms(), "tranId": tid, "tradeId": str(tid)})
        self._emit_order(o, "TRADE", last_qty=qty, last_px=px, fee=fee, realized=realized,
                         trade_id=tid, maker=maker)
        self._emit_account("ORDER")

    def _place_algo(self, req: OrderRequest) -> OrderState:
        if req.order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            raise BinanceAPIError(400, -1116, f"paper: 지원하지 않는 조건부 유형 {req.order_type}")
        ok, why = self.spec.check_price(req.trigger_price)
        if not ok:
            raise BinanceAPIError(400, -4014, f"Price not increased by tick size: {why}")
        if req.quantity is not None:
            ok, why = self.spec.check_qty(req.quantity, market=True)
            if not ok:
                raise BinanceAPIError(400, -4003, f"Quantity invalid: {why}")
        ref = self._price_for(req.working_type)
        trig = float(req.trigger_price)
        if ref is not None and self._would_trigger(req.order_type, req.side, trig, ref):
            raise BinanceAPIError(400, -2021, "Order would immediately trigger.", "/dapi/v1/algoOrder")
        a = {"client_id": req.client_id, "symbol": req.symbol, "side": req.side,
             "type": req.order_type, "algo_id": self._next("_aid"), "trigger": trig,
             "working_type": req.working_type or "CONTRACT_PRICE",
             "qty": Decimal(req.quantity) if req.quantity is not None else None,
             "close_position": req.close_position, "reduce_only": req.reduce_only,
             "status": "NEW", "actual_order_id": None, "actual_qty": Decimal(0),
             "avg_price": 0.0, "create_ms": self.now_ms(), "update_ms": self.now_ms(),
             "reject_reason": ""}
        self.algos[req.client_id] = a
        self._emit_algo(a)
        return self._algo_state(a)

    @staticmethod
    def _would_trigger(order_type: str, side: str, trigger: float, price: float) -> bool:
        if order_type == "STOP_MARKET":
            return price >= trigger if side == "BUY" else price <= trigger
        return price <= trigger if side == "BUY" else price >= trigger

    def _check_algos(self) -> None:
        for a in sorted(list(self.algos.values()), key=lambda x: x["create_ms"]):
            if a["status"] != "NEW":
                continue
            ref = self._price_for(a["working_type"])
            if ref is None or not self._would_trigger(a["type"], a["side"], a["trigger"], ref):
                continue
            self._trigger_algo(a)

    def _trigger_algo(self, a: Dict[str, Any]) -> None:
        side_dir = 1 if a["side"] == "BUY" else -1
        a["status"] = "TRIGGERED"
        a["update_ms"] = self.now_ms()
        if a["close_position"] or a["reduce_only"]:
            if self.pos_qty == 0 or _sgn(self.pos_qty) == side_dir:
                a["status"] = "FINISHED"
                a["reject_reason"] = "Reduce Only reject"
                self._emit_algo(a)
                return
        qty = abs(self.pos_qty) if a["close_position"] else min(a["qty"], abs(self.pos_qty)) \
            if a["reduce_only"] else a["qty"]
        oid = self._next("_oid")
        a["actual_order_id"] = str(oid)
        self._emit_algo(a)
        o = {"client_id": f"autoalgo_{a['algo_id']}", "symbol": a["symbol"], "side": a["side"],
             "type": "MARKET", "order_id": oid, "qty": Decimal(qty), "executed": Decimal(0),
             "notional_inv_qty": 0.0, "status": "NEW", "reduce_only": True,
             "close_position": a["close_position"], "update_ms": self.now_ms(),
             "working_type": a["working_type"], "orig_type": a["type"],
             "stop_price": a["trigger"]}
        self.orders[o["client_id"]] = o
        self._emit_order(o, "NEW")
        bps = self.stop_slip_bps if a["type"] == "STOP_MARKET" else self.slip_bps
        px = float(self.spec.round_price(im.price_with_slippage(self.last, a["side"], bps)))
        self._apply_trade(o, side_dir, Decimal(qty), px, maker=False)
        a["status"] = "FINISHED"
        a["actual_qty"] = o["executed"]
        a["avg_price"] = px
        a["update_ms"] = self.now_ms()
        self._emit_algo(a)

    def _check_liquidation(self) -> None:
        if self.pos_qty == 0 or not self.mark:
            return
        lp = self._liq_price()
        d = _sgn(self.pos_qty)
        if (d > 0 and self.mark <= lp) or (d < 0 and self.mark >= lp):
            logger.error("paper: 강제청산 (mark %.1f, 청산가 %.1f)", self.mark, lp)
            side = "SELL" if d > 0 else "BUY"
            o = {"client_id": f"autoclose-{self._next('_oid')}", "symbol": self.spec.symbol,
                 "side": side, "type": "LIQUIDATION", "order_id": self._oid,
                 "qty": abs(self.pos_qty), "executed": Decimal(0), "notional_inv_qty": 0.0,
                 "status": "NEW", "reduce_only": True, "close_position": True,
                 "update_ms": self.now_ms(), "working_type": None, "orig_type": "LIQUIDATION",
                 "stop_price": 0.0}
            self.orders[o["client_id"]] = o
            margin = self.pos_margin
            realized = im.pnl_btc(d, abs(self.pos_qty), self.spec.contract_size, self.pos_entry, lp)
            self._apply_trade(o, -d, abs(self.pos_qty), lp, maker=False, liquidation=True)
            # 청산가 손실을 뺀 격리 증거금 잔여분은 보험기금으로 간다 (청산 수수료 성격)
            remainder = max(0.0, margin + realized)
            self.wallet -= min(remainder, max(0.0, self.wallet))

    def _process_funding(self) -> None:
        if self.next_funding_ms is None:
            return
        while self.market_ts_ms >= self.next_funding_ms:
            ft = self.next_funding_ms
            if self.pos_qty != 0 and self.mark:
                fee = im.funding_fee_btc(_sgn(self.pos_qty), abs(self.pos_qty),
                                         self.spec.contract_size, self.mark, self.funding_rate)
                self.wallet += fee
                self.income.append({"symbol": self.spec.symbol, "incomeType": "FUNDING_FEE",
                                    "income": f"{fee:.8f}", "asset": "BTC", "time": ft,
                                    "tranId": self._next("_tid"), "tradeId": "",
                                    "info": f"rate={self.funding_rate}"})
                self._emit_account("FUNDING_FEE", funding_time=ft, balance_change=fee)
            self.next_funding_ms += FUNDING_INTERVAL_MS

    # ------------------------------------------------------------------ 이벤트 (웹소켓 원문 형식)
    def _emit_order(self, o: Dict[str, Any], exec_type: str, last_qty: Decimal = Decimal(0),
                    last_px: float = 0.0, fee: float = 0.0, realized: float = 0.0,
                    trade_id: int = 0, maker: bool = False) -> None:
        now = self.now_ms()
        avg = float(o["executed"]) / o["notional_inv_qty"] if o["notional_inv_qty"] else 0.0
        self.emit({"e": "ORDER_TRADE_UPDATE", "E": now, "T": now, "i": "paper", "o": {
            "s": o["symbol"], "c": o["client_id"], "S": o["side"], "o": o["type"], "f": "GTC",
            "q": str(o["qty"]), "p": "0", "ap": f"{avg:.8f}", "sp": str(o.get("stop_price", 0)),
            "x": exec_type, "X": o["status"], "i": o["order_id"], "l": str(last_qty),
            "z": str(o["executed"]), "L": f"{last_px}", "ma": "BTC", "N": "BTC",
            "n": f"{fee:.8f}", "T": now, "t": trade_id, "rp": f"{realized:.8f}",
            "b": "0", "a": "0", "m": maker, "R": o["reduce_only"],
            "wt": o.get("working_type") or "CONTRACT_PRICE", "ot": o.get("orig_type", o["type"]),
            "ps": "BOTH", "cp": o.get("close_position", False)}})

    def _emit_algo(self, a: Dict[str, Any]) -> None:
        now = self.now_ms()
        self.emit({"e": "ALGO_UPDATE", "T": now, "E": now, "o": {
            "caid": a["client_id"], "aid": a["algo_id"], "at": "CONDITIONAL", "o": a["type"],
            "s": a["symbol"], "S": a["side"], "ps": "BOTH", "f": "GTC",
            "q": str(a["qty"]) if a["qty"] is not None else "0", "X": a["status"],
            "ai": a["actual_order_id"] or "", "ap": f"{a['avg_price']:.8f}",
            "aq": str(a["actual_qty"]), "act": "MARKET" if a["actual_order_id"] else "0",
            "tp": str(a["trigger"]), "p": "0", "wt": a["working_type"], "cp": a["close_position"],
            "pP": False, "R": a["reduce_only"], "tt": now if a["status"] in ("TRIGGERED", "FINISHED") else 0,
            "rm": a.get("reject_reason", "")}})

    def _emit_account(self, reason: str, funding_time: Optional[int] = None,
                      balance_change: float = 0.0) -> None:
        now = funding_time or self.now_ms()
        a: Dict[str, Any] = {"m": reason, "B": [{"a": "BTC", "wb": f"{self.wallet:.8f}",
                                                "cw": f"{self.wallet - self.pos_margin:.8f}",
                                                "bc": f"{balance_change:.8f}"}],
                             "P": [{"s": self.spec.symbol, "pa": str(self.pos_qty),
                                    "ep": f"{self.pos_entry}", "cr": "0",
                                    "up": f"{self._unrealized():.8f}", "mt": self.margin_type,
                                    "iw": f"{self.pos_margin:.8f}", "ps": "BOTH"}]}
        if reason == "FUNDING_FEE":
            a["S"] = self.spec.symbol
        self.emit({"e": "ACCOUNT_UPDATE", "E": now, "T": now, "i": "paper", "a": a})

    # ------------------------------------------------------------------ 영속화
    def to_state(self) -> Dict[str, Any]:
        def enc(v):
            if isinstance(v, Decimal):
                return {"__d": str(v)}
            if isinstance(v, dict):
                return {k: enc(x) for k, x in v.items()}
            if isinstance(v, list):
                return [enc(x) for x in v]
            return v
        return enc({
            "wallet": self.wallet, "pos_qty": self.pos_qty, "pos_entry": self.pos_entry,
            "pos_margin": self.pos_margin, "leverage": self.leverage, "margin_type": self.margin_type,
            "orders": self.orders, "algos": self.algos,
            "fills": [f.__dict__ for f in self.fills[-500:]], "income": self.income[-500:],
            "ids": [self._oid, self._tid, self._aid], "funding_rate": self.funding_rate,
            "next_funding_ms": self.next_funding_ms, "market_ts_ms": self.market_ts_ms})

    def load_state(self, st: Dict[str, Any]) -> None:
        def dec_(v):
            if isinstance(v, dict):
                if set(v) == {"__d"}:
                    return Decimal(v["__d"])
                return {k: dec_(x) for k, x in v.items()}
            if isinstance(v, list):
                return [dec_(x) for x in v]
            return v
        st = dec_(copy.deepcopy(st))
        self.wallet = float(st["wallet"])
        self.pos_qty = Decimal(st["pos_qty"])
        self.pos_entry = float(st["pos_entry"])
        self.pos_margin = float(st["pos_margin"])
        self.leverage = int(st.get("leverage", self.leverage))
        self.margin_type = st.get("margin_type", self.margin_type)
        self.orders = st.get("orders", {})
        self.algos = st.get("algos", {})
        self.fills = [Fill(**f) for f in st.get("fills", [])]
        self.income = st.get("income", [])
        self._oid, self._tid, self._aid = st.get("ids", [self._oid, self._tid, self._aid])
        self.funding_rate = float(st.get("funding_rate", 0.0))
        self.next_funding_ms = st.get("next_funding_ms")
        self.market_ts_ms = int(st.get("market_ts_ms", 0))
