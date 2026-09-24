"""
청산 관리: 봉 마감 청산(반전·반대 신호·기한·종가 손절), 비상 청산, 부분청산,
손절/목표 체결로 포지션이 0 이 된 뒤 정리(CLOSING -> CLOSED).

원칙:
  - 청산 주문은 전부 reduceOnly 시장가, 수량 = '지금 거래소 포지션' (로컬 추정 아님).
    reduceOnly 라서 중복 전송돼도 포지션을 뒤집거나 새로 만들 수 없다.
  - 청산 중에도 보호 손절은 포지션이 0 으로 확인될 때까지 취소하지 않는다.
    목표(TP) 주문만 먼저 취소한다 (청산과 겹치지 않게).
  - 포지션 0 을 확인한 뒤 남은 주문(손절·목표)을 모두 취소하고, 체결을 REST 로
    다시 받아 손익을 확정한다. 이 정리가 끝나야 CLOSED 가 된다 - 이전 거래의 주문이
    남은 채로 다음 거래가 시작되지 않게.
  - 청산이 계속 실패하면 ERROR (보호 손절은 남겨 둔다) + 치명 알림.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, List, Optional

from ..exchange.errors import (BinanceAPIError, CODE_REDUCE_ONLY_REJECT, ExchangeError,
                               LiveOrderBlocked, OrderStatusUnknown)
from ..exchange.models import OrderRequest
from . import order_ids as ids
from .context import ExecutionContext
from .state_machine import (CLOSED, CLOSING, ERROR, HOLDING_STATES, TradeRecord)

logger = logging.getLogger(__name__)


class ExitManager:
    def __init__(self, ctx: ExecutionContext):
        self.ctx = ctx
        self.protection: Any = None

    async def _position_qty(self) -> Optional[Any]:
        try:
            return await self.ctx.gw.get_position(self.ctx.symbol)
        except ExchangeError as e:
            logger.warning("포지션 조회 실패: %s", e)
            return None

    async def cancel_trade_orders(self, t: TradeRecord, roles: tuple) -> bool:
        """이 거래의 해당 역할 주문 중 살아 있는 것을 전부 취소한다 (DB + 거래소 조회)."""
        ctx = self.ctx
        complete = True
        targets: List[tuple] = []
        for row in ctx.db.list_orders(ctx.mode, trade_id=t.trade_id):
            p = ids.parse(row["client_order_id"])
            if not p:
                continue
            role = {"SL": "stop", "TP": "tp", "EN": "entry", "EX": "exit", "EM": "exit"}[p[1]]
            if role in roles and row["status"] not in ("FILLED", "CANCELED", "EXPIRED", "REJECTED",
                                                       "NOT_PLACED", "NOT_FOUND", "FINISHED"):
                targets.append((row["client_order_id"], bool(row["is_algo"])))
        # 거래소에만 있는 이 거래의 주문도 (DB 기록 누락 대비)
        try:
            orders = (await ctx.gw.get_open_algo_orders(ctx.symbol) +
                      await ctx.gw.get_open_orders(ctx.symbol))
            for a in orders:
                if ids.trade_of(a.client_id) == t.trade_id:
                    role = {"SL": "stop", "TP": "tp", "EN": "entry",
                            "EX": "exit", "EM": "exit"}[ids.parse(a.client_id)[1]]
                    if role in roles and (a.client_id, a.is_algo) not in targets:
                        targets.append((a.client_id, a.is_algo))
        except ExchangeError as e:
            logger.warning("미체결 조건부 주문 조회 실패: %s", e)
            complete = False
        for cid, is_algo in targets:
            try:
                st = await ctx.cancel(cid, is_algo)
                if not st.is_terminal:
                    complete = False
            except LiveOrderBlocked as e:
                logger.error("취소가 LiveOrderGate 에 막힘 %s: %s", cid, e)
                complete = False
            except Exception as e:
                logger.warning("취소 실패 %s: %s", cid, e)
                complete = False
        return complete

    async def close(self, t: TradeRecord, reason: str, emergency: bool = False) -> bool:
        ctx = self.ctx
        if t.state == CLOSED:
            return True
        if t.state != CLOSING:
            ctx.transition(t, CLOSING, f"{'비상 ' if emergency else ''}청산: {reason}")
        t.close_reason = t.close_reason or reason
        ctx.save(t)
        await self.cancel_trade_orders(t, ("tp",))
        role = "EM" if emergency else "EX"
        for attempt in range(3):
            pos = await self._position_qty()
            if pos is None:
                continue
            if pos.qty == 0:
                break
            if pos.direction != t.direction:
                ctx.db.log_risk_event(ctx.mode, "opposite_position_on_close",
                                      {"trade_id": t.trade_id, "position": str(pos.position_amt)},
                                      severity="critical")
                break
            cid = ids.make(t.trade_id, role, t.next_seq(role))
            ctx.save(t)
            req = OrderRequest(cid, ctx.symbol, t.side_close, "MARKET", quantity=pos.qty,
                               reduce_only=True, purpose="EMERGENCY" if emergency else "EXIT",
                               trade_id=t.trade_id)
            try:
                st = await ctx.submit(t, req)
                if st.found:
                    await ctx.wait_final(cid)
            except BinanceAPIError as e:
                if e.code == CODE_REDUCE_ONLY_REJECT:
                    continue                         # 이미 0 일 가능성 - 다시 조회
                logger.error("[%s] 청산 주문 거부 code=%s %s", t.trade_id, e.code, e.msg)
            except (LiveOrderBlocked, OrderStatusUnknown, ExchangeError) as e:
                logger.error("[%s] 청산 주문 실패: %s", t.trade_id, e)
        pos = await self._position_qty()
        if pos is None or pos.qty != 0:
            t.error = f"청산 실패 (포지션 {pos.position_amt if pos else '조회불가'})"
            ctx.transition(t, ERROR, t.error)
            ctx.notify("critical", f"청산 실패 [{t.trade_id}] {reason} - 보호 손절은 유지, 수동 확인 필요",
                       critical=True)
            return False
        return await self.finalize(t)

    async def on_flat(self, t: TradeRecord, reason: str) -> None:
        """손절/목표/외부 청산으로 포지션이 0 이 됐다."""
        ctx = self.ctx
        if t.state == CLOSED:
            return
        if t.state != CLOSING:
            ctx.transition(t, CLOSING, f"포지션 0 확인: {reason}")
        t.close_reason = t.close_reason or reason
        await self.finalize(t)

    async def finalize(self, t: TradeRecord) -> bool:
        ctx = self.ctx
        cleaned = await self.cancel_trade_orders(t, ("stop", "tp", "entry", "exit"))
        t.qty_open = "0"
        if not cleaned:
            ctx.save(t)
            ctx.db.log_risk_event(ctx.mode, "close_cleanup_pending", {"trade_id": t.trade_id})
            return False
        await ctx.sync_fills(t)
        await ctx.sync_funding(t, int((t.opened_at or t.created_ts) * 1000))
        t.qty_open = "0"
        t.closed_at = ctx.now()
        acc = ctx.recompute_accounting(t)
        ctx.transition(t, CLOSED, t.close_reason or "closed")
        side = "LONG" if t.direction > 0 else "SHORT"
        btc = f"{acc['net_pnl_btc']:+.8f}" if acc['net_pnl_btc'] is not None else "미확정"
        usd = f"{acc['net_pnl_usd']:+.2f}" if acc['net_pnl_usd'] is not None else "미확정"
        krw = f"{acc['net_pnl_krw']:+,.0f}" if acc['net_pnl_krw'] is not None else "미확정"
        ctx.notify("close", f"{side} 종료 [{t.trade_id}] 사유 {t.close_reason}\n"
                            f"순손익 {btc} BTC (실현 {acc['realized_pnl_btc']:+.8f}, "
                            f"수수료 {acc['trading_fee_btc']:.8f}, 펀딩 {acc['funding_fee_btc']:+.8f})\n"
                            f"= {usd} USD / {krw} KRW ({ctx.krw_rate_label()})")
        return True

    async def partial_now(self, t: TradeRecord, level: int, qty: Decimal, reason: str) -> None:
        """목표가 이미 지나 TP 주문을 걸 수 없을 때 즉시 부분청산 (reduceOnly)."""
        ctx = self.ctx
        pos = await self._position_qty()
        if pos is None or pos.qty == 0 or pos.direction != t.direction:
            return
        q = min(qty, pos.qty)
        cid = ids.make(t.trade_id, "TP", t.next_seq(f"tp{level}"), level=level)
        t.orders[f"tp{level}"] = cid
        ctx.save(t)
        req = OrderRequest(cid, ctx.symbol, t.side_close, "MARKET", quantity=q, reduce_only=True,
                           purpose=f"TP{level + 1}", trade_id=t.trade_id)
        try:
            await ctx.submit(t, req)
            await ctx.wait_final(cid)
        except Exception as e:
            logger.warning("[%s] 즉시 부분청산 실패: %s", t.trade_id, e)
