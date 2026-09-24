"""
가격행동(Naked Forex) 전략 실행기.

구성:
  Trade      - 한 신호의 수명(대기 -> 보유 -> 청산). 매수스톱 진입, 구조적 손절,
               존 목표가/분할/사다리/3봉 추적 청산(11장)을 한 상태기계로 처리한다.
  PaperBook  - 모든 신호를 종이 매매로 굴린다. 청산 방식 5가지를 동시에 돌려
               검증(DSR/PBO)용 비교 데이터를 만든다. 백테스트도 이걸 그대로 쓴다.
  NakedStrategy - 라이브 연결부. 봉 마감마다 캔들을 받아 판정하고, 웹소켓이
               붙은 종목은 틱으로 진입·청산을 실제 주문으로 낸다.

종이 매매와 라이브를 둘 다 두는 이유: 라이브 체결은 필터(뉴스·메타·서킷
브레이커·리스크 한도)를 타서 표본이 치우친다. 전략 자체의 성과는 필터와
무관하게 모든 신호로 재야 검증이 성립한다.

종이 매매의 봉 내부 가정은 보수적으로 둔다:
  - 같은 봉에서 진입과 손절이 둘 다 가능하면 '진입 후 손절' 로 본다
  - 같은 봉에서 손절과 목표가 둘 다 닿으면 손절로 본다
  - 진입한 봉에서는 목표가 도달을 인정하지 않는다
  - 갭(시가가 손절 아래)이면 시가에 청산
"""

import asyncio
import json
import logging
import os
import time
import uuid as uuidlib
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from price_action import (Bars, ENTRY, EXIT_ONLY, ENTRY_PATTERNS, Signal, Zone,
                          atr, evaluate_bar, find_zones)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

# meta_trainer.ROUND_TRIP_COST 와 같은 값. 업비트 수수료 0.05% x2 + 스프레드.
ROUND_TRIP_COST = 0.0012

# 'auto'(존이 있으면 존, 없으면 3봉 추적)는 따로 두지 않는다. zone 이 목표 존이
# 없을 때 이미 3봉 추적으로 가므로 규칙이 같다 - 같은 전략이 시행 집합에 두 번
# 들어가면 DSR 시행 수가 부풀고 PBO 행렬이 중복된다. 설정값 auto 는 zone 으로 읽는다.
EXIT_MODES = ("zone", "split", "ladder", "three_bar")

# 메타 모델 입력. 순서를 바꾸면 기존 모델이 무효가 된다.
FEATURE_KEYS = ("range_atr", "close_pos", "vol_ratio", "trend_atr", "trend_r2",
                "rr", "risk_pct", "room", "zone_touches")


def feature_row(sig: Dict[str, Any], is_alt: bool) -> List[float]:
    f = sig.get("features") or {}
    row = [float(f.get(k, 0.0) or 0.0) for k in FEATURE_KEYS]
    row += [1.0 if sig.get("pattern") == p else 0.0 for p in ENTRY_PATTERNS]
    row += [1.0 if is_alt else 0.0]
    return row


def feature_names() -> List[str]:
    return list(FEATURE_KEYS) + [f"p_{p}" for p in ENTRY_PATTERNS] + ["is_alt"]


