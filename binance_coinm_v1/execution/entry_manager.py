"""
진입 관리: 신호 -> 돌파 대기(ENTRY_PENDING) -> 시장가 진입 -> 실제 체결 확인(ENTRY_FILLED).

- 돌파 판정은 설정한 가격 종류(기본 CONTRACT_PRICE = 체결가)로 한다.
- 대기 중 손절선에 먼저 닿으면 신호 무효 (원본 13장 규칙), 유효 봉 수가 지나면 만료.
- 진입 직전에 거래소 포지션을 다시 조회한다. 로컬 기록과 무관하게 실제 포지션이
  있으면 진입하지 않고 복구로 넘긴다 (Binance 가 source of truth).
- 사이징은 '지금 가격' 으로 한다 (신호 가격이 아니라). 수량은 내림만 한다.
- 체결 확인: 주문 응답이 아니라 GET order 의 executedQty/avgPrice + 포지션 조회.
  부분 체결이면 실제 체결 수량으로 진행한다.
- 결과를 모르는 진입 주문은 같은 clientOrderId 로 조회하고, 그래도 모르면 포지션을
  확인한다. 확정 전에는 새 진입 주문을 내지 않는다.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional, Tuple

from ..exchange.errors import BinanceAPIError, ExchangeError, LiveOrderBlocked, OrderStatusUnknown
from ..exchange.models import OrderRequest
from ..notifications.telegram import kst
from ..risk.limits import RiskLimits
from ..risk.sizing import SizingInput, size_position
from ..strategy.signals import TradeSignal
from . import order_ids as ids
from .context import ExecutionContext
from .state_machine import (CLOSED, ENTRY_FILLED, ENTRY_PENDING, IDLE, RECOVERY_REQUIRED,
                            SIGNAL_DETECTED, TradeRecord, new_trade_id)

logger = logging.getLogger(__name__)


class EntryManager:
    def __init__(self, ctx: ExecutionContext, limits: RiskLimits):
        self.ctx = ctx
        self.limits = limits
        self.protection: Any = None          # ProtectionManager (engine 이 연결)
        self.entry_gate = lambda: (True, "ok")   # engine 이 연결 (거래 허용·시세 신선도 등)

    # ------------------------------------------------------------------
    def arm(self, sig: TradeSignal, signal_id: Optional[int]) -> TradeRecord:
        ctx, spec, s = self.ctx, self.ctx.contract, self.ctx.settings
        d = sig.direction
        t = TradeRecord(new_trade_id(), ctx.symbol, ctx.mode, d, pattern=sig.pattern)
        ctx.save(t)
        ctx.db.log_transition(ctx.mode, t.trade_id, None, IDLE, "새 거래 슬롯")
        ctx.transition(t, SIGNAL_DETECTED,
                       f"{sig.side} {sig.pattern} (신호봉 마감 {kst(sig.close_time)})")
        t.signal = sig.to_dict()
        t.signal_id = signal_id
        t.entry_trigger = float(spec.round_price_away(sig.entry, d, is_stop=False))
        t.init_stop = t.stop_price = float(spec.round_price_away(sig.stop, d, is_stop=True))
        t.targets = [float(spec.round_target(x, d)) for x in sig.targets]
        t.expire_ts = sig.close_time + s.entry_valid_bars * s.signal_period_sec
        ctx.transition(t, ENTRY_PENDING,
                       f"돌파 대기 {t.entry_trigger} / 손절 {t.stop_price} / 목표 {t.targets} "
                       f"(만료 {kst(t.expire_ts)})")
        ctx.notify("signal", f"{sig.side} {sig.pattern}\n돌파 {t.entry_trigger} · 손절 {t.stop_price}\n"
                             f"목표 {', '.join(f'{x:.1f}' for x in t.targets) or '없음(추적)'}\n"
                             f"유효 {s.entry_valid_bars}봉 (~{kst(t.expire_ts)})")
        return t

    async def cancel(self, t: TradeRecord, reason: str, notify: bool = False) -> None:
        t.close_reason = reason
        t.closed_at = self.ctx.now()
        self.ctx.transition(t, CLOSED, f"진입 취소: {reason}")
        if t.signal_id:
            self.ctx.db.update_signal(t.signal_id, "cancelled", reason, t.trade_id)
        if notify:
            self.ctx.notify("signal", f"진입 취소 [{t.trade_id}] {reason}")

    # ------------------------------------------------------------------
    async def on_price(self, t: TradeRecord, price: float, ts: float) -> None:
        if t.state != ENTRY_PENDING:
            return
        d = t.direction
        if t.expire_ts is not None and ts >= t.expire_ts:
            await self.cancel(t, "expired")
            return
        if d * price <= d * float(t.stop_price):
            await self.cancel(t, "stop_before_entry")
            return
        if d * price >= d * float(t.entry_trigger):
            await self.execute(t, price)

    async def execute(self, t: TradeRecord, price: float) -> None:
        ctx, s, spec = self.ctx, self.ctx.settings, self.ctx.contract
        ok, why = self.entry_gate()
        if not ok:
            # 일시적 차단(시세 지연 등)이면 대기 유지, 구조적 차단이면 취소
            if why.startswith("transient:"):
                logger.info("[%s] 진입 보류: %s", t.trade_id, why)
                return
            await self.cancel(t, f"blocked:{why}", notify=True)
            return
        try:
            pos = await ctx.gw.get_position(ctx.symbol)
            acct = await ctx.gw.get_account()
        except ExchangeError as e:
            logger.warning("[%s] 진입 전 조회 실패 - 보류: %s", t.trade_id, e)
            return
        if pos is not None and pos.qty != 0:
            ctx.db.log_risk_event(ctx.mode, "entry_blocked_existing_position",
                                  {"trade_id": t.trade_id, "position_amt": str(pos.position_amt)})
            await self.cancel(t, "exchange_position_exists", notify=True)
            return
        btc = acct.asset(spec.margin_asset)
        equity, avail = btc.margin_balance, btc.available_balance
        ok, why = self.limits.check_new_entry(equity, 0, ctx.now())
        if not ok:
            await self.cancel(t, f"risk:{why}", notify="일일" in why)
            if "일일" in why:
                ctx.notify("daily_loss", why, critical=True)
            return
        sizing = size_position(SizingInput(
            equity_btc=equity, available_btc=avail, risk_fraction=s.risk_fraction,
            direction=t.direction, entry_price=price, stop_price=float(t.stop_price),
            leverage=s.leverage, taker_fee=ctx.taker_fee, entry_slippage_bps=s.slippage_bps,
            stop_slippage_bps=s.stop_slippage_bps, max_exposure_multiple=s.max_exposure_multiple,
            funding_rate=ctx.market.funding_rate or 0.0,
            expected_funding_periods=ctx.funding_periods_estimate(),
            mmr=ctx.mmr, cum_btc=ctx.cum_btc, liq_guard_min_ratio=s.liq_guard_min_ratio), spec)
        t.sizing = sizing.steps
        if not sizing.ok:
            ctx.db.log_risk_event(ctx.mode, "sizing_rejected",
                                  {"trade_id": t.trade_id, "reason": sizing.reason, "steps": sizing.steps})
            await self.cancel(t, f"sizing:{sizing.reason}", notify=True)
            return
        t.equity_at_entry_btc = equity
        t.entry_attempts += 1
        cid = ids.make(t.trade_id, "EN", t.next_seq("EN"))
        t.orders["entry"] = cid
        ctx.save(t)
        req = OrderRequest(cid, ctx.symbol, t.side_open, "MARKET", quantity=sizing.qty,
                           purpose="ENTRY", trade_id=t.trade_id)
        try:
            st = await ctx.submit(t, req)
        except LiveOrderBlocked as e:
            await self.cancel(t, "live_gate_blocked", notify=True)
            ctx.notify("validation_gate", f"실거래 진입 차단: {e}")
            return
        except BinanceAPIError as e:
            ctx.notify("api_error", f"진입 주문 거부 [{t.trade_id}] code={e.code} {e.msg}")
            await self.cancel(t, f"entry_rejected:{e.code}")
            return
        except OrderStatusUnknown:
            # 조회로도 확정 못 함 -> 포지션을 보고 판단, 그래도 모르면 복구로
            await self._resolve_by_position(t, cid)
            return
        if st.status == "NOT_FOUND":
            # 거래소에 도달하지 않았다. 포지션도 비었는지 확인 후 재시도 허용
            pos = await ctx.gw.get_position(ctx.symbol)
            if pos is not None and pos.qty != 0:
                await self._adopt_from_position(t, pos, cid)
                return
            if t.entry_attempts >= s.entry_max_attempts:
                await self.cancel(t, "entry_not_placed", notify=True)
            else:
                logger.warning("[%s] 진입 주문 미도달 - 다음 가격에서 재시도", t.trade_id)
                ctx.save(t)
            return
        await self._confirm_fill(t, cid)

    async def _confirm_fill(self, t: TradeRecord, cid: str) -> None:
        ctx = self.ctx
        try:
            final = await ctx.wait_final(cid)
        except OrderStatusUnknown:
            await self._resolve_by_position(t, cid)
            return
        pos = await ctx.gw.get_position(ctx.symbol)
        executed = final.executed_qty
        if executed <= 0:
            if pos is not None and pos.qty != 0:
                await self._adopt_from_position(t, pos, cid)
                return
            await self.cancel(t, f"entry_not_filled:{final.status}", notify=True)
            return
        avg = final.avg_price or (pos.entry_price if pos else None)
        qty = executed
        if pos is not None and pos.qty != executed:
            ctx.db.log_risk_event(ctx.mode, "fill_position_mismatch",
                                  {"trade_id": t.trade_id, "executed": str(executed),
                                   "position": str(pos.position_amt)})
            if pos.qty > 0 and pos.direction == t.direction:
                qty = pos.qty                      # 거래소 포지션이 기준
        self._mark_filled(t, qty, avg, final.update_time_ms or ctx.now_ms(),
                          partial=final.status != "FILLED")
        await ctx.sync_fills(t)
        ctx.recompute_accounting(t)
        ctx.save(t)
        await self.protection.protect(t)

    def _mark_filled(self, t: TradeRecord, qty: Decimal, avg: Optional[float], fill_ms: int,
                     partial: bool) -> None:
        ctx = self.ctx
        t.qty_initial = t.qty_open = str(qty)
        t.entry_avg_price = float(avg) if avg else None
        t.opened_at = ctx.now()
        t.entry_fill_time_ms = int(fill_ms)
        ctx.transition(t, ENTRY_FILLED, f"{'부분 ' if partial else ''}체결 {qty}계약 @ {avg}")
        if t.signal_id:
            ctx.db.update_signal(t.signal_id, "entered", "filled", t.trade_id)
        side = "LONG" if t.direction > 0 else "SHORT"
        ctx.notify("entry", f"{side} 진입 [{t.trade_id}] {qty}계약 @ {avg}\n"
                            f"손절 {t.stop_price} · 목표 {t.targets or '추적'}"
                            + ("\n(부분 체결 - 실제 체결 수량으로 진행)" if partial else ""))

    async def _adopt_from_position(self, t: TradeRecord, pos: Any, cid: str) -> None:
        ctx = self.ctx
        if pos.direction != t.direction:
            ctx.db.log_risk_event(ctx.mode, "unexpected_position_direction",
                                  {"trade_id": t.trade_id, "position": str(pos.position_amt)},
                                  severity="critical")
            ctx.transition(t, RECOVERY_REQUIRED, "예상과 반대 방향 포지션")
            ctx.notify("critical", f"예상과 반대 방향 포지션 발견 [{t.trade_id}] - 복구 필요",
                       critical=True)
            return
        self._mark_filled(t, pos.qty, pos.entry_price, ctx.now_ms(), partial=False)
        await ctx.sync_fills(t)
        ctx.recompute_accounting(t)
        await self.protection.protect(t)

    async def _resolve_by_position(self, t: TradeRecord, cid: str) -> None:
        ctx = self.ctx
        try:
            pos = await ctx.gw.get_position(ctx.symbol)
        except ExchangeError:
            pos = None
        if pos is not None and pos.qty != 0:
            await self._adopt_from_position(t, pos, cid)
            return
        ctx.transition(t, RECOVERY_REQUIRED, f"진입 주문 결과 불명 ({cid})")
        ctx.notify("recovery", f"진입 주문 결과 불명 [{t.trade_id}] - 복구 대기", critical=True)
