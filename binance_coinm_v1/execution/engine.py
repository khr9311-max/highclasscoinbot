"""
실행 엔진 - 전략(봉 마감) · 시세(돌파 감시) · 사용자 이벤트(체결) · 복구를 한 줄로 세운다.

동시성: 모든 상태 변경과 주문은 하나의 asyncio.Lock 안에서 순서대로 처리한다.
사용자 이벤트는 큐에 쌓였다가 락 안에서 처리된다 (주문 처리 중 도착한 이벤트가
재진입해 상태를 꼬지 않게). 알림은 기다리지 않는다.

1시간봉 마감 처리 순서:
  1. 마감 봉 i 로 4시간봉 존 -> 신호 판정 (미래 봉 없음)
  2. 진행 중 거래:
       돌파 대기 -> 만료 / 반대 방향 신호면 취소
       보유 중   -> 원본 청산 규칙(반전 신호·반대 신호·기한·종가 손절·사다리·추적)
  3. 새 신호로 진입 대기 등록. 단,
       - 이번 봉에서 기존 포지션을 닫았거나(닫는 중이거나), 청산에 쓰인 반대 신호면
         등록하지 않는다 (즉시 반전 금지: 먼저 닫고, 다음 '새' 신호를 기다린다)
       - 재시작 뒤 밀린 옛 봉(신선하지 않은 봉)으로는 진입하지 않는다
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from decimal import Decimal
from typing import Any, Callable, Deque, Dict, Optional

from ..config.settings import Settings
from ..exchange.contract import ContractSpec
from ..exchange.errors import ExchangeError
from ..exchange.gateway import ExchangeGateway
from ..exchange.models import Fill
from ..notifications.telegram import Notifier, kst
from ..risk.limits import RiskLimits
from ..storage.db import Database
from ..strategy.ladder import PositionLogic
from ..strategy.price_action import Bars
from ..strategy.signals import BarEvaluation, SignalEngine, zones_at
from . import order_ids as ids
from .context import ExecutionContext
from .entry_manager import EntryManager
from .exit_manager import ExitManager
from .protection_manager import ProtectionManager
from .recovery_manager import RecoveryManager
from .state_machine import (CLOSED, CLOSING, ENTRY_PENDING, HOLDING_STATES, PROTECTING,
                            TradeRecord)
from .user_events import (AccountUpdate, AlgoUpdate, OrderUpdate, OtherEvent, parse_user_event)

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: Settings, gateway: ExchangeGateway, db: Database,
                 contract: ContractSpec, notifier: Optional[Notifier] = None,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep, live_gate: Any = None):
        self.settings = settings
        self.ctx = ExecutionContext(settings, gateway, db, contract, notifier, clock, mono, sleep)
        self.db = db
        self.mode = self.ctx.mode
        self.limits = RiskLimits(settings, db, self.mode)
        self.entries = EntryManager(self.ctx, self.limits)
        self.protection = ProtectionManager(self.ctx)
        self.exits = ExitManager(self.ctx)
        self.entries.protection = self.protection
        self.protection.exits = self.exits
        self.exits.protection = self.protection
        self.recovery = RecoveryManager(self.ctx, self)
        self.signals = SignalEngine(settings.directions, settings.min_rr)
        self.live_gate = live_gate
        self.lock = asyncio.Lock()
        self.user_q: Deque[Dict[str, Any]] = deque()
        self.trading_allowed = False
        self.halts: Dict[str, str] = {}
        self.last_bar_ts: Optional[float] = db.kv_get(f"{self.mode}:last_bar_ts")
        self.last_eval: Optional[BarEvaluation] = None
        self.position_amt_ws: Optional[Decimal] = None
        self._pos_event_ms = 0
        actives = [TradeRecord.from_dict(d) for d in db.active_positions(self.mode, contract.symbol)]
        self.trade: Optional[TradeRecord] = max(actives, key=lambda x: x.created_ts) if actives else None
        self.entries.entry_gate = self._entry_gate
        gateway.set_event_sink(self.enqueue_user_event)

    # ------------------------------------------------------------------ 상태
    @property
    def state(self) -> str:
        return self.trade.state if self.trade is not None else "IDLE"

    def _sync_trade(self) -> None:
        if self.trade is not None and self.trade.state == CLOSED:
            if self.trade.adopted:
                self.halts.pop("orphan", None)
            self.trade = None

    def _entry_gate(self):
        if not self.trading_allowed:
            return False, "복구 미완료 (trading_allowed=False)"
        if self.halts:
            return False, "; ".join(self.halts.values())
        age = self.ctx.market.age(self.ctx.mono())
        if age > self.settings.market_ws_stale_sec:
            return False, f"transient: 시세 지연 {age:.0f}s"
        if self.mode in ("live", "testnet") and self.live_gate is not None and not self.live_gate.is_open():
            return False, "LiveOrderGate 닫힘: " + "; ".join(self.live_gate.reasons())
        return True, "ok"

    def status(self) -> Dict[str, Any]:
        t = self.trade
        return {"mode": self.mode, "state": self.state, "trading_allowed": self.trading_allowed,
                "halts": dict(self.halts), "last_bar_ts": self.last_bar_ts,
                "trade": t.to_dict() if t else None,
                "market": {"last": self.ctx.market.last, "mark": self.ctx.market.mark,
                           "index": self.ctx.market.index}}

    # ------------------------------------------------------------------ 입력: 사용자 이벤트
    def enqueue_user_event(self, raw: Dict[str, Any]) -> None:
        self.user_q.append(raw)

    async def drain_user_events(self) -> None:
        async with self.lock:
            await self._drain_locked()

    async def _drain_locked(self) -> None:
        guard = 0
        while self.user_q and guard < 10_000:
            guard += 1
            raw = self.user_q.popleft()
            try:
                await self._handle_user_event(raw)
            except Exception:
                logger.exception("사용자 이벤트 처리 실패 (계속)")
        self._sync_trade()

    async def _handle_user_event(self, raw: Dict[str, Any]) -> None:
        ctx = self.ctx
        ev = parse_user_event(raw)
        t = self.trade
        if isinstance(ev, OrderUpdate):
            if ev.symbol != ctx.symbol:
                return
            accepted, new_fill = ctx.tracker.apply_order(ev)
            if accepted and ctx.db.get_order(ev.client_id):
                ctx.db.upsert_order({"client_order_id": ev.client_id, "status": ev.status,
                                     "exchange_order_id": ev.order_id, "executed_qty": ev.cum_qty,
                                     "avg_price": ev.avg_price or None,
                                     "exchange_update_ms": ev.transaction_time_ms})
            if new_fill:
                f = Fill(ev.symbol, ev.trade_id, ev.order_id, ev.side, ev.last_price, ev.last_qty,
                         ev.realized_pnl, ev.commission, ev.commission_asset,
                         ev.transaction_time_ms, ev.maker, client_id=ev.client_id)
                tid = ctx.record_fill(f, "ws", t)
                if t is not None and tid == t.trade_id:
                    await self._on_trade_fill(t, ev)
        elif isinstance(ev, AlgoUpdate):
            if ev.symbol and ev.symbol != ctx.symbol:
                return
            changed = ctx.tracker.apply_algo(ev)
            if ctx.db.get_order(ev.client_algo_id):
                ctx.db.upsert_order({"client_order_id": ev.client_algo_id, "status": ev.status,
                                     "exchange_order_id": ev.algo_id,
                                     "actual_order_id": ev.actual_order_id})
            if t is None or ids.trade_of(ev.client_algo_id) != t.trade_id or not changed:
                return
            p = ids.parse(ev.client_algo_id)
            if p and p[1] == "SL" and ev.client_algo_id == t.orders.get("stop"):
                if ev.status in ("CANCELED", "EXPIRED", "REJECTED") and t.state in HOLDING_STATES:
                    # 우리가 취소한 게 아닌데 활성 손절이 사라졌다 -> 즉시 재보호
                    row = ctx.db.get_order(ev.client_algo_id)
                    ctx.db.log_risk_event(self.mode, "stop_disappeared",
                                          {"trade_id": t.trade_id, "order": ev.client_algo_id,
                                           "status": ev.status, "reason": ev.reject_reason})
                    await self.protection.ensure_stop(t)
                elif ev.status in ("TRIGGERED", "FINISHED"):
                    await self._check_flat(t, self._stop_reason(t))
            elif p and p[1] == "TP" and ev.status == "FINISHED" and ev.actual_qty > 0:
                # 발동된 TP 의 체결 이벤트가 ALGO_UPDATE 보다 먼저 와서 연결을 못 했어도
                # 여기서 TP 체결을 확정한다 (이벤트 도착 순서에 의존하지 않게)
                await self._mark_tp_filled(t, int(p[2]), ev.actual_qty, ev.avg_price)
        elif isinstance(ev, AccountUpdate):
            for p in ev.positions:
                if p.symbol == ctx.symbol and p.position_side in ("BOTH", ""):
                    if ev.event_time_ms >= self._pos_event_ms:
                        self._pos_event_ms = ev.event_time_ms
                        self.position_amt_ws = p.position_amt
            if ev.reason == "FUNDING_FEE":
                await ctx.sync_funding(t, None)
                if t is not None:
                    ctx.recompute_accounting(t)
                    ctx.save(t)
            if t is not None and t.state in HOLDING_STATES + (PROTECTING,) and \
                    self.position_amt_ws is not None and self.position_amt_ws == 0:
                await self._check_flat(t, self._stop_reason(t))
        elif isinstance(ev, OtherEvent):
            if ev.name == "MARGIN_CALL":
                ctx.db.log_risk_event(self.mode, "margin_call", ev.raw, severity="critical")
                ctx.notify("critical", "마진콜 이벤트 수신 - 포지션 확인 필요", critical=True)
            elif ev.name == "ACCOUNT_CONFIG_UPDATE":
                ctx.db.log_risk_event(self.mode, "account_config_update", ev.raw)

    def _stop_reason(self, t: TradeRecord) -> str:
        lg = t.logic or {}
        if lg.get("trailing_active"):
            return "trailing_stop"
        if int(lg.get("ladder_step", 0)) >= 1 or t.tp_filled:
            return "ladder_stop"
        return "stop"

    async def _check_flat(self, t: TradeRecord, reason: str) -> None:
        try:
            pos = await self.ctx.gw.get_position(self.ctx.symbol)
        except ExchangeError:
            return
        if pos is not None and pos.qty == 0 and t.state != CLOSED:
            await self.exits.on_flat(t, reason)
        elif pos is not None and pos.qty != 0 and t.state != CLOSED:
            t.qty_open = str(pos.qty)
            self.ctx.save(t)

    async def _mark_tp_filled(self, t: TradeRecord, level: int, qty: Any, avg: float) -> None:
        ctx = self.ctx
        if t.state == CLOSED:
            return
        if level not in t.tp_filled:
            t.tp_filled.append(level)
            if t.logic:
                lg = PositionLogic.from_dict(t.logic)
                new = lg.on_tp_filled(level)
                t.logic = lg.to_dict()
                if new is not None:
                    await self.protection.move_stop(t, new, "split_breakeven")
            ctx.notify("tp", f"TP{level + 1} 체결 [{t.trade_id}] {qty}계약 @ {avg}")
        await self._check_flat(t, f"tp{level + 1}_final")
        if t.state != CLOSED:
            self.protection.refresh_state(t)
        ctx.save(t)

    async def _on_trade_fill(self, t: TradeRecord, ev: OrderUpdate) -> None:
        ctx = self.ctx
        p = ids.parse(ev.client_id)
        if p is None:
            algo = ctx.tracker.algo_for_order(ev.order_id)
            p = ids.parse(algo) if algo else None
        role = p[1] if p else None
        level = p[2] if p else None
        ctx.recompute_accounting(t)
        if role == "TP" and level is not None:
            if ev.status == "FILLED":
                await self._mark_tp_filled(t, level, ev.cum_qty, ev.avg_price)
            else:
                await self._check_flat(t, f"tp{level + 1}_final")
        elif role == "SL":
            ctx.notify("stop", f"손절 체결 [{t.trade_id}] {ev.last_qty}계약 @ {ev.last_price}")
            await self._check_flat(t, self._stop_reason(t))
        elif role in ("EN", "EX", "EM"):
            ctx.save(t)                      # 진입·청산 흐름이 직접 확인한다
        else:
            # 모르는 주문(강제청산·ADL·수동)으로 포지션이 줄었다
            ctx.db.log_risk_event(self.mode, "external_fill",
                                  {"trade_id": t.trade_id, "client_id": ev.client_id,
                                   "order_type": ev.order_type, "qty": str(ev.last_qty)})
            reason = "liquidation" if ev.order_type == "LIQUIDATION" or \
                ev.client_id.startswith("autoclose") else "external_close"
            await self._check_flat(t, reason)
        self._sync_trade()

    # ------------------------------------------------------------------ 입력: 시세
    async def on_market(self, last: Optional[float] = None, mark: Optional[float] = None,
                        index: Optional[float] = None, ts_ms: Optional[int] = None,
                        funding_rate: Optional[float] = None,
                        next_funding_ms: Optional[int] = None) -> None:
        m = self.ctx.market
        if last is not None:
            m.last = float(last)
        if mark is not None:
            m.mark = float(mark)
        if index is not None:
            m.index = float(index)
        if funding_rate is not None:
            m.funding_rate = float(funding_rate)
        if next_funding_ms is not None:
            m.next_funding_ms = int(next_funding_ms)
        if ts_ms is not None:
            m.ts_ms = max(m.ts_ms, int(ts_ms))
        m.updated_mono = self.ctx.mono()
        t = self.trade
        if t is None or t.state != ENTRY_PENDING:
            return
        async with self.lock:
            await self._drain_locked()
            t = self.trade
            if t is None or t.state != ENTRY_PENDING:
                return
            px = m.price(self.settings.entry_trigger_type)
            if px is None:
                return
            ts = (ts_ms / 1000.0) if ts_ms else self.ctx.now()
            await self.entries.on_price(t, px, ts)
            await self._drain_locked()
            self._sync_trade()

    # ------------------------------------------------------------------ 입력: 봉 마감
    async def on_bar_close(self, bars: Bars, htf: Bars,
                           mirrored: Optional[Bars] = None) -> Optional[BarEvaluation]:
        async with self.lock:
            await self._drain_locked()
            return await self._bar_close_locked(bars, htf, mirrored)

    async def _bar_close_locked(self, bars: Bars, htf: Bars,
                                mirrored: Optional[Bars]) -> Optional[BarEvaluation]:
        ctx, s = self.ctx, self.settings
        i = len(bars) - 1
        if i < 60:
            return None
        bar_t, close_t = float(bars.t[i]), bars.close_time(i)
        if self.last_bar_ts is not None and bar_t <= self.last_bar_ts:
            return None
        now = ctx.now()
        if close_t > now + 1.0:
            logger.error("형성 중인 봉이 들어왔다 (close %.0f > now %.0f) - 무시", close_t, now)
            return None
        fresh = (now - close_t) <= s.signal_period_sec * 0.5
        zones = zones_at(htf, close_t, s.zone_bars)
        ev = self.signals.evaluate(bars, i, zones, mirrored)
        self.last_eval = ev
        sids: Dict[int, Optional[int]] = {}
        for sig in ev.entries:
            sids[sig.direction] = ctx.db.insert_signal(
                {"symbol": ctx.symbol, "pattern": sig.pattern, "direction": sig.direction,
                 "bar_time": sig.bar_time, "close_time": sig.close_time, "entry": sig.entry,
                 "stop": sig.stop, "targets": sig.targets, "atr": sig.atr,
                 "features": sig.features, "action": "detected"}, self.mode)
        consumed = set()
        exited_now = False
        t = self.trade
        if t is not None:
            opp = ev.entry_for(-t.direction)
            if t.state == ENTRY_PENDING:
                if t.expire_ts is not None and close_t >= t.expire_ts:
                    await self.entries.cancel(t, "expired")
                elif opp is not None:
                    consumed.add(opp.direction)
                    exited_now = True
                    await self.entries.cancel(t, "opposite_signal", notify=True)
            elif t.state in HOLDING_STATES and not t.adopted and t.logic and \
                    t.entry_fill_time_ms and close_t * 1000 > t.entry_fill_time_ms:
                lg = PositionLogic.from_dict(t.logic)
                reversal = ev.exit_long if t.direction > 0 else ev.exit_short
                dec = lg.on_bar_close(bars, i, reversal, opp is not None)
                t.logic = lg.to_dict()
                t.last_bar_close_ts = close_t
                ctx.save(t)
                if opp is not None:
                    consumed.add(opp.direction)
                if dec.exit_reason:
                    exited_now = True
                    ctx.event(t, "bar_exit", {"reason": dec.exit_reason, "bar": kst(bar_t),
                                              "reversal": ev.reversal_patterns})
                    await self.exits.close(t, dec.exit_reason)
                else:
                    if dec.new_stop is not None:
                        why = "trailing" if dec.trailing else f"ladder_step{dec.ladder_step}"
                        await self.protection.move_stop(t, dec.new_stop, why)
                    if t.state != CLOSED:
                        self.protection.refresh_state(t)
            elif t.state == CLOSING:
                exited_now = True
        self._sync_trade()
        for sig in ev.entries:
            sid = sids.get(sig.direction)
            if sid is None:
                continue                                  # 이미 처리한 봉 (재시작 중복)
            if sig.direction in consumed:
                ctx.db.update_signal(sid, "consumed_for_exit",
                                     "보유/대기 거래를 닫는 데 사용 - 즉시 반전하지 않음")
                continue
            if exited_now or self.trade is not None:
                ctx.db.update_signal(sid, "ignored_busy",
                                     f"진행 중 거래 {self.trade.state if self.trade else 'CLOSING'}")
                continue
            if not fresh:
                ctx.db.update_signal(sid, "ignored_stale_bar", "재시작 뒤 밀린 봉 - 진입 안 함")
                continue
            ok, why = self._entry_gate()
            if not ok and not why.startswith("transient:"):
                ctx.db.update_signal(sid, "ignored_blocked", why)
                continue
            self.trade = self.entries.arm(sig, sid)
            ctx.db.update_signal(sid, "armed", "돌파 대기 등록", self.trade.trade_id)
        self.last_bar_ts = bar_t
        ctx.db.kv_set(f"{self.mode}:last_bar_ts", bar_t)
        return ev

    # ------------------------------------------------------------------ 복구
    async def reconcile(self, reason: str) -> Dict[str, Any]:
        async with self.lock:
            await self._drain_locked()
            rep = await self.recovery.run(reason)
            await self._drain_locked()
            self._sync_trade()
            return rep

    async def startup(self) -> Dict[str, Any]:
        """시작 복구. 끝나기 전에는 새 신호를 받지 않는다."""
        ctx = self.ctx
        if self.mode != "paper":
            try:
                maker, taker = await ctx.gw.get_commission_rate(ctx.symbol)
                if taker > 0:
                    ctx.maker_fee, ctx.taker_fee = maker, taker
            except ExchangeError as e:
                logger.warning("수수료율 조회 실패 - 설정값 사용: %s", e)
            try:
                from ..risk.inverse_math import select_bracket
                br = await ctx.gw.get_leverage_brackets(ctx.symbol)
                if br:
                    ctx.mmr, ctx.cum_btc = select_bracket(br, 0.0)
            except Exception as e:
                logger.warning("레버리지 구간 조회 실패 - exchangeInfo 유지증거금률 사용: %s", e)
        return await self.reconcile("startup")