# ---------------------------------------------------------------------------
# 한 거래
# ---------------------------------------------------------------------------
class Trade:
    def __init__(self, ticker: str, sig: Dict[str, Any], mode: str, period: int,
                 valid_bars: int = 2, max_hold_bars: int = 72, live: bool = False,
                 is_alt: bool = False, trade_id: Optional[str] = None):
        self.id = trade_id or uuidlib.uuid4().hex[:12]
        self.ticker = ticker
        self.pattern = sig["pattern"]
        self.signal = sig
        self.variant = mode
        self.live = live
        self.is_alt = is_alt
        self.period = int(period)

        self.entry = float(sig["entry"])
        self.stop = float(sig["stop"])
        self.init_stop = self.stop
        self.targets = [float(x) for x in sig.get("targets") or []]
        self.soft_exit = sig.get("soft_exit")

        # auto: 목표 존이 있으면 존에서 전량(gunner), 없으면(신고가 부근) 3봉 추적(runner)
        if mode == "auto":
            mode = "zone" if self.targets else "three_bar"
        if mode in ("zone", "split") and not self.targets:
            mode = "three_bar"
        self.mode = mode

        self.created_ts = float(sig["close_time"])
        self.expire_ts = self.created_ts + valid_bars * self.period
        self.max_hold_bars = max_hold_bars

        self.status = "pending"
        self.fill_px: Optional[float] = None
        self.fill_ts: Optional[float] = None
        self.bars_held = 0
        self.remaining = 1.0
        self.fills: List[Tuple[float, float, float, str]] = []   # (frac, px, ts, reason)
        self.ladder_step = 0
        self.exit_ts: Optional[float] = None
        self.exit_reason = ""
        self.last_bar_ts = float(sig["bar_time"])   # 이 봉까지는 이미 반영됨
        # 라이브 전용
        self.volume = 0.0
        self.krw = 0.0
        self.busy = False
        self.pending_exit: Optional[str] = None   # 봉 마감 청산 사유 (다음 틱에 매도)

    # ---------------- 상태 조회 ----------------
    @property
    def done(self) -> bool:
        return self.status in ("closed", "cancelled")

    def current_target(self) -> Optional[float]:
        if self.mode == "zone":
            return self.targets[0] if self.targets else None
        if self.mode == "split":
            if self.remaining >= 1.0:
                return self.targets[0] if self.targets else None
            return self.targets[1] if len(self.targets) > 1 else None
        return None

    def target_fraction(self) -> float:
        if self.mode == "split" and self.remaining >= 1.0:
            return 0.5
        return self.remaining

    def trailing(self) -> bool:
        if self.mode == "three_bar":
            return True
        # 분할 청산 후 두 번째 존이 없으면 나머지는 추적으로 (11장 split 의 보완)
        return self.mode == "split" and self.remaining < 1.0 and len(self.targets) < 2

    # ---------------- 상태 변경 ----------------
    def mark_filled(self, px: float, ts: float):
        self.status = "open"
        self.fill_px = float(px)
        self.fill_ts = float(ts)

    def mark_exit(self, frac: float, px: float, ts: float, reason: str):
        frac = min(frac, self.remaining)
        self.fills.append((frac, float(px), float(ts), reason))
        self.remaining = round(self.remaining - frac, 10)
        if self.mode == "split" and self.remaining > 0 and reason == "target":
            # 첫 목표 도달 후 나머지는 본전 손절 (11장 split exit)
            self.stop = max(self.stop, self.fill_px or self.stop)
        if self.remaining <= 1e-9:
            self.remaining = 0.0
            self.status = "closed"
            self.exit_ts = float(ts)
            self.exit_reason = reason

    def cancel(self, ts: float, reason: str):
        self.status = "cancelled"
        self.exit_ts = float(ts)
        self.exit_reason = reason

    # ---------------- 종이 매매: 봉 하나 ----------------
    def on_bar(self, t: float, o: float, h: float, l: float, c: float):
        if self.done or t <= self.last_bar_ts:
            return
        if self.status == "pending":
            if t >= self.expire_ts:
                self.cancel(t, "expired")
                return
            if h >= self.entry:
                self.mark_filled(max(self.entry, o), t)
                if l <= self.stop:              # 같은 봉 진입 후 손절 (보수적)
                    self.mark_exit(self.remaining, min(self.stop, max(self.entry, o)), t, "stop")
                return
            if l <= self.stop:
                # 진입 전에 손절선부터 닿았다 - 신호 무효 (13장)
                self.cancel(t, "stop_before_entry")
            return

        if self.status == "open":
            if l <= self.stop:
                self.mark_exit(self.remaining, min(self.stop, o), t, "stop")
                return
            for _ in range(3):
                tgt = self.current_target()
                if tgt is None or h < tgt:
                    break
                self.mark_exit(self.target_fraction(), max(tgt, o), t, "target")
                if self.done:
                    return

    # ---------------- 봉 마감 처리 (종이·라이브 공통) ----------------
    def on_bar_close(self, bars: Bars, i: int, reversal: bool) -> Optional[str]:
        """
        마감된 봉 i 기준 갱신. 즉시 청산해야 하면 사유를 돌려준다
        (종이는 종가로 청산, 라이브는 호출부가 시장가로 판다).
        """
        t = float(bars.t[i])
        if self.done or self.status != "open" or t < (self.fill_ts or 0):
            self.last_bar_ts = max(self.last_bar_ts, t)
            return None
        self.last_bar_ts = max(self.last_bar_ts, t)
        self.bars_held += 1
        c = float(bars.c[i])
        buf = 0.05 * atr(bars, i)

        # 사다리 (11장 ladder): 다음 존에 닿으면 손절을 한 칸 올린다
        if self.mode == "ladder":
            while self.ladder_step < len(self.targets) and bars.h[i] >= self.targets[self.ladder_step]:
                new_stop = self.fill_px if self.ladder_step == 0 else self.targets[self.ladder_step - 1]
                self.stop = max(self.stop, float(new_stop))
                self.ladder_step += 1
            if self.ladder_step >= len(self.targets) and self.bars_held >= 3:
                self.stop = max(self.stop, float(bars.l[i - 2:i + 1].min()) - buf)

        # 3봉 추적 (11장 three-bar exit): 최근 3봉 최저가 아래
        if self.trailing() and self.bars_held >= 3:
            self.stop = max(self.stop, float(bars.l[i - 2:i + 1].min()) - buf)

        if self.soft_exit is not None and c < float(self.soft_exit):
            return "back_in_box"          # 라스트 키스: 종가가 박스 안으로 (5장)
        if reversal:
            return "reversal_signal"      # 저항 존의 약세 반전 (13장)
        if self.bars_held >= self.max_hold_bars:
            return "time"
        # 봉 마감 시점에 이미 손절선 아래 (추적 손절이 올라온 경우)
        if c <= self.stop:
            return "stop_close"
        return None

    # ---------------- 라이브: 틱 ----------------
    def on_price(self, px: float, ts: float) -> Optional[Tuple[str, float, str]]:
        """(행동, 비율, 사유). 행동: enter / exit / cancel."""
        if self.done:
            return None
        if self.status == "pending":
            if ts >= self.expire_ts:
                return ("cancel", 0.0, "expired")
            if px <= self.stop:
                return ("cancel", 0.0, "stop_before_entry")
            if px >= self.entry:
                return ("enter", 1.0, "buy_stop")
            return None
        if px <= self.stop:
            return ("exit", self.remaining, "stop")
        tgt = self.current_target()
        if tgt is not None and px >= tgt:
            return ("exit", self.target_fraction(), "target")
        return None

    # ---------------- 결과 ----------------
    def result(self) -> Dict[str, Any]:
        gross = 0.0
        if self.fill_px:
            gross = sum(f * (px / self.fill_px - 1.0) for f, px, _, _ in self.fills)
        filled = self.fill_px is not None
        net = gross - ROUND_TRIP_COST if filled else 0.0
        risk = (self.fill_px - self.init_stop) / self.fill_px if filled and self.fill_px > self.init_stop else None
        return {
            "id": self.id, "ticker": self.ticker, "pattern": self.pattern,
            "variant": self.variant, "mode": self.mode, "live": self.live,
            "is_alt": self.is_alt, "status": self.status, "filled": filled,
            "signal_ts": self.created_ts, "fill_ts": self.fill_ts, "exit_ts": self.exit_ts,
            "entry": self.entry, "fill_px": self.fill_px, "init_stop": self.init_stop,
            "targets": self.targets, "exit_reason": self.exit_reason,
            "bars_held": self.bars_held, "gross_ret": gross, "net_ret": net,
            "r_net": (net / risk) if risk else None,
            "fills": self.fills, "signal": self.signal,
        }

    # ---------------- 영속화 ----------------
    def to_state(self) -> Dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "busy"}
        return d

    @classmethod
    def from_state(cls, d: Dict[str, Any]) -> "Trade":
        tr = cls.__new__(cls)
        tr.__dict__.update(d)
        tr.fills = [tuple(x) for x in d.get("fills", [])]
        tr.busy = False
        tr.__dict__.setdefault("pending_exit", None)
        return tr


