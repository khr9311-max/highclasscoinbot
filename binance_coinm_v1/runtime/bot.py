"""
봇 런타임 (paper 기본).

시작 순서:
  1. 설정·비밀값 마스킹 등록 -> 새 DB (state/coinm_v1.sqlite3)
  2. 서버 시간 동기화 -> exchangeInfo -> 계약 해석 (실패하면 거래하지 않고 종료)
  3. 게이트웨이: paper = PaperGateway(DB 에 저장된 종이 계좌 복원) / live·testnet = BinanceGateway
     (서명 요청은 전부 LiveOrderGate 를 거친다)
  4. 초기 시세 (마크·지수·체결가·펀딩)
  5. [live/testnet] 계정 설정 확인: 원웨이(아니면 정지, 봇이 바꾸지 않음), 격리(포지션 없을
     때만 변경), 레버리지(설정보다 높으면 낮추기만 - 자동 상향 금지)
  6. 시작 복구 (execution/recovery_manager) - 끝나야 신규 신호 허용
  7. 상시 작업: 시세 웹소켓, [live] 사용자 데이터 스트림 + listenKey 연장, 1시간봉 마감
     스케줄러, 사용자 이벤트 펌프, 주기 대사(5분), 계좌 스냅샷(15분), 하트비트, 종이 계좌 저장

REST 는 시작·대사·복구·명시적 조회에만, 실시간 상태는 웹소켓으로 받는다.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Callable, Dict, Optional

from ..config.settings import ConfigError, Settings
from ..exchange.binance_gateway import BinanceGateway
from ..exchange.contract import resolve_contract
from ..exchange.errors import ContractResolutionError, ExchangeError, LiveOrderBlocked
from ..exchange.market_data import MarketData
from ..exchange.paper_gateway import PaperGateway
from ..exchange.rest_client import BinanceRestClient, endpoints
from ..exchange.websocket import (KlineEvent, ListenKeyManager, MarkPriceEvent, MarketStream,
                                  TradeEvent, UserDataStream, default_connect)
from ..execution.engine import Engine
from ..execution.live_gate import LiveOrderGate
from ..notifications.telegram import Notifier, build_notifier, kst
from ..storage.db import Database
from ..storage.redact import GLOBAL_REDACTOR
from ..validation.gate import ValidationGate
from .accounting import FxProvider, build_snapshot

logger = logging.getLogger(__name__)


class Bot:
    def __init__(self, settings: Settings, transport: Any = None,
                 ws_connect: Callable = default_connect, notifier: Optional[Notifier] = None,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.transport = transport
        self.ws_connect = ws_connect
        self.notifier = notifier or build_notifier(settings)
        self.clock = clock
        self.stop_event = asyncio.Event()
        self.fx = FxProvider(settings.usd_krw_source, settings.usd_krw_rate)
        self.db: Optional[Database] = None
        self.engine: Optional[Engine] = None
        self.gateway: Any = None
        self.spec: Any = None
        self.public: Optional[BinanceRestClient] = None
        self.private: Optional[BinanceRestClient] = None
        self.md: Optional[MarketData] = None
        self.market_stream: Optional[MarketStream] = None
        self.user_stream: Optional[UserDataStream] = None
        self.live_gate: Optional[LiveOrderGate] = None
        self._evt = asyncio.Event()
        self._bar_evt = asyncio.Event()
        self._gate_open_last: Optional[bool] = None
        self._stale_notified = False
        self.stats: Dict[str, int] = {"market_events": 0, "bars": 0, "reconciles": 0}

    @property
    def paper(self) -> bool:
        return self.settings.execution_mode == "paper"

    # ------------------------------------------------------------------ 시작
    async def setup(self) -> Dict[str, Any]:
        s = self.settings
        GLOBAL_REDACTOR.add(*s.secrets())
        self.db = Database(s.db_path)
        rest_url, self.ws_base = endpoints(s.binance_env)
        self.public = BinanceRestClient(rest_url, transport=self.transport, recv_window=s.recv_window_ms,
                                        clock=self.clock)
        self.md = MarketData(self.public)
        try:
            await self.public.sync_time()
            xi = await self.md.exchange_info()
            self.spec = resolve_contract(xi, s.symbol)
        except (ExchangeError, ContractResolutionError) as e:
            self.notifier.notify("critical", f"계약 확인 실패 - 거래하지 않음: {e}", critical=True)
            raise
        vgate = ValidationGate(s, self.db, self.spec.essentials(), clock=self.clock)
        self.live_gate = LiveOrderGate(
            s, rest_url, vgate.check,
            on_block=lambda what, why: self.db.log_risk_event(s.execution_mode, "live_gate_block",
                                                              {"request": what, "reasons": why}))
        if self.paper:
            self.gateway = PaperGateway(self.spec, s, clock=self.clock)
            st = self.db.kv_get("paper:paper_state")
            if st:
                self.gateway.load_state(st)
                logger.info("종이 계좌 복원: 지갑 %.8f BTC, 포지션 %s", self.gateway.wallet,
                            self.gateway.pos_qty)
        else:
            if not s.has_api_keys:
                raise ConfigError(f"EXECUTION_MODE={s.execution_mode} 는 API 키가 필요합니다")
            self.private = BinanceRestClient(rest_url, s.api_key, s.api_secret,
                                             transport=self.transport, recv_window=s.recv_window_ms,
                                             clock=self.clock, mutation_guard=self.live_gate.check)
            self.private.time_offset_ms = self.public.time_offset_ms
            self.gateway = BinanceGateway(self.private, s.execution_mode)
        self.engine = Engine(s, self.gateway, self.db, self.spec, self.notifier, clock=self.clock,
                             live_gate=self.live_gate)
        self.gateway.set_event_sink(self._sink)
        pi = await self.md.premium_index(s.symbol)
        last, _ = await self.md.ticker_price(s.symbol)
        await self._apply_market(last=last, mark=pi.mark_price, index=pi.index_price,
                                 ts_ms=pi.time_ms, funding_rate=pi.last_funding_rate,
                                 next_funding_ms=pi.next_funding_time_ms)
        if not self.paper:
            await self._ensure_account_config()
        rep = await self.engine.startup()
        gate = self.live_gate.status()
        self._gate_open_last = gate["open"]
        self.notifier.notify("startup", (
            f"모드 {s.execution_mode} ({s.binance_env}) · {s.symbol}\n"
            f"계약 contractSize={self.spec.contract_size} tick={self.spec.tick_size} "
            f"step={self.spec.step_size} minQty={self.spec.min_qty} 증거금 {self.spec.margin_asset}\n"
            f"레버리지 {s.leverage}x 격리 · 위험 {s.risk_per_trade_pct}%/거래 · 일손실 한도 "
            f"{s.max_daily_loss_pct}%\n신규 진입 {'허용' if rep.get('trading_allowed') else '차단'} · "
            f"실거래 게이트 {'열림' if gate['open'] else '닫힘'}"
            + ("" if gate["open"] else f" ({'; '.join(gate['reasons'])[:200]})")))
        self.db.log_event(s.execution_mode, "startup", {"recovery": rep, "gate": gate,
                                                        "contract": self.spec.to_dict()})
        return rep

    async def _ensure_account_config(self) -> None:
        s, gw, eng = self.settings, self.gateway, self.engine
        try:
            if await gw.get_position_mode():
                eng.halts["position_mode"] = "헤지 모드 - 원웨이로 직접 바꿔야 함 (UM 과 공유 설정)"
                self.notifier.notify("critical", eng.halts["position_mode"], critical=True)
                return
            pos = await gw.get_position(s.symbol)
        except ExchangeError as e:
            eng.halts["account_config"] = f"계정 설정 조회 실패: {type(e).__name__}"
            return
        if pos is None:
            return
        try:
            if pos.qty == 0:
                if pos.margin_type != "isolated":
                    await gw.set_margin_type(s.symbol, "ISOLATED")
                if pos.leverage > s.leverage:
                    await gw.set_leverage(s.symbol, s.leverage)       # 낮추기만
                elif pos.leverage and pos.leverage < s.leverage:
                    logger.warning("거래소 레버리지 %dx < 설정 %dx - 자동으로 올리지 않음 "
                                   "(필요 증거금만 늘어난다)", pos.leverage, s.leverage)
            elif pos.margin_type != "isolated":
                eng.halts["account_config"] = "보유 포지션이 격리가 아님 - 수동 확인 필요"
        except LiveOrderBlocked as e:
            logger.warning("계정 설정 변경이 LiveOrderGate 에 막힘: %s", e)
        except ExchangeError as e:
            eng.halts["account_config"] = f"계정 설정 변경 실패: {e}"

    # ------------------------------------------------------------------ 이벤트
    def _sink(self, raw: Dict[str, Any]) -> None:
        self.engine.enqueue_user_event(raw)
        self._evt.set()

    async def _apply_market(self, **kw: Any) -> None:
        if self.paper:
            self.gateway.update_market(**kw)
        await self.engine.on_market(**kw)
        if self.engine.user_q:
            await self.engine.drain_user_events()

    async def _on_market_event(self, ev: Any) -> None:
        self.stats["market_events"] += 1
        if isinstance(ev, KlineEvent):
            if ev.closed:
                self._bar_evt.set()
            return
        if isinstance(ev, MarkPriceEvent):
            await self._apply_market(mark=ev.mark_price, index=ev.index_price, ts_ms=ev.event_time_ms,
                                     funding_rate=ev.funding_rate, next_funding_ms=ev.next_funding_ms)
        elif isinstance(ev, TradeEvent):
            await self._apply_market(last=ev.price, ts_ms=ev.trade_time_ms)

    async def _on_user_reconnected(self, why: str) -> None:
        self.stats["reconciles"] += 1
        await self.engine.reconcile(f"user_stream_{why}")
        if why == "reconnected":
            self.notifier.notify("ws_reconnect", "사용자 데이터 스트림 재연결 - REST 대사 완료")

    async def _on_market_disconnected(self, reason: str) -> None:
        if self.market_stream and self.market_stream.stream.failures in (3, 20):
            self.notifier.notify("connection_error", f"시세 웹소켓 연결 문제: {reason}")

    async def _on_market_connected(self, n: int) -> None:
        logger.info("시세 웹소켓 연결 (%d회째)", n)
        if n > 1:
            self.notifier.notify("ws_reconnect", f"시세 웹소켓 재연결 ({n}회째)")
            self._bar_evt.set()                   # 끊긴 사이 마감된 봉 확인

    # ------------------------------------------------------------------ 작업
    async def _event_pump(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self._evt.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self._evt.clear()
            await self.engine.drain_user_events()

    async def _bar_loop(self) -> None:
        period = self.settings.signal_period_sec
        while not self.stop_event.is_set():
            now = self.clock()
            next_close = (math.floor(now / period) + 1) * period
            wait = max(1.0, next_close + 3.0 - now)
            try:
                await asyncio.wait_for(self._bar_evt.wait(), timeout=min(wait, 60.0))
            except asyncio.TimeoutError:
                if self.clock() < next_close + 3.0:
                    continue
            self._bar_evt.clear()
            try:
                await self.process_bars()
            except Exception as e:
                logger.exception("봉 처리 실패: %s", e)
                self.notifier.notify("api_error", f"봉 처리 실패: {type(e).__name__}")

    async def process_bars(self) -> int:
        s = self.settings
        bars = await self.md.closed_bars(s.symbol, s.signal_interval, 300)
        htf = await self.md.closed_bars(s.symbol, s.zone_interval, max(250, s.zone_bars + 10))
        if len(bars) < 61 or len(htf) < 60:
            return 0
        last = self.engine.last_bar_ts
        idx = [k for k in range(len(bars)) if last is None or float(bars.t[k]) > last]
        if last is None:
            idx = idx[-1:]                        # 첫 실행: 과거 봉을 재생하지 않는다
        for k in idx:
            ev = await self.engine.on_bar_close(bars.upto(k + 1), htf)
            if ev is not None:
                logger.info("봉 마감 %s UTC 처리 · 신호 %s · 반전 %s · 상태 %s",
                            time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(bars.t[k]))),
                            [x.side for x in ev.entries] or "없음", ev.reversal_patterns or "없음",
                            self.engine.state)
        self.stats["bars"] += len(idx)
        return len(idx)

    async def _periodic(self) -> None:
        s = self.settings
        mode = s.execution_mode
        last_rec = last_snap = last_status = self.clock()
        last_funding = last_gate = 0.0
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=10.0)
                return
            except asyncio.TimeoutError:
                pass
            now = self.clock()
            try:
                self.db.kv_set(f"{mode}:heartbeat", now)
                if self.paper:
                    self.db.kv_set("paper:paper_state", self.gateway.to_state())
                if now - last_rec >= s.reconcile_interval_sec:
                    last_rec = now
                    self.stats["reconciles"] += 1
                    await self.engine.reconcile("periodic")
                if now - last_snap >= 900:
                    last_snap = now
                    await self.snapshot()
                if now - last_status >= 300:
                    last_status = now
                    m = self.engine.ctx.market
                    logger.info("상태 %s · 진입 %s · mark %s · index %s · 봉 %d · 시세 이벤트 %d · 대사 %d",
                                self.engine.state,
                                "허용" if self.engine.trading_allowed and not self.engine.halts
                                else f"차단 {list(self.engine.halts)}", m.mark, m.index,
                                self.stats["bars"], self.stats["market_events"],
                                self.stats["reconciles"])
                if not self.paper and now - last_funding >= 3600:
                    last_funding = now
                    await self.engine.ctx.sync_funding(self.engine.trade, int((now - 86400) * 1000))
                if now - last_gate >= 3600:
                    last_gate = now
                    st = self.live_gate.status()
                    if self._gate_open_last is not None and st["open"] != self._gate_open_last:
                        self.notifier.notify("validation_gate",
                                             f"실거래 게이트 {'열림' if st['open'] else '닫힘'}: "
                                             f"{'; '.join(st['reasons']) or 'ok'}")
                    self._gate_open_last = st["open"]
                age = self.engine.ctx.market.age(self.engine.ctx.mono())
                if age > 60 and not self._stale_notified:
                    self._stale_notified = True
                    self.notifier.notify("connection_error", f"시세가 {age:.0f}초째 갱신되지 않음 - 신규 진입 보류")
                elif age <= 60:
                    self._stale_notified = False
            except Exception as e:
                logger.exception("주기 작업 실패: %s", e)

    async def snapshot(self) -> Dict[str, Any]:
        acct = await self.gateway.get_account()
        pos = await self.gateway.get_position(self.settings.symbol)
        m = self.engine.ctx.market
        rate = await self.fx.usd_krw()
        self.engine.ctx.usd_krw = rate
        snap = build_snapshot(acct, pos, self.spec, m.mark, m.index, rate, self.settings.execution_mode)
        self.db.insert_account_snapshot(snap, self.settings.execution_mode)
        # 일일 손실 한도의 '하루 시작 equity' 를 진입 시도 전에 미리 잡아 둔다 (UTC 날짜 기준)
        self.engine.limits.day_state(snap["equity_btc"], snap["ts"])
        return snap

    # ------------------------------------------------------------------ 실행
    async def run(self, duration: Optional[float] = None) -> None:
        s = self.settings
        await self.setup()
        await self.notifier.start()
        self.market_stream = MarketStream(self.ws_base, s.symbol, s.signal_interval,
                                          self._on_market_event, connect=self.ws_connect,
                                          stale_after=s.market_ws_stale_sec,
                                          on_connected=self._on_market_connected,
                                          on_disconnected=self._on_market_disconnected)
        tasks = [asyncio.create_task(self.market_stream.run(self.stop_event), name="market_ws"),
                 asyncio.create_task(self._bar_loop(), name="bars"),
                 asyncio.create_task(self._event_pump(), name="events"),
                 asyncio.create_task(self._periodic(), name="periodic")]
        if not self.paper:
            lk = ListenKeyManager(self.gateway)
            self.user_stream = UserDataStream(self.ws_base, lk, self._sink,
                                              on_reconnected=self._on_user_reconnected,
                                              connect=self.ws_connect)
            tasks.append(asyncio.create_task(self.user_stream.run(self.stop_event), name="user_ws"))
            tasks.append(asyncio.create_task(self.user_stream.keepalive_loop(self.stop_event),
                                             name="listenkey"))
        snap = await self.snapshot()
        logger.info("시작 equity %.8f BTC (= %.2f USD / %.0f KRW) · 상태 %s",
                    snap["equity_btc"], snap["equity_usd"] or 0, snap["equity_krw"] or 0,
                    self.engine.state)
        self._bar_evt.set()                       # 시작 즉시 최근 마감 봉 확인 (진입은 신선한 봉만)
        try:
            if duration is not None:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=duration)
                except asyncio.TimeoutError:
                    pass
            else:
                await self.stop_event.wait()
        finally:
            await self.shutdown(tasks)

    async def shutdown(self, tasks=()) -> None:
        self.stop_event.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        try:
            if self.engine is not None:
                await self.engine.drain_user_events()
            if self.paper and self.gateway is not None and self.db is not None:
                self.db.kv_set("paper:paper_state", self.gateway.to_state())
            if self.db is not None:
                self.db.kv_set(f"{self.settings.execution_mode}:heartbeat", self.clock())
        except Exception:
            logger.exception("종료 저장 실패")
        self.notifier.notify("shutdown", f"봇 종료 ({kst()}) · 상태 {self.engine.state if self.engine else '-'}")
        await self.notifier.stop()
        for c in (self.public, self.private):
            if c is not None:
                try:
                    await c.close()
                except Exception:
                    pass
        if self.db is not None:
            self.db.close()
