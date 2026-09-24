"""
보호 관리: 체결 직후 보호 손절 생성 -> 존재 확인 -> PROTECTED, 목표(TP) 주문, 손절 이동.

순서 (명세):
  1. 실제 체결 수량 조회   2. 실제 평균 체결가 확인   (entry_manager)
  3. 보호 손절 생성        4. 보호 손절 존재 확인 (GET algoOrder)   5. PROTECTED

보호 손절 = STOP_MARKET closePosition=true (algo 주문). closePosition 이라
  - 부분청산 뒤에도 남은 포지션 전체를 닫는다 (수량을 다시 맞출 필요 없음)
  - 포지션이 없으면 아무것도 하지 않는다 (새 포지션을 만들 수 없다)
생성·확인이 3번 실패하면 즉시 비상 청산한다. 손절가가 이미 현재가 너머라
'즉시 발동(-2021)' 이면 기다리지 않고 비상 청산한다.

목표 주문 = TAKE_PROFIT_MARKET reduceOnly + 수량 (TP1/TP2/TP3). reduceOnly 라서
포지션을 늘리거나 뒤집을 수 없다. 목표 주문 실패는 치명적이지 않다(손절이 지킨다) -
다음 대사 때 다시 낸다.

손절 이동(사다리·추적): 새 손절을 먼저 내고 확인한 뒤 옛 손절을 취소한다 (보호 공백
없음). 거래소가 새 손절을 명시적으로 거부하면(동시 closePosition 제한 등) '옛 것 취소 ->
새 것' 으로 한 번 더 시도하고, 그것도 실패하면 비상 청산한다.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, List, Optional, Tuple

from ..exchange.errors import (BinanceAPIError, CODE_IMMEDIATE_TRIGGER, ExchangeError,
                               LiveOrderBlocked, OrderStatusUnknown)
from ..exchange.models import OrderRequest
from ..strategy.ladder import PositionLogic, tp_quantities, tp_schedule
from . import order_ids as ids
from .context import ExecutionContext
from .state_machine import (CLOSED, CLOSING, PROTECTED, PROTECTING, RECOVERY_REQUIRED,
                            TradeRecord, progress_state)

logger = logging.getLogger(__name__)

OK, IMMEDIATE, FAILED, REJECTED = "ok", "immediate", "failed", "rejected"


class ProtectionManager:
    def __init__(self, ctx: ExecutionContext):
        self.ctx = ctx
        self.exits: Any = None                 # ExitManager (engine 이 연결)

    # ------------------------------------------------------------------ 손절
    async def _place_stop(self, t: TradeRecord, stop: float) -> Tuple[str, Optional[str]]:
        ctx, s = self.ctx, self.ctx.settings
        cid = ids.make(t.trade_id, "SL", t.next_seq("SL"))
        ctx.save(t)
        req = OrderRequest(cid, ctx.symbol, t.side_close, "STOP_MARKET",
                           trigger_price=Decimal(str(stop)), close_position=True,
                           working_type=s.stop_trigger_type, price_protect=s.stop_price_protect,
                           purpose="STOP", trade_id=t.trade_id)
        try:
            st = await ctx.submit(t, req)
        except BinanceAPIError as e:
            if e.code == CODE_IMMEDIATE_TRIGGER:
                return IMMEDIATE, cid
            logger.error("[%s] 손절 생성 거부 code=%s %s", t.trade_id, e.code, e.msg)
            return REJECTED, cid
        except LiveOrderBlocked as e:
            logger.error("[%s] 손절 생성이 LiveOrderGate 에 막힘: %s", t.trade_id, e)
            return FAILED, cid
        except (OrderStatusUnknown, ExchangeError) as e:
            logger.error("[%s] 손절 생성 결과 불명: %s", t.trade_id, e)
            return FAILED, cid
        if not st.found:
            return FAILED, cid
        # 존재 확인 (응답만 믿지 않고 조회)
        try:
            v = await ctx.gw.get_algo_order(ctx.symbol, cid)
            ctx.store_state(v)
        except ExchangeError as e:
            logger.error("[%s] 손절 존재 확인 실패: %s", t.trade_id, e)
            return FAILED, cid
        if v.status in ("NEW", "TRIGGERING"):
            return OK, cid
        return FAILED, cid

    def is_valid_stop(self, t: TradeRecord, order: Any) -> bool:
        return (ids.trade_of(order.client_id) == t.trade_id and order.symbol == t.symbol
                and order.order_type == "STOP_MARKET" and order.is_open
                and order.close_position and order.side == t.side_close
                and order.working_type == self.ctx.settings.stop_trigger_type
                and order.trigger_price is not None
                and t.direction * order.trigger_price >= t.direction * float(t.stop_price))

    async def protect(self, t: TradeRecord, reason: str = "보호 손절 생성") -> bool:
        ctx = self.ctx
        if t.state != PROTECTING:
            ctx.transition(t, PROTECTING, reason)
        if t.entry_avg_price and not t.logic:
            t.logic = PositionLogic(t.direction, float(t.entry_avg_price), float(t.stop_price),
                                    list(t.targets), "ladder", ctx.settings.max_hold_bars).to_dict()
        for attempt in range(3):
            res, cid = await self._place_stop(t, float(t.stop_price))
            if res == OK:
                t.orders["stop"] = cid
                ctx.transition(t, PROTECTED, f"보호 손절 확인 {t.stop_price} ({cid})")
                ctx.notify("fill", f"보호 손절 확인 [{t.trade_id}] {t.stop_price} "
                                   f"({ctx.settings.stop_trigger_type})")
                await self.place_tps(t)
                self.refresh_state(t)
                return True
            if res == IMMEDIATE:
                break
        ctx.db.log_risk_event(ctx.mode, "protection_failed",
                              {"trade_id": t.trade_id, "stop": t.stop_price}, severity="critical")
        ctx.notify("critical", f"보호 손절 생성 실패 [{t.trade_id}] - 비상 청산 시도", critical=True)
        await self.exits.close(t, "protect_failed", emergency=True)
        return False

    def refresh_state(self, t: TradeRecord) -> None:
        """TP/사다리/추적 진행을 상태 라벨로 반영 (앞으로만)."""
        lg = t.logic or {}
        target = progress_state(int(lg.get("ladder_step", 0)), t.tp_filled,
                                bool(lg.get("trailing_active", False)))
        if target != t.state and t.state in (PROTECTED, "TP1", "TP2", "TP3", "TRAILING"):
            from .state_machine import is_forward
            if is_forward(t.state, target):
                self.ctx.transition(t, target, "진행 갱신")

    # ------------------------------------------------------------------ 목표 주문
    def plan_tps(self, t: TradeRecord) -> List[Tuple[int, Decimal]]:
        sch = tp_schedule("ladder", len(t.targets), self.ctx.settings.ladder_tp_fractions)
        return tp_quantities(t.qty_initial_d, sch, self.ctx.contract.market_step_size)

    async def place_tps(self, t: TradeRecord) -> None:
        ctx, s = self.ctx, self.ctx.settings
        plan = self.plan_tps(t)
        t.tp_plan = [[lvl, str(q), t.targets[lvl]] for lvl, q in plan]
        ctx.save(t)
        for lvl, q in plan:
            if t.state in (CLOSED, CLOSING) or t.qty_open_d <= 0:
                return
            if lvl in t.tp_filled:
                continue
            key = f"tp{lvl}"
            # 누락된 WS 체결을 먼저 조회한다. 취소/만료된 부분 체결도 누적 차감해야
            # 재시작 때 같은 목표를 두 번 청산하거나 잔여량보다 크게 재발주하지 않는다.
            filled = Decimal(0)
            pending = False
            for row in ctx.db.list_orders(ctx.mode, trade_id=t.trade_id):
                parsed = ids.parse(row["client_order_id"])
                if not parsed or parsed[1:3] != ("TP", lvl):
                    continue
                if row["status"] not in ("REJECTED", "NOT_PLACED", "NOT_FOUND"):
                    try:
                        st = await ctx.resolve(row["client_order_id"], bool(row["is_algo"]))
                        if not st.found:
                            # 과거에 존재했던 주문의 조회 불가는 미발주 증거가 아니다.
                            pending = True
                        else:
                            ctx.store_state(st)
                            pending |= not st.is_terminal
                            # FINISHED인데 actualQty가 없으면 체결량을 확정할 수 없다.
                            pending |= st.status == "FINISHED" and st.executed_qty == 0
                    except ExchangeError as e:
                        pending = True
                        logger.warning("TP 체결 대사 보류 %s: %s", row["client_order_id"], e)
                row = ctx.db.get_order(row["client_order_id"])
                filled += Decimal(str(row.get("executed_qty") or 0))
            if filled >= q:
                t.tp_filled.append(lvl)
                if t.logic:
                    lg = PositionLogic.from_dict(t.logic)
                    lg.on_tp_filled(lvl)
                    t.logic = lg.to_dict()
                ctx.save(t)
                continue
            if pending:
                continue
            q -= filled
            if q > t.qty_open_d:
                q = t.qty_open_d
            if q <= 0:
                continue
            px = Decimal(str(t.targets[lvl]))
            cid = ids.make(t.trade_id, "TP", t.next_seq(key), level=lvl)
            t.orders[key] = cid
            ctx.save(t)
            req = OrderRequest(cid, ctx.symbol, t.side_close, "TAKE_PROFIT_MARKET", quantity=q,
                               reduce_only=True, trigger_price=px, working_type=s.tp_trigger_type,
                               purpose=f"TP{lvl + 1}", trade_id=t.trade_id)
            try:
                await ctx.submit(t, req)
            except BinanceAPIError as e:
                if e.code == CODE_IMMEDIATE_TRIGGER:
                    # 이미 목표 너머 - 목표 도달로 보고 지금 부분청산
                    await self.exits.partial_now(t, lvl, q, "tp_immediate")
                else:
                    ctx.db.log_risk_event(ctx.mode, "tp_place_failed",
                                          {"trade_id": t.trade_id, "level": lvl, "code": e.code})
            except (ExchangeError, OrderStatusUnknown) as e:
                ctx.db.log_risk_event(ctx.mode, "tp_place_failed",
                                      {"trade_id": t.trade_id, "level": lvl, "error": str(e)})

    # ------------------------------------------------------------------ 손절 이동
    async def move_stop(self, t: TradeRecord, new_stop: float, why: str) -> bool:
        ctx, spec = self.ctx, self.ctx.contract
        d = t.direction
        new_stop = float(spec.round_price_away(new_stop, d, is_stop=True))
        if d * new_stop <= d * float(t.stop_price):
            return False                                   # 손절은 한 방향으로만
        ref = ctx.market.price(ctx.settings.stop_trigger_type) or ctx.market.last
        if ref is not None and d * ref <= d * new_stop:
            # 새 손절이 이미 현재가 너머 -> 기다릴 이유가 없다
            await self.exits.close(t, "stop_close")
            return True
        old = t.orders.get("stop")
        res, cid = await self._place_stop(t, new_stop)
        if res == IMMEDIATE:
            await self.exits.close(t, "stop_close")
            return True
        if res == REJECTED and old:
            # 동시에 두 개를 못 두는 경우: 옛 것 취소 -> 새 것 (공백 최소화)
            logger.warning("[%s] 새 손절 거부 - 교체 방식으로 재시도", t.trade_id)
            await self._cancel_quiet(old)
            res, cid = await self._place_stop(t, new_stop)
            if res != OK:
                ctx.notify("critical", f"손절 교체 실패 [{t.trade_id}] - 비상 청산", critical=True)
                await self.exits.close(t, "stop_replace_failed", emergency=True)
                return False
            old = None
        if res != OK:
            ctx.db.log_risk_event(ctx.mode, "stop_move_failed",
                                  {"trade_id": t.trade_id, "from": t.stop_price, "to": new_stop})
            return False                                   # 옛 손절이 그대로 보호 중
        t.orders["stop"] = cid
        prev = t.stop_price
        t.stop_price = new_stop
        if t.logic:
            t.logic["stop"] = new_stop
        ctx.save(t)
        if old and old != cid:
            await self._cancel_quiet(old)
        ctx.event(t, "stop_moved", {"from": prev, "to": new_stop, "why": why, "order": cid})
        ctx.notify("trailing", f"손절 이동 [{t.trade_id}] {prev} -> {new_stop} ({why})")
        return True

    async def _cancel_quiet(self, cid: str) -> None:
        try:
            await self.ctx.cancel(cid, is_algo=True)
        except Exception as e:
            logger.warning("주문 취소 실패 %s: %s (대사 때 정리)", cid, e)

    async def ensure_stop(self, t: TradeRecord) -> bool:
        """복구·대사용: 활성 손절이 거래소에 없으면 다시 만든다."""
        ctx = self.ctx
        try:
            algos = await ctx.gw.get_open_algo_orders(ctx.symbol)
        except ExchangeError as e:
            logger.warning("손절 확인 실패: %s", e)
            return False
        mine = [a for a in algos if self.is_valid_stop(t, a)]
        if mine:
            t.orders["stop"] = mine[-1].client_id
            return True
        ctx.db.log_risk_event(ctx.mode, "missing_stop", {"trade_id": t.trade_id}, severity="critical")
        ctx.notify("critical", f"보호 손절 누락 발견 [{t.trade_id}] - 재생성", critical=True)
        if t.state not in (PROTECTING, RECOVERY_REQUIRED):
            ctx.transition(t, RECOVERY_REQUIRED, "보호 손절 누락")
        return await self.protect(t, "누락된 보호 손절 재생성")