# ---------------------------------------------------------------------------
# 종이 매매 장부 (라이브 스캔·백테스트 공용)
# ---------------------------------------------------------------------------
class PaperBook:
    def __init__(self, variants: Sequence[str] = EXIT_MODES,
                 patterns: Sequence[str] = ENTRY_PATTERNS,
                 min_rr: float = 1.0, valid_bars: int = 2, max_hold_bars: int = 72):
        self.variants = tuple(variants)
        self.patterns = tuple(patterns)
        self.min_rr = min_rr
        self.valid_bars = valid_bars
        self.max_hold_bars = max_hold_bars
        self.open: Dict[str, List[Trade]] = {}

    def process_bar(self, ticker: str, bars: Bars, i: int, zones: List[Zone],
                    is_alt: bool = False) -> Tuple[List[Signal], List[Trade]]:
        """봉 i 마감 처리. (이번 봉에서 나온 신호, 이번 봉에서 끝난 거래)."""
        sigs = evaluate_bar(bars, i, zones, self.patterns, self.min_rr)
        reversal = any(s.kind == EXIT_ONLY for s in sigs)
        t, o, h, l, c = (float(bars.t[i]), float(bars.o[i]), float(bars.h[i]),
                         float(bars.l[i]), float(bars.c[i]))

        finished: List[Trade] = []
        keep: List[Trade] = []
        for tr in self.open.get(ticker, []):
            tr.on_bar(t, o, h, l, c)
            if not tr.done:
                why = tr.on_bar_close(bars, i, reversal)
                if why:
                    tr.mark_exit(tr.remaining, c, t + bars.period, why)
            (finished if tr.done else keep).append(tr)

        # 종목·청산방식·패턴마다 거래는 하나 (진행 중이면 같은 패턴 새 신호는 건너뜀).
        # 패턴 간에는 막지 않는다 - 막으면 '와미 종이 거래가 열려 있어서 추세
        # 캥거루를 못 탔다' 는 식으로, 라이브 패턴만 거래하는 실제 운용과 다른
        # 표본이 된다. 여러 패턴을 함께 라이브로 쓸 때의 '종목당 1거래' 는
        # validate_naked.non_overlap 이 평가 단계에서 적용한다.
        busy = {(tr.variant, tr.pattern) for tr in keep}
        for s in sigs:
            if s.kind != ENTRY:
                continue
            d = s.to_dict()
            for v in self.variants:
                if (v, s.pattern) in busy:
                    continue
                keep.append(Trade(ticker, d, v, bars.period, self.valid_bars,
                                  self.max_hold_bars, live=False, is_alt=is_alt))
                busy.add((v, s.pattern))
        self.open[ticker] = keep
        return sigs, finished

    def all_open(self) -> List[Trade]:
        return [tr for lst in self.open.values() for tr in lst]


