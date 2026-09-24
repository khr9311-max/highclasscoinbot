"""
실행 계층 공용 컨텍스트: 주문 제출(선기록), 결과 불명 주문 확정, 체결 기록·귀속,
거래별 손익 계산, 상태 전이 기록, 알림.

중복 주문 방지 규칙 (모든 주문 경로가 이 함수들을 거친다):
  1. 보내기 전에 DB 에 PENDING_SUBMIT 으로 기록한다 (프로세스가 죽어도 흔적이 남는다)
  2. 결과를 모르면(시간 초과·5xx·-1007) 같은 clientOrderId 로 조회해 확정한다
  3. 조회로 '없음' 이 여러 번 확인되기 전에는 새 주문을 내지 않는다
  4. 진입 주문이 불명이면 포지션까지 확인한다 (체결됐는데 조회가 늦는 경우)
  5. 청산·손절은 reduceOnly/closePosition 이라 중복돼도 포지션을 뒤집지 못한다

손익은 BTC 와 USD 를 섞지 않는다:
  realized_pnl_btc = 체결별 실현손익(거래소 값) 합
  trading_fee_btc  = 체결별 수수료 합 (BTC)
  funding_fee_btc  = 보유 중 펀딩 (받으면 +, 내면 -)
  net_pnl_btc      = realized - fee + funding
  *_usd            = 각 BTC 금액을 '그 사건 시점의 가격' 으로 환산해 합한 것
  net_pnl_krw      = net_pnl_usd x USD/KRW (표시용)
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from ..config.settings import Settings
from ..exchange.contract import ContractSpec
from ..exchange.errors import (BinanceAPIError, ExchangeError, LiveOrderBlocked,
                               OrderStatusUnknown)
from ..exchange.gateway import ExchangeGateway
from ..exchange.models import Fill, OrderRequest, OrderState
from ..notifications.telegram import Notifier, NullNotifier
from ..risk import inverse_math as im
from ..storage.db import Database
from . import order_ids as ids
from .state_machine import (CLOSED, InvalidTransition, TradeRecord, check_transition,
                            is_forward)
from .user_events import OrderTracker

logger = logging.getLogger(__name__)


@dataclass
class MarketState:
    last: Optional[float] = None
    mark: Optional[float] = None
    index: Optional[float] = None
    ts_ms: int = 0
    updated_mono: Optional[float] = None
    last_mono: Optional[float] = None
    mark_mono: Optional[float] = None
    last_ts_ms: int = 0
    mark_ts_ms: int = 0
    funding_rate: Optional[float] = None
    next_funding_ms: Optional[int] = None

    def price(self, trigger_type: str) -> Optional[float]:
        return self.mark if trigger_type == "MARK_PRICE" else self.last

    def age(self, now_mono: float) -> float:
        if self.last_mono is None or self.mark_mono is None:
            return float("inf")
        return max(now_mono - self.last_mono, now_mono - self.mark_mono)


class ExecutionContext:
    def __init__(self, settings: Settings, gateway: ExchangeGateway, db: Database,
                 contract: ContractSpec, notifier: Optional[Notifier] = None,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 resolve_attempts: int = 5):
        self.settings = settings
        self.gw = gateway
        self.db = db
        self.contract = contract
        self.notifier = notifier or NullNotifier()
        self.clock = clock
        self.mono = mono
        self.sleep = sleep
        self.resolve_attempts = resolve_attempts
        self.mode = settings.execution_mode
        self.symbol = contract.symbol
        self.tracker = OrderTracker()
        self.market = MarketState()
        self.maker_fee = settings.maker_fee_rate
        self.taker_fee = settings.taker_fee_rate
        self.mmr: Optional[float] = None
        self.cum_btc = 0.0
        self.usd_krw = settings.usd_krw_rate
        self.fx_rate_source = "fixed" if settings.usd_krw_source == "fixed" else "fixed_fallback"

    # ------------------------------------------------------------------ 기록
    def now(self) -> float:
        return self.clock()

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def krw_rate_label(self) -> str:
        if self.fx_rate_source in ("upbit_usdt", "upbit_usdt_cached"):
            source = "공개 참고 시세" if self.fx_rate_source == "upbit_usdt" else "최근 공개 참고 시세"
            return f"USDT/KRW {self.usd_krw:,.0f}원 ({source}, 1 USDT≈1 USD 가정)"
        source = "설정 환율" if self.fx_rate_source == "fixed" else "시세 조회 불가·설정 환율"
        return f"USD/KRW {self.usd_krw:,.0f}원 ({source})"

    def contract_value(self, qty: Any, price: Any) -> str:
        """Executed contract notional, shown as BTC and indicative KRW."""
        try:
            px = float(price)
            quantity = Decimal(str(qty))
            rate = float(self.usd_krw)
            if not (math.isfinite(px) and px > 0 and math.isfinite(rate) and rate > 0
                    and quantity > 0):
                raise ValueError("invalid conversion input")
            usd = float(quantity * self.contract.contract_size)
            btc = usd / px
            return (f"계약 명목가치 {btc:.8f} BTC (약 {usd * rate:,.0f}원; "
                    f"{self.krw_rate_label()})")
        except (ValueError, TypeError, ArithmeticError):
            return "계약 명목가치 원화 환산 미확정"

    def save(self, t: TradeRecord) -> None:
        d = t.to_dict()
        self.db.upsert_position(d)

    def transition(self, t: TradeRecord, to: str, reason: str) -> None:
        cur = t.state
        if cur == to:
            return
        try:
            check_transition(cur, to)
            if not is_forward(cur, to):
                raise InvalidTransition(f"역방향 전이 {cur} -> {to}")
        except InvalidTransition as e:
            self.db.log_risk_event(self.mode, "invalid_transition",
                                   {"trade_id": t.trade_id, "from": cur, "to": to, "reason": reason},
                                   severity="error")
            logger.error("[%s] 전이 차단: %s (%s)", t.trade_id, e, reason)
            raise
        t.state = to
        self.db.log_transition(self.mode, t.trade_id, cur, to, reason)
        self.save(t)
        logger.info("[%s] %s -> %s (%s)", t.trade_id, cur, to, reason)

    def event(self, t: Optional[TradeRecord], name: str, details: Any = None) -> None:
        self.db.log_event(self.mode, name, details, trade_id=t.trade_id if t else None)

    def notify(self, kind: str, text: str, critical: bool = False) -> None:
        try:
            self.notifier.notify(kind, text, critical)
        except Exception:
            logger.exception("알림 실패 (무시)")

    # ------------------------------------------------------------------ 주문
    def _order_row(self, t: Optional[TradeRecord], req: OrderRequest, status: str) -> Dict[str, Any]:
        return {"client_order_id": req.client_id, "trade_id": t.trade_id if t else None,
                "purpose": req.purpose or "", "symbol": req.symbol, "side": req.side,
                "order_type": req.order_type, "is_algo": req.is_conditional,
                "quantity": req.quantity, "price": req.price, "trigger_price": req.trigger_price,
                "reduce_only": req.reduce_only, "close_position": req.close_position,
                "working_type": req.working_type, "status": status, "mode": self.mode,
                "raw": req.to_dict()}

    def store_state(self, st: OrderState) -> None:
        if not st.client_id or self.db.get_order(st.client_id) is None:
            return
        self.db.upsert_order({"client_order_id": st.client_id, "status": st.status,
                              "exchange_order_id": st.exchange_id,
                              "actual_order_id": st.actual_order_id,
                              "executed_qty": st.executed_qty,
                              "avg_price": st.avg_price, "exchange_update_ms": st.update_time_ms})

    async def submit(self, t: Optional[TradeRecord], req: OrderRequest) -> OrderState:
        """선기록 -> 전송 -> (불명이면) 조회 확정. 거부는 예외로 올린다."""
        self.db.upsert_order(self._order_row(t, req, "PENDING_SUBMIT"))
        try:
            st = await self.gw.place_order(req)
        except OrderStatusUnknown as e:
            self.db.upsert_order({"client_order_id": req.client_id, "status": "UNKNOWN",
                                  "last_error": str(e)})
            self.db.log_risk_event(self.mode, "order_status_unknown",
                                   {"client_id": req.client_id, "purpose": req.purpose})
            st = await self.resolve(req.client_id, req.is_conditional)
            if st.status == "NOT_FOUND":
                self.db.upsert_order({"client_order_id": req.client_id, "status": "NOT_PLACED"})
            else:
                self.store_state(st)
            return st
        except LiveOrderBlocked as e:
            self.db.upsert_order({"client_order_id": req.client_id, "status": "NOT_PLACED",
                                  "last_error": str(e)})
            raise
        except BinanceAPIError as e:
            self.db.upsert_order({"client_order_id": req.client_id, "status": "REJECTED",
                                  "last_error": str(e)})
            raise
        except ExchangeError as e:
            self.db.upsert_order({"client_order_id": req.client_id, "status": "REJECTED",
                                  "last_error": str(e)})
            raise
        self.store_state(st)
        return st

    async def resolve(self, client_id: str, is_algo: bool) -> OrderState:
        """
        결과를 모르는 주문 확정. '없음' 이 끝까지 반복되면 NOT_FOUND (거래소에 도달 안 함).
        조회 자체가 계속 실패하면 OrderStatusUnknown (호출부가 RECOVERY_REQUIRED 로).
        """
        delay = 0.5
        last_err: Optional[Exception] = None
        not_found = 0
        for _ in range(self.resolve_attempts):
            try:
                st = await (self.gw.get_algo_order(self.symbol, client_id) if is_algo
                            else self.gw.get_order(self.symbol, client_id))
                if st.found:
                    return st
                not_found += 1
            except ExchangeError as e:
                last_err = e
            await self.sleep(delay)
            delay = min(delay * 2, 4.0)
        if not_found >= 1 and last_err is None:
            return OrderState.not_found(client_id, self.symbol, is_algo)
        raise OrderStatusUnknown(client_id, last_err or RuntimeError("조회 실패"))

    async def wait_final(self, client_id: str, is_algo: bool = False, attempts: int = 6) -> OrderState:
        """시장가 주문의 최종 상태(체결량·평균가)를 조회로 확인한다."""
        st: Optional[OrderState] = None
        delay = 0.2
        for _ in range(attempts):
            try:
                st = await (self.gw.get_algo_order(self.symbol, client_id) if is_algo
                            else self.gw.get_order(self.symbol, client_id))
                self.store_state(st)
                if st.is_terminal and (st.avg_price or st.executed_qty == 0 or st.status != "FILLED"):
                    return st
            except ExchangeError as e:
                logger.warning("주문 조회 실패 %s: %s", client_id, e)
            await self.sleep(delay)
            delay = min(delay * 2, 2.0)
        if st is None:
            raise OrderStatusUnknown(client_id, RuntimeError("최종 상태 조회 실패"))
        return st

    async def cancel(self, client_id: str, is_algo: bool) -> OrderState:
        try:
            st = await self.gw.cancel_order(self.symbol, client_id, is_algo)
        except OrderStatusUnknown:
            st = await self.resolve(client_id, is_algo)
        self.store_state(st)
        return st

    # ------------------------------------------------------------------ 체결·손익
    def attribute(self, client_id: Optional[str], order_id: Optional[str],
                  active: Optional[TradeRecord]) -> Optional[str]:
        if client_id:
            tid = ids.trade_of(client_id)
            if tid:
                return tid
        if order_id:
            row = self.db.find_order_by_exchange_id(order_id, self.mode)
            if row and row.get("trade_id"):
                return row["trade_id"]
            algo = self.tracker.algo_for_order(order_id)
            if algo and ids.trade_of(algo):
                return ids.trade_of(algo)
        # 단일 포지션 봇: 알 수 없는 체결은 진행 중 거래의 것 (강제청산·ADL·트리거된 주문)
        if active is not None and active.state != CLOSED and active.entry_avg_price:
            return active.trade_id
        return None

    def record_fill(self, f: Fill, source: str, active: Optional[TradeRecord]) -> Optional[str]:
        """새 체결이면 귀속된 trade_id, 중복이면 None."""
        if f.symbol != self.symbol:
            return None
        if active is not None and f.time_ms < (active.entry_fill_time_ms or
                                               int((active.opened_at or active.created_ts) * 1000)):
            active = None
        tid = self.attribute(f.client_id, f.order_id, active)
        inserted = self.db.insert_fill({
            "symbol": f.symbol, "exchange_trade_id": f.trade_id, "exchange_order_id": f.order_id,
            "client_order_id": f.client_id, "trade_id": tid, "side": f.side, "price": f.price,
            "qty": float(f.qty), "realized_pnl_btc": f.realized_pnl_btc,
            "commission": f.commission, "commission_asset": f.commission_asset,
            "is_maker": f.maker, "time_ms": f.time_ms, "source": source, "mode": self.mode})
        return tid if inserted else None

    async def sync_fills(self, t: TradeRecord) -> int:
        """REST 로 거래의 체결을 다시 받아 빠진 것을 채운다 (웹소켓 누락 대비)."""
        start = int((t.opened_at or t.created_ts) * 1000) - 60_000
        try:
            fills = await self.gw.get_user_trades(self.symbol, start_ms=start)
        except ExchangeError as e:
            logger.warning("체결 동기화 실패: %s", e)
            if "fills_sync" not in t.accounting_errors:
                t.accounting_errors.append("fills_sync")
            return 0
        if "fills_sync" in t.accounting_errors:
            t.accounting_errors.remove("fills_sync")
        n = 0
        for f in fills:
            if not f.client_id:
                row = self.db.find_order_by_exchange_id(f.order_id, self.mode)
                if row:
                    f.client_id = row["client_order_id"]
            if self.record_fill(f, "rest", t):
                n += 1
        return n

    async def sync_funding(self, t: Optional[TradeRecord], since_ms: Optional[int] = None) -> int:
        """펀딩 수수료를 income 에서 수집 (단일 출처: 거래소 income 기록)."""
        try:
            rows = await self.gw.get_income(self.symbol, "FUNDING_FEE", since_ms)
        except ExchangeError as e:
            logger.warning("펀딩 수집 실패: %s", e)
            if t is not None and "funding_sync" not in t.accounting_errors:
                t.accounting_errors.append("funding_sync")
            return 0
        if t is not None and "funding_sync" in t.accounting_errors:
            t.accounting_errors.remove("funding_sync")
        rows = [r for r in rows if r.get("symbol") == self.symbol]
        missing = [int(r["time"]) for r in rows if not r.get("markPrice")]
        marks = {}
        if missing:
            try:
                marks = await self.gw.funding_marks(self.symbol, min(missing), max(missing))
            except ExchangeError as e:
                logger.warning("과거 펀딩 환산가격 조회 실패: %s", e)
        # Income can contain several transactions at the same settlement time.
        grouped: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            if r.get("asset") != self.contract.margin_asset:
                if t is not None and "funding_asset" not in t.accounting_errors:
                    t.accounting_errors.append("funding_asset")
                continue
            ft = int(r["time"])
            ev = grouped.setdefault(ft, dict(r, income=0.0))
            ev["income"] += float(r["income"])
        n = 0
        for r in grouped.values():
            ft = int(r.get("time") or 0)
            fee = float(r.get("income") or 0.0)
            mark = float(r.get("markPrice") or marks.get(ft) or 0.0)
            if not math.isfinite(mark) or mark <= 0:
                mark = 0.0
            tid = None
            if t is not None and t.opened_at and ft >= int(t.opened_at * 1000) - 1000 and \
                    (not t.closed_at or ft <= int(t.closed_at * 1000) + 1000):
                tid = t.trade_id
                if r.get("estimated") and "estimated_paper_funding" not in t.accounting_errors:
                    t.accounting_errors.append("estimated_paper_funding")
            rate = None
            info = str(r.get("info") or "")
            if info.startswith("rate="):
                try:
                    rate = float(info[5:])
                except ValueError:
                    rate = None
            if self.db.insert_funding_event({
                    "symbol": self.symbol, "funding_time_ms": ft, "funding_rate": rate,
                    "mark_price": mark or None, "position_qty": float(t.qty_open_d) if t else None,
                    "funding_fee_btc": fee, "funding_fee_usd": fee * mark if mark else None,
                    "trade_id": tid, "source": "income"}, self.mode):
                n += 1
                if tid and self.now_ms() - 300_000 <= ft <= self.now_ms() + 1_000:
                    krw = f"약 {fee * mark * self.usd_krw:+,.0f}원" if mark else "원화 환산 미확정"
                    estimate = " (지연 계산 추정)" if r.get("estimated") else ""
                    self.notify("funding", f"펀딩 정산 [{tid}] {fee:+.8f} BTC{estimate}\n"
                                f"{krw} · {self.krw_rate_label()}")
        return n

    def recompute_accounting(self, t: TradeRecord) -> Dict[str, Any]:
        fills = self.db.fills_for_trade(t.trade_id)
        realized = sum(float(f["realized_pnl_btc"] or 0.0) for f in fills)
        fee = sum(float(f["commission"] or 0.0) for f in fills
                  if (f.get("commission_asset") or "BTC") == self.contract.margin_asset)
        realized_usd = sum(float(f["realized_pnl_btc"] or 0.0) * float(f["price"]) for f in fills)
        fee_usd = sum(float(f["commission"] or 0.0) * float(f["price"]) for f in fills
                      if f.get("commission_asset") == self.contract.margin_asset)
        errors = list(t.accounting_errors)
        if any(f.get("commission_asset") != self.contract.margin_asset and f["commission"]
               for f in fills):
            errors.append("unconverted_fee_asset")
        if t.entry_avg_price and t.qty_open_d == 0:
            opened = sum((Decimal(str(f["qty"])) for f in fills if f["side"] == t.side_open), Decimal(0))
            closed = sum((Decimal(str(f["qty"])) for f in fills if f["side"] == t.side_close), Decimal(0))
            if opened != t.qty_initial_d or closed != opened:
                errors.append("fill_quantity_mismatch")
        funds = self.db.funding_events(self.mode, t.trade_id)
        funding = sum(float(r["funding_fee_btc"] or 0.0) for r in funds)
        funding_usd = sum(float(r["funding_fee_usd"] or 0.0) for r in funds)
        unreal = 0.0
        unreal_usd = 0.0
        mark = self.market.mark or self.market.last
        if t.state != CLOSED and t.entry_avg_price and t.qty_open_d > 0 and mark:
            unreal = im.pnl_btc(t.direction, t.qty_open_d, self.contract.contract_size,
                                t.entry_avg_price, mark)
            unreal_usd = unreal * mark
        btc_complete = not errors
        usd_complete = btc_complete and all(r["funding_fee_usd"] is not None for r in funds)
        net = realized - fee + funding if btc_complete else None
        net_usd = realized_usd - fee_usd + funding_usd if usd_complete else None
        acc = {"realized_pnl_btc": realized, "unrealized_pnl_btc": unreal,
               "trading_fee_btc": fee, "funding_fee_btc": funding, "net_pnl_btc": net,
               "realized_pnl_usd": realized_usd, "unrealized_pnl_usd": unreal_usd,
               "trading_fee_usd": fee_usd,
               "funding_fee_usd": funding_usd if all(r["funding_fee_usd"] is not None for r in funds) else None,
               "net_pnl_usd": net_usd, "net_pnl_krw": net_usd * self.usd_krw if net_usd is not None else None,
               "usd_krw": self.usd_krw, "accounting_complete": btc_complete,
               "usd_accounting_complete": usd_complete, "accounting_errors": errors}
        # 거래소 실현손익과 로컬 인버스 계산의 교차 확인 (차이는 기록만)
        if t.entry_avg_price:
            local = 0.0
            for f in fills:
                if f["side"] == t.side_close and f.get("price"):
                    local += im.pnl_btc(t.direction, f["qty"], self.contract.contract_size,
                                        t.entry_avg_price, float(f["price"]))
            acc["local_realized_pnl_btc"] = local
        t.accounting = acc
        return acc

    def funding_periods_estimate(self) -> float:
        return self.settings.max_hold_bars * self.settings.signal_period_sec / (8 * 3600)
