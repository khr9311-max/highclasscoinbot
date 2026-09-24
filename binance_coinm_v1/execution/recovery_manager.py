"""
복구·대사 (Binance 실제 상태가 기준).

시작할 때와 웹소켓 재연결 뒤, 그리고 주기적으로 돈다:
   1. 계정 조회            2. BTC 잔고           3. 포지션 조회
   4. 미체결 일반 주문      5. 미체결 조건부(algo) 주문
   6. 로컬 DB 와 비교
   7. 고아 포지션 (거래소엔 있는데 로컬 거래가 없음) -> 채택 후 보호(기본) 또는 청산
   8. 고아 주문 (이 봇의 접두사인데 진행 중 거래와 무관) -> 취소
      남의 주문(접두사 없음) -> 건드리지 않고 신규 진입 차단(기본 정책)
   9. 손절 누락 (포지션이 있는데 활성 손절 없음) -> 즉시 재생성
  10. 오래된 진입 (결과 불명 진입 주문 확정 / 대기 신호가 재시작 공백을 넘김)
  11. 대사: 수량·평단 동기화, 오프라인 중 청산된 거래 정리, 청산 중이던 거래 마무리
  12. 이상 없으면 신규 신호 허용

로컬 DB 에 포지션이 없다는 이유로 새 포지션을 만들지 않는다. 거래소에 포지션이
있으면 먼저 복구한다. 복구가 끝나기 전에는 trading_allowed=False.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..exchange.errors import ExchangeError, OrderStatusUnknown
from ..strategy.ladder import PositionLogic
from . import order_ids as ids
from .context import ExecutionContext
from .state_machine import (CLOSED, CLOSING, ENTRY_FILLED, ENTRY_PENDING, ERROR, HOLDING_STATES,
                            IDLE, POSITION_STATES, PROTECTING, RECOVERY_REQUIRED, SIGNAL_DETECTED,
                            TradeRecord, new_trade_id)

logger = logging.getLogger(__name__)


class RecoveryManager:
    def __init__(self, ctx: ExecutionContext, engine: Any):
        self.ctx = ctx
        self.engine = engine
        self.last_report: Dict[str, Any] = {}

    def _heartbeat_age(self) -> Optional[float]:
        hb = self.ctx.db.kv_get(f"{self.ctx.mode}:heartbeat")
        return None if hb is None else self.ctx.now() - float(hb)

    async def run(self, reason: str = "startup") -> Dict[str, Any]:
        ctx, eng = self.ctx, self.engine
        rep: Dict[str, Any] = {"reason": reason, "ts": ctx.now(), "actions": [], "issues": []}
        act = rep["actions"].append
        issue = rep["issues"].append
        eng.trading_allowed = False
        eng.halts.pop("recovery", None)
        # ---- 1~5. 거래소 상태 ----
        try:
            acct = await ctx.gw.get_account()                                   # 1
            btc = acct.asset(ctx.contract.margin_asset)                          # 2
            rep["wallet_balance_btc"] = btc.wallet_balance
            rep["equity_btc"] = btc.margin_balance
            rep["available_btc"] = btc.available_balance
            if ctx.mode != "paper":
                if await ctx.gw.get_position_mode():
                    eng.halts["position_mode"] = ("헤지 모드(dualSidePosition) - V1 은 원웨이 전용. "
                                                  "UM 과 공유되는 설정이라 봇이 바꾸지 않는다")
                    issue("hedge_mode")
                else:
                    eng.halts.pop("position_mode", None)
            pos = await ctx.gw.get_position(ctx.symbol)                          # 3
            open_orders = await ctx.gw.get_open_orders(ctx.symbol)               # 4
            algos = await ctx.gw.get_open_algo_orders(ctx.symbol)                # 5
        except ExchangeError as e:
            eng.halts["recovery"] = f"거래소 조회 실패: {type(e).__name__}"
            issue(f"exchange_query_failed:{e}")
            self._finish(rep)
            return rep
        qty = pos.qty if pos else Decimal(0)
        rep["position_amt"] = str(pos.position_amt) if pos else "0"
        rep["open_orders"] = [o.client_id for o in open_orders]
        rep["open_algo_orders"] = [o.client_id for o in algos]

        # ---- 6. 로컬 비교 ----
        actives = [TradeRecord.from_dict(d) for d in ctx.db.active_positions(ctx.mode, ctx.symbol)]
        actives.sort(key=lambda x: x.created_ts)
        t: Optional[TradeRecord] = actives[-1] if actives else None
        for old in actives[:-1]:
            # 슬롯은 하나. 오래된 활성 기록은 거래소 기준으로 정리한다.
            old.close_reason = "recovery_superseded"
            old.closed_at = ctx.now()
            self._force_state(old, CLOSED, "복구: 중복 활성 기록 정리")
            act(f"superseded:{old.trade_id}")
        eng.trade = t

        # ---- 10. 오래된 진입 ----
        if t is not None and not t.entry_avg_price:
            en = t.orders.get("entry")
            row = ctx.db.get_order(en) if en else None
            if row and row["status"] in ("PENDING_SUBMIT", "UNKNOWN", "NEW", "PARTIALLY_FILLED"):
                try:
                    st = await ctx.resolve(en, False)
                except OrderStatusUnknown:
                    st = None
                if st is not None and st.found:
                    ctx.store_state(st)
                    if st.executed_qty > 0:
                        if t.state not in (ENTRY_PENDING, RECOVERY_REQUIRED):
                            self._force_state(t, RECOVERY_REQUIRED, "복구: 진입 체결 확인")
                        await eng.entries._confirm_fill(t, en)            # 재시작 중 진입
                        act(f"entry_filled_while_offline:{t.trade_id}")
                    elif st.is_terminal:
                        await self._cancel_pending(t, f"stale_entry:{st.status}")
                        act(f"stale_entry_cancelled:{t.trade_id}")
                elif st is not None:
                    ctx.db.upsert_order({"client_order_id": en, "status": "NOT_PLACED"})
                    if qty == 0:
                        await self._cancel_pending(t, "stale_entry:not_placed")
                        act(f"stale_entry_cancelled:{t.trade_id}")
                else:
                    issue(f"entry_status_unknown:{t.trade_id}")
                    eng.halts["recovery"] = "진입 주문 상태 불명"
            t = eng.trade if eng.trade and eng.trade.state != CLOSED else None
            if t is not None and t.state in (SIGNAL_DETECTED, ENTRY_PENDING) and not t.orders.get("entry"):
                age = self._heartbeat_age()
                expired = t.expire_ts is not None and ctx.now() >= t.expire_ts
                if expired or (reason == "startup" and (age is None or
                                                        age > ctx.settings.restart_pending_grace_sec)):
                    await self._cancel_pending(t, "stale_after_restart" if not expired else "expired")
                    act(f"pending_cancelled:{t.trade_id}")
            eng.trade = t if t is not None and t.state != CLOSED else None
            t = eng.trade

        # ---- 7 / 11. 포지션 대사 ----
        if qty != 0:
            if t is None or not t.entry_avg_price:
                if t is not None:
                    await self._cancel_pending(t, "superseded_by_exchange_position")
                t = await self._adopt_orphan(pos, algos, rep)
            else:
                if pos.direction != t.direction:
                    issue(f"direction_mismatch:{t.trade_id}")
                    eng.halts["recovery"] = "거래소 포지션 방향이 로컬 기록과 반대"
                    self._force_state(t, ERROR, "복구: 방향 불일치")
                    ctx.notify("critical", f"포지션 방향 불일치 [{t.trade_id}] - 수동 확인 필요",
                               critical=True)
                else:
                    if Decimal(t.qty_open) != qty:
                        act(f"qty_synced:{t.qty_open}->{qty}")
                        t.qty_open = str(qty)
                    if t.state == CLOSING or (t.state == ERROR and t.close_reason):
                        # 청산 중(또는 청산 실패로 ERROR)이던 거래는 보유로 되돌리지 않고 청산을 재개
                        if t.state == ERROR:
                            ctx.transition(t, CLOSING, f"복구: 청산 재개 ({t.close_reason})")
                        await eng.exits.close(t, t.close_reason or "resume_close")
                        act(f"resumed_close:{t.trade_id}")
                    elif t.state in (ENTRY_FILLED, PROTECTING):
                        await eng.protection.protect(t, "복구: 보호 손절 생성")
                        act(f"protected:{t.trade_id}")
                    elif t.state in HOLDING_STATES + (RECOVERY_REQUIRED, ERROR):
                        # ---- 9. 손절 누락 ----
                        have = [a for a in algos if ids.trade_of(a.client_id) == t.trade_id
                                and a.order_type == "STOP_MARKET" and a.is_open]
                        if not have:
                            issue(f"missing_stop:{t.trade_id}")
                            if t.state not in (RECOVERY_REQUIRED,):
                                self._force_state(t, RECOVERY_REQUIRED, "복구: 손절 누락")
                            await eng.protection.protect(t, "복구: 누락 손절 재생성")
                            act(f"stop_recreated:{t.trade_id}")
                        else:
                            t.orders["stop"] = have[-1].client_id
                            if t.state in (RECOVERY_REQUIRED, ERROR):
                                self._force_state(t, RECOVERY_REQUIRED, "복구: 손절 확인")
                                self._force_state(t, "PROTECTED", "복구: 손절 확인")
                                eng.protection.refresh_state(t)
                            await eng.protection.place_tps(t)
                    ctx.save(t)
        else:
            if t is not None and t.entry_avg_price and t.state in POSITION_STATES + (RECOVERY_REQUIRED, ERROR):
                # 오프라인 중 손절/목표/수동으로 청산됐다 (또는 청산 중 재시작)
                if t.state in (RECOVERY_REQUIRED, ERROR):
                    self._force_state(t, CLOSING, "복구: 거래소 포지션 0")
                await eng.exits.on_flat(t, t.close_reason or "closed_while_offline")
                act(f"closed_while_offline:{t.trade_id}")
        eng.trade = t if t is not None and t.state != CLOSED else None

        # ---- 8. 고아·외부 주문 ----
        try:
            open_orders = await ctx.gw.get_open_orders(ctx.symbol)
            algos = await ctx.gw.get_open_algo_orders(ctx.symbol)
        except ExchangeError as e:
            issue(f"order_requery_failed:{e}")
        foreign: List[str] = []
        for o in list(open_orders) + list(algos):
            tid = ids.trade_of(o.client_id)
            if tid is None:
                foreign.append(o.client_id)
                continue
            if eng.trade is None or tid != eng.trade.trade_id:
                try:
                    await ctx.cancel(o.client_id, o.is_algo)
                    act(f"orphan_order_cancelled:{o.client_id}")
                except Exception as e:
                    issue(f"orphan_cancel_failed:{o.client_id}:{e}")
        if foreign:
            rep["foreign_orders"] = foreign
            issue(f"foreign_orders:{len(foreign)}")
            if ctx.settings.foreign_order_policy == "block":
                eng.halts["foreign_orders"] = f"이 봇이 만들지 않은 주문 {len(foreign)}건 - 신규 진입 차단"
        else:
            eng.halts.pop("foreign_orders", None)

        # 로컬에 '열림' 으로 남은 주문 기록을 거래소 상태로 갱신
        for row in ctx.db.open_local_orders(ctx.mode):
            if row["status"] in ("PENDING_SUBMIT", "UNKNOWN", "NEW", "PARTIALLY_FILLED", "TRIGGERING"):
                live = {o.client_id for o in list(open_orders) + list(algos)}
                if row["client_order_id"] in live:
                    continue
                try:
                    st = await ctx.resolve(row["client_order_id"], bool(row["is_algo"]))
                    if st.found:
                        ctx.store_state(st)
                    else:
                        ctx.db.upsert_order({"client_order_id": row["client_order_id"],
                                             "status": "NOT_PLACED"})
                except OrderStatusUnknown:
                    issue(f"order_unknown:{row['client_order_id']}")

        # ---- 12. 허용 ----
        self._finish(rep)
        return rep

    # ------------------------------------------------------------------
    def _finish(self, rep: Dict[str, Any]) -> None:
        ctx, eng = self.ctx, self.engine
        critical = [i for i in rep["issues"] if i.startswith(("exchange_query_failed",
                                                              "direction_mismatch",
                                                              "entry_status_unknown"))]
        eng.trading_allowed = not critical and "recovery" not in eng.halts
        rep["trading_allowed"] = eng.trading_allowed
        rep["halts"] = dict(eng.halts)
        self.last_report = rep
        ctx.db.log_event(ctx.mode, "recovery", rep)
        if rep["actions"] or rep["issues"]:
            ctx.notify("recovery", f"복구/대사 ({rep['reason']})\n조치: {rep['actions'] or '없음'}\n"
                                   f"문제: {rep['issues'] or '없음'}\n신규 진입 "
                                   f"{'허용' if eng.trading_allowed else '차단'}",
                       critical=bool(critical))

    def _force_state(self, t: TradeRecord, to: str, reason: str) -> None:
        """복구 전용: 허용 전이 경로로만 이동 (필요하면 RECOVERY_REQUIRED 경유)."""
        from .state_machine import ALLOWED
        if t.state == to:
            return
        if to not in ALLOWED.get(t.state, set()) and t.state != RECOVERY_REQUIRED:
            if RECOVERY_REQUIRED in ALLOWED.get(t.state, set()):
                self.ctx.transition(t, RECOVERY_REQUIRED, reason)
            elif t.state == ERROR and to != CLOSED:
                self.ctx.transition(t, RECOVERY_REQUIRED, reason)
        self.ctx.transition(t, to, reason)

    async def _cancel_pending(self, t: TradeRecord, reason: str) -> None:
        if t.state == CLOSED:
            return
        t.close_reason = reason
        t.closed_at = self.ctx.now()
        if t.state == RECOVERY_REQUIRED:
            self.ctx.transition(t, CLOSED, f"복구: {reason}")
        else:
            self._force_state(t, CLOSED, f"복구: {reason}")
        if t.signal_id:
            self.ctx.db.update_signal(t.signal_id, "cancelled", reason, t.trade_id)

    async def _adopt_orphan(self, pos: Any, algos: List[Any], rep: Dict[str, Any]) -> TradeRecord:
        ctx, eng, s = self.ctx, self.engine, self.ctx.settings
        d = pos.direction
        t = TradeRecord(new_trade_id(), ctx.symbol, ctx.mode, d, pattern="orphan", adopted=True)
        ctx.save(t)
        ctx.db.log_transition(ctx.mode, t.trade_id, None, IDLE, "고아 포지션 채택")
        ctx.transition(t, RECOVERY_REQUIRED, f"고아 포지션 {pos.position_amt} @ {pos.entry_price}")
        t.qty_initial = t.qty_open = str(pos.qty)
        t.entry_avg_price = float(pos.entry_price) or None
        t.opened_at = ctx.now()
        t.entry_fill_time_ms = ctx.now_ms()
        ref = ctx.market.price(s.stop_trigger_type) or pos.mark_price or pos.entry_price
        pct = s.orphan_stop_pct / 100.0
        stop = pos.entry_price * (1 - pct) if d > 0 else pos.entry_price * (1 + pct)
        t.init_stop = t.stop_price = float(ctx.contract.round_price_away(stop, d, is_stop=True))
        t.targets = []
        t.logic = PositionLogic(d, float(t.entry_avg_price or ref), float(t.stop_price), [],
                                "ladder", s.max_hold_bars).to_dict()
        ctx.save(t)
        eng.trade = t
        ctx.db.log_risk_event(ctx.mode, "orphan_position",
                              {"trade_id": t.trade_id, "position": str(pos.position_amt),
                               "entry": pos.entry_price, "policy": s.orphan_position_policy},
                              severity="critical")
        eng.halts["orphan"] = "고아 포지션 채택 - 사람 확인 전 신규 진입 차단"
        rep["actions"].append(f"orphan_adopted:{t.trade_id}")
        ctx.notify("critical", f"고아 포지션 발견 {pos.position_amt}계약 @ {pos.entry_price}\n"
                               f"정책: {s.orphan_position_policy}", critical=True)
        too_far = ref is not None and d * ref <= d * t.stop_price
        if s.orphan_position_policy == "close" or too_far:
            await eng.exits.close(t, "orphan_close" if not too_far else "orphan_beyond_stop",
                                  emergency=True)
        else:
            # 이 봇의 손절이 이미 있으면 그대로 쓰고, 없으면 정책 손절을 건다
            have = [a for a in algos if a.order_type == "STOP_MARKET" and a.is_open
                    and a.close_position]
            if have and ids.trade_of(have[-1].client_id):
                t.orders["stop"] = have[-1].client_id
                self._force_state(t, "PROTECTED", "고아 포지션: 기존 손절 사용")
            else:
                await eng.protection.protect(t, "고아 포지션 보호 손절")
        return t