# ---------------------------------------------------------------------------
# 라이브 연결부
# ---------------------------------------------------------------------------
class NakedStrategy:
    """
    main.py 가 쓰는 진입점.
      scan()     - 봉 마감마다(내부에서 판단) 캔들 받아 판정 + 종이 매매 갱신
      on_tick()  - 웹소켓 종목의 라이브 진입·청산 (매 틱)

    주입받는 것:
      buy(ticker, krw) -> bool, sell(ticker, volume) -> bool   (실행 엔진)
      price_of(ticker) -> Optional[float]                        (웹소켓 최신가)
      entry_gate(ticker, signal_dict) -> (bool, str)             (뉴스·메타·CB 필터)
    """

    def __init__(self, state_dir: str, feed, cfg, universe=None,
                 buy: Optional[Callable] = None, sell: Optional[Callable] = None,
                 price_of: Optional[Callable] = None,
                 entry_gate: Optional[Callable] = None,
                 notify: Optional[Callable] = None,
                 watch: Optional[Callable] = None,
                 unwatch: Optional[Callable] = None):
        self.cfg = cfg
        self.feed = feed
        self.universe = universe
        self.buy, self.sell, self.price_of = buy, sell, price_of
        self.entry_gate = entry_gate
        self.notify = notify
        self.watch, self.unwatch = watch, unwatch

        self.base = os.path.join(state_dir, "naked")
        self.sig_dir = os.path.join(self.base, "signals")
        self.trade_dir = os.path.join(self.base, "trades")
        for d in (self.sig_dir, self.trade_dir):
            os.makedirs(d, exist_ok=True)
        self.state_path = os.path.join(self.base, "state.json")

        self.core = list(cfg.TARGET_TICKERS)
        self.live_tickers = set(cfg.NAKED_LIVE_TICKERS)
        self.live_patterns = set(cfg.NAKED_LIVE_PATTERNS)
        self.live_enabled = bool(cfg.STRATEGY_MODE == "naked" and cfg.NAKED_LIVE)

        self.book = PaperBook(EXIT_MODES, cfg.NAKED_PATTERNS, cfg.NAKED_MIN_RR,
                              cfg.NAKED_ENTRY_VALID_BARS, cfg.NAKED_MAX_HOLD_BARS)
        self.live: Dict[str, Trade] = {}           # ticker -> 라이브 거래 (종목당 1개)
        self.last_bar: Dict[str, float] = {}
        self._zone_cache: Dict[str, Tuple[int, float, List[Zone]]] = {}
        self.stats = {"signals": 0, "paper_closed": 0, "live_entries": 0}
        self._notify_tasks: set = set()
        self._drive_tasks: set = set()
        self._stopping = False
        self._load()

    # ---------------- 유니버스 ----------------
    def tickers(self) -> List[str]:
        alts = list(self.universe.tickers) if (self.universe and self.cfg.NAKED_ALTS_ENABLED) else []
        seen, out = set(), []
        for t in self.core + alts:
            if t not in seen:
                seen.add(t)
                out.append(t)
        # 종이·라이브 거래가 남아 있는 종목은 유니버스에서 빠져도 끝까지 추적한다
        # (라이브는 봉 마감 청산 - 시간 초과·반대 신호·3봉 추적 - 을 스캔에서 받는다)
        pending = [t for t, lst in self.book.open.items() if lst] + list(self.live)
        for t in pending:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def is_alt(self, ticker: str) -> bool:
        return ticker not in self.core

    # ---------------- 스캔 ----------------
    def _zones(self, ticker: str, htf: Bars, ts: float) -> List[Zone]:
        h = htf.closed_by(ts)
        h = h.tail(self.cfg.NAKED_ZONE_BARS)
        key = (len(h), float(h.t[-1]) if len(h) else 0.0)
        cached = self._zone_cache.get(ticker)
        if cached and cached[:2] == key:
            return cached[2]
        z = find_zones(h)
        self._zone_cache[ticker] = (key[0], key[1], z)
        return z

    async def scan(self) -> int:
        """새로 마감된 봉이 있는 종목만 처리. 처리한 봉 수를 돌려준다."""
        n = 0
        for t in self.tickers():
            try:
                n += await self._scan_ticker(t)
            except Exception as e:
                logger.exception("가격행동 스캔 실패 %s: %s", t, e)
        if n:
            self._save()
        return n

    async def _scan_ticker(self, ticker: str) -> int:
        ltf = await self.feed.get_bars(ticker, self.cfg.NAKED_TF_MIN, 200)
        if ltf is None or len(ltf) < 60:
            return 0
        last = self.last_bar.get(ticker)
        if last is not None and float(ltf.t[-1]) <= last:
            return 0
        htf = await self.feed.get_bars(ticker, self.cfg.NAKED_ZONE_TF_MIN, 200)
        if htf is None or len(htf) < 60:          # 신규 상장: 존을 그릴 이력 부족
            self.last_bar[ticker] = float(ltf.t[-1])
            return 0

        if last is None:
            idx = [len(ltf) - 1]                   # 첫 스캔은 과거를 재생하지 않는다
        else:
            idx = [i for i in range(len(ltf)) if ltf.t[i] > last]
            if idx and idx[0] == 0 and float(ltf.t[0]) > last + ltf.period:
                # 받아온 200봉보다 오래 꺼져 있었다. 사이 봉을 못 봤으므로 이
                # 종목의 종이 거래는 결과를 믿을 수 없다 - 성과 집계에서 빼려고
                # 취소 처리한다 (validate 는 closed 만 센다). 라이브 거래는 실제
                # 포지션이라 그대로 관리한다.
                for tr in self.book.open.pop(ticker, []):
                    tr.cancel(float(ltf.t[0]), "data_gap")
                    self._record_trade(tr, "paper")
                logger.warning("가격행동 %s: 받아온 봉보다 긴 데이터 공백 - 종이 거래 취소", ticker)
        # 라이브 진입은 방금 마감된 봉에서만. 재시작 뒤 밀린 봉을 재생할 때
        # 옛 신호로 주문을 내면 안 된다 (종이 매매는 재생해도 된다).
        fresh_after = time.time() - 1.5 * ltf.period
        for i in idx:
            zones = self._zones(ticker, htf, ltf.close_time(i))
            self.process_bar(ticker, ltf, i, zones,
                             allow_live=ltf.close_time(i) >= fresh_after)
        self.last_bar[ticker] = float(ltf.t[-1])
        return len(idx)

    def process_bar(self, ticker: str, bars: Bars, i: int, zones: List[Zone],
                    allow_live: bool = True):
        is_alt = self.is_alt(ticker)
        sigs, finished = self.book.process_bar(ticker, bars, i, zones, is_alt)
        for tr in finished:
            self._record_trade(tr, "paper")
        self.stats["paper_closed"] += len(finished)

        reversal = any(s.kind == EXIT_ONLY for s in sigs)
        lt = self.live.get(ticker)
        # busy = on_tick 이 이 거래로 주문을 내고 응답을 기다리는 중. 여기서
        # 만료 처리하면 체결된 포지션이 추적에서 빠져 손절 없는 고아가 된다.
        if lt is not None and not lt.done and not lt.busy:
            if lt.status == "pending" and bars.close_time(i) >= lt.expire_ts:
                lt.cancel(bars.close_time(i), "expired")
                self._finish_live(ticker, lt)
            elif lt.status == "open":
                why = lt.on_bar_close(bars, i, reversal)
                if why:
                    lt.pending_exit = why       # 다음 틱에 시장가로 판다

        for s in sigs:
            if s.kind != ENTRY:
                continue
            d = s.to_dict()
            self.stats["signals"] += 1
            if allow_live:
                accepted, why = self._maybe_go_live(ticker, d, bars.period)
            else:
                accepted, why = False, "재생 중인 과거 봉 (종이 매매만)"
            self._record_signal(ticker, d, is_alt, accepted, why)
            logger.info("가격행동 신호 %s %s 진입 %.8g 손절 %.8g 목표 %s | %s",
                        ticker, s.pattern, s.entry, s.stop,
                        [round(x, 8) for x in s.targets[:2]] or "추적", why)

    # ---------------- 라이브 ----------------
    def _maybe_go_live(self, ticker: str, sig: Dict[str, Any], period: int) -> Tuple[bool, str]:
        if not self.live_enabled:
            return False, "라이브 비활성 (종이 매매만)"
        if sig["pattern"] not in self.live_patterns:
            return False, "라이브 패턴 아님 (종이 매매만)"
        is_alt = self.is_alt(ticker)
        if is_alt and not self.cfg.NAKED_LIVE_ALTS:
            return False, "알트 라이브 비활성 (섀도)"
        if not is_alt and ticker not in self.live_tickers:
            return False, "라이브 대상 아님 (섀도)"
        cur = self.live.get(ticker)
        if cur is not None and not cur.done:
            return False, "같은 종목 라이브 거래 진행 중"
        if self.entry_gate:
            ok, why = self.entry_gate(ticker, sig)
            if not ok:
                return False, why
        tr = Trade(ticker, sig, self.cfg.NAKED_EXIT_MODE, period,
                   self.cfg.NAKED_ENTRY_VALID_BARS, self.cfg.NAKED_MAX_HOLD_BARS,
                   live=True, is_alt=self.is_alt(ticker))
        self.live[ticker] = tr
        if is_alt and self.watch:
            self.watch(ticker)          # 매수스톱 감시를 위해 시세 구독
        return True, f"라이브 대기 (매수스톱 {tr.entry:.8g}, {tr.mode})"

    def position_krw(self, sig: Dict[str, Any], px: Optional[float] = None) -> float:
        """
        고정 위험 사이징: 손절에 닿으면 NAKED_RISK_PER_TRADE_KRW 를 잃는 금액.
        px(실제 매수 시점 가격)로 잰다. 스캔 지연이나 갭으로 매수스톱보다 비싸게
        사면 손절까지 거리가 길어지므로, 신호의 진입가로 재면 손실이 한도를 넘는다.
        """
        entry = float(px) if px else float(sig["entry"])
        risk_pct = (entry - sig["stop"]) / entry if entry > 0 else 0
        if risk_pct <= 0:
            return 0.0
        krw = self.cfg.NAKED_RISK_PER_TRADE_KRW / (risk_pct + ROUND_TRIP_COST)
        krw = min(krw, self.cfg.MAX_POSITION_KRW)
        return max(krw, self.cfg.MIN_ORDER_KRW)

    async def on_tick(self, can_enter: bool = True):
        if not self.live or self._stopping:
            return
        if self._drive_tasks:
            return                  # 지난 틱의 주문 처리가 아직 진행 중
        # 클럭은 30초 넘게 걸린 iterator 를 취소한다. 주문이 접수된 뒤에 취소되면
        # 거래 상태(체결·청산 표시)가 갱신되지 않아 다음 틱에 같은 주문을 또
        # 낸다(이중 매수). 주문~상태 갱신은 별도 태스크로 돌려 취소에서 보호한다.
        # 종목끼리는 순차로 처리한다 - 동시에 사면 리스크 한도 검사(노출·잔고)를
        # 둘 다 통과해 한도를 넘을 수 있다.
        task = asyncio.get_running_loop().create_task(self._drive_all(can_enter))
        self._drive_tasks.add(task)
        task.add_done_callback(self._drive_tasks.discard)
        await asyncio.shield(task)

    async def drain(self, timeout: float = 20.0):
        """
        종료 전: 새 주문을 멈추고 진행 중인 주문 처리를 기다린다. 체결 표시 전에
        저장하거나 거래소 클라이언트를 닫으면 재시작 후 같은 주문을 다시 낸다.
        """
        self._stopping = True
        if self._drive_tasks:
            await asyncio.wait(list(self._drive_tasks), timeout=timeout)

    async def _drive_all(self, can_enter: bool):
        now = time.time()
        for ticker, tr in list(self.live.items()):
            if tr.done or tr.busy:
                if tr.done:
                    self.live.pop(ticker, None)
                continue
            px = self.price_of(ticker) if self.price_of else None
            if not px:
                continue
            tr.busy = True
            try:
                await self._drive(tr, float(px), now, can_enter)
            except Exception as e:
                logger.exception("가격행동 라이브 처리 실패 %s: %s", ticker, e)
            finally:
                tr.busy = False
            if tr.done:
                self._finish_live(ticker, tr)
                self._save()

    async def _drive(self, tr: Trade, px: float, now: float, can_enter: bool):
        pend = getattr(tr, "pending_exit", None)
        if tr.status == "open" and pend:
            if await self.sell(tr.ticker, tr.volume * (1.01 if tr.is_alt else 1.0)):
                tr.volume = 0.0
                tr.mark_exit(tr.remaining, px, now, pend)
                tr.pending_exit = None
            else:
                tr.exit_failures = getattr(tr, "exit_failures", 0) + 1
                if tr.exit_failures in (10, 600):
                    self._notify_later(f"<b>⚠️ 가격행동 청산 실패 반복</b> {tr.ticker} "
                                      f"({pend}, {tr.exit_failures}회) - 수동 확인 필요")
            return

        act = tr.on_price(px, now)
        if act is None:
            return
        kind, frac, why = act
        if kind == "cancel":
            tr.cancel(now, why)
            return
        if kind == "enter":
            if not self.live_enabled:
                # 검증 게이트가 닫혔다 (재시작 때 복원된 대기 거래 포함).
                # 보유 포지션 관리는 계속하지만 새로 사지는 않는다.
                tr.cancel(now, "live_disabled")
                return
            if not can_enter:
                return                      # 서킷브레이커 중: 대기 유지(만료되면 취소)
            krw = self.position_krw(tr.signal, px)
            if await self.buy(tr.ticker, krw):
                tr.krw = krw
                tr.volume = krw / px * (1 - ROUND_TRIP_COST / 2)
                tr.mark_filled(px, now)
                self.stats["live_entries"] += 1
                self._save()
                tg = ", ".join(f"{x:,.8g}" for x in tr.targets[:2]) or "없음"
                self._notify_later(
                    f"<b>📐 가격행동 진입</b> {tr.ticker} {tr.pattern}\n"
                    f"체결 {px:,.8g} · 손절 {tr.stop:,.8g} · {krw:,.0f}원\n"
                    f"청산 {tr.mode} · 존 {tg}")
            else:
                # 리스크 관문 거부 등. 계속 두면 매 틱 재시도하므로 취소한다.
                tr.cancel(now, "entry_rejected")
            return
        if kind == "exit":
            vol = tr.volume * (frac / tr.remaining) if tr.remaining > 0 else 0.0
            # 분할 청산분이 업비트 최소 주문금액 미만이거나 남는 쪽이 미만이면
            # 영원히 거부당하므로 전량으로 바꾼다.
            left = (tr.volume - vol) * px
            if (tr.remaining - frac <= 1e-9 or vol * px < self.cfg.MIN_ORDER_KRW * 1.02
                    or left < self.cfg.MIN_ORDER_KRW * 1.02):
                vol, frac = tr.volume, tr.remaining
                if tr.is_alt:
                    # 알트는 이 전략만 보유한다. 수수료 반올림 먼지까지 판다
                    # (엔진이 보유량으로 자른다).
                    vol *= 1.01
            if await self.sell(tr.ticker, vol):
                tr.volume = max(0.0, tr.volume - vol)
                tr.mark_exit(frac, px, now, why)
                tr.exit_failures = 0
                self._save()
            else:
                # 조회 실패·시세 정체면 다음 틱에 다시 시도. 오래 막히면 알린다.
                tr.exit_failures = getattr(tr, "exit_failures", 0) + 1
                if tr.exit_failures in (10, 600):
                    self._notify_later(f"<b>⚠️ 가격행동 청산 실패 반복</b> {tr.ticker} "
                                      f"({why}, {tr.exit_failures}회) - 수동 확인 필요")

    def _notify_later(self, msg: str):
        """알림은 기다리지 않는다. 텔레그램이 느려도 다른 종목의 손절 감시가 밀리면 안 된다."""
        if not self.notify:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.notify(msg))
        except RuntimeError:
            return
        self._notify_tasks.add(task)             # 참조가 없으면 GC 로 사라질 수 있다
        task.add_done_callback(self._notify_tasks.discard)

    def _finish_live(self, ticker: str, tr: Trade):
        self._record_trade(tr, "live")
        self.live.pop(ticker, None)
        if self.is_alt(ticker) and self.unwatch:
            self.unwatch(ticker)

    def restore_watches(self):
        """재시작 후 진행 중인 알트 라이브 거래의 시세 구독을 되살린다."""
        for t, tr in self.live.items():
            if not tr.done and self.is_alt(t) and self.watch:
                self.watch(t)

    # ---------------- 기록 ----------------
    @staticmethod
    def _today() -> str:
        return datetime.now(KST).strftime("%Y-%m-%d")

    def _append(self, d: str, rec: Dict[str, Any]):
        try:
            with open(os.path.join(d, self._today() + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=float) + "\n")
        except Exception as e:
            logger.error("가격행동 기록 실패: %s", e)

    def _record_signal(self, ticker, sig, is_alt, live, why):
        self._append(self.sig_dir, {"ts": time.time(), "ticker": ticker, "is_alt": is_alt,
                                    "live": live, "why": why, **sig})

    def _record_trade(self, tr: Trade, source: str):
        rec = tr.result()
        rec["source"] = source
        self._append(self.trade_dir, rec)

    def _save(self):
        state = {
            "saved_at": time.time(),
            "last_bar": self.last_bar,
            "paper": [tr.to_state() for tr in self.book.all_open()],
            "live": [tr.to_state() for tr in self.live.values() if not tr.done],
        }
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, default=float)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.error("가격행동 상태 저장 실패: %s", e)

    def save(self):
        self._save()

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            self.last_bar = {k: float(v) for k, v in st.get("last_bar", {}).items()}
            for d in st.get("paper", []):
                tr = Trade.from_state(d)
                self.book.open.setdefault(tr.ticker, []).append(tr)
            for d in st.get("live", []):
                tr = Trade.from_state(d)
                self.live[tr.ticker] = tr
            logger.info("가격행동 상태 복원: 종이 %d건 · 라이브 %d건",
                        len(self.book.all_open()), len(self.live))
        except Exception as e:
            logger.error("가격행동 상태 로딩 실패(새로 시작): %s", e)

    def summary(self) -> str:
        opens = self.book.all_open()
        held = sum(1 for tr in opens if tr.status == "open")
        live = ", ".join(f"{t}:{tr.status}" for t, tr in self.live.items()) or "없음"
        return (f"종목 {len(self.tickers())} · 종이 대기/보유 {len(opens) - held}/{held} · "
                f"라이브 {live} · 신호 {self.stats['signals']} · 종이 종료 {self.stats['paper_closed']}")
