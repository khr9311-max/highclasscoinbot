"""
COIN-M 봉 단위 시뮬레이터 - 실시간 엔진과 같은 규칙·같은 코드.

  신호      : strategy.signals.SignalEngine (롱=원본 evaluate_bar, 숏=반전 대칭)
  청산 규칙 : strategy.ladder.PositionLogic (사다리 손절·3봉 추적·반전·반대 신호·기한·종가 손절)
  부분청산  : strategy.ladder.tp_schedule / tp_quantities (누적 내림, 정수 계약)
  사이징    : risk.sizing.size_position (BTC 위험 예산, 내림, 최소 수량 미달이면 건너뜀)
  손익      : risk.inverse_math (인버스 BTC 손익·수수료·펀딩)

봉 내부 가정 (보수적, 원본 업비트 PaperBook 과 같은 방향):
  - 돌파 대기 봉에서 트리거와 손절이 둘 다 닿으면 '진입 후 손절'
  - 트리거 전에 손절선부터 닿으면 신호 무효 (진입 판정이 먼저, 원본과 같은 순서)
  - 보유 봉에서 손절과 목표가 둘 다 닿으면 손절
  - 진입한 봉에서는 목표 도달을 인정하지 않는다
  - 갭(시가가 이미 손절/목표 너머)이면 시가에 체결
  - 체결가에는 슬리피지를 불리하게 더한다 (진입·청산 slippage_bps, 손절 stop_slippage_bps)
  - 봉 마감 청산(반전·반대 신호·기한·종가 손절)은 다음 봉 시가에 체결 (실시간은 마감
    직후 시장가로 내므로 가장 가까운 관측 가격)
  - MARK_PRICE 손절은 마크가격 봉의 고가/저가로 판정하고, 체결은 체결가(시가 갭) 기준
  - 펀딩은 펀딩 시각(00/08/16 UTC = 봉 시작)에 보유 중이면 마크가 x 비율로 정산
  - 포지션은 한 번에 하나 (MAX_POSITIONS=1), 청산한 봉에서 반대 방향으로 즉시 진입하지 않음
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..exchange.contract import ContractSpec
from ..exchange.market_data import FundingRecord
from ..risk import inverse_math as im
from ..risk.sizing import SizingInput, size_position
from ..strategy.ladder import PositionLogic, tp_quantities, tp_schedule
from ..strategy.price_action import Bars, find_zones, mirror_bars
from ..strategy.signals import SignalEngine, TradeSignal


@dataclass
class SimConfig:
    variant: str = "ladder"
    directions: Tuple[int, ...] = (1, -1)
    fractions: Tuple[float, float, float] = (0.25, 0.25, 0.25)
    start_equity_btc: float = 1.0
    risk_fraction: float = 0.005
    leverage: int = 3
    taker_fee: float = 0.0005
    slippage_bps: float = 3.0
    stop_slippage_bps: float = 5.0
    stop_trigger: str = "MARK_PRICE"
    tp_trigger: str = "CONTRACT_PRICE"
    valid_bars: int = 2
    max_hold_bars: int = 72
    max_exposure_multiple: float = 3.0
    liq_guard_min_ratio: float = 2.0            # 사이징은 항상 현재 equity 기준 (복리)

    @property
    def name(self) -> str:
        d = {(1, -1): "both", (1,): "long", (-1,): "short"}.get(tuple(self.directions), "?")
        return f"{self.variant}|{d}"


class Precomputed:
    """모든 변형이 공유하는 봉별 판정 (신호·반전 청산 플래그). 한 번만 계산한다."""

    def __init__(self, ltf: Bars, htf: Bars, zone_bars: int = 200, min_rr: float = 1.0,
                 directions: Sequence[int] = (1, -1), progress=None):
        self.ltf = ltf
        n = len(ltf)
        self.n = n
        self.long: List[Optional[TradeSignal]] = [None] * n
        self.short: List[Optional[TradeSignal]] = [None] * n
        self.exit_long = np.zeros(n, dtype=bool)
        self.exit_short = np.zeros(n, dtype=bool)
        self.valid = np.zeros(n, dtype=bool)
        eng = SignalEngine(directions, min_rr)
        mirrored = mirror_bars(ltf)
        zkey, zones = None, []
        for i in range(60, n):
            ct = ltf.close_time(i)
            h = htf.closed_by(ct)
            if len(h) < 60:                       # 원본 백테스트와 같은 준비 구간
                continue
            h = h.tail(zone_bars)
            key = (len(h), float(h.t[-1]))
            if key != zkey:
                zones, zkey = find_zones(h), key
            ev = eng.evaluate(ltf, i, zones, mirrored)
            self.valid[i] = True
            self.long[i] = ev.entry_for(1)
            self.short[i] = ev.entry_for(-1)
            self.exit_long[i] = ev.exit_long
            self.exit_short[i] = ev.exit_short
            if progress and i % 5000 == 0:
                progress(i, n)

    def entry(self, i: int, d: int) -> Optional[TradeSignal]:
        return self.long[i] if d > 0 else self.short[i]

    def signal_count(self) -> Dict[str, int]:
        return {"long": sum(1 for s in self.long if s), "short": sum(1 for s in self.short if s)}


@dataclass
class SimTrade:
    direction: int
    signal: TradeSignal
    trigger: float
    stop: float
    targets: List[float]
    expire_ts: float
    status: str = "pending"                  # pending / open / closed / cancelled / skipped
    fill_bar: int = -1
    fill_ts: float = 0.0
    fill_px: float = 0.0
    qty0: int = 0
    qty: int = 0
    tp_plan: List[Tuple[int, int]] = field(default_factory=list)
    logic: Optional[PositionLogic] = None
    realized_btc: float = 0.0
    realized_usd: float = 0.0
    fee_btc: float = 0.0
    fee_usd: float = 0.0
    funding_btc: float = 0.0
    funding_usd: float = 0.0
    planned_loss_btc: float = 0.0
    equity_at_entry: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = ""
    exits: List[Tuple[str, int, float]] = field(default_factory=list)
    skip_reason: str = ""

    @property
    def net_btc(self) -> float:
        return self.realized_btc - self.fee_btc + self.funding_btc

    @property
    def net_usd(self) -> float:
        return self.realized_usd - self.fee_usd + self.funding_usd

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction, "side": "LONG" if self.direction > 0 else "SHORT",
            "signal_ts": self.signal.close_time, "fill_ts": self.fill_ts, "exit_ts": self.exit_ts,
            "trigger": self.trigger, "fill_px": self.fill_px, "init_stop": self.stop,
            "targets": self.targets, "qty": self.qty0, "status": self.status,
            "exit_reason": self.exit_reason, "exits": self.exits,
            "realized_btc": self.realized_btc, "fee_btc": self.fee_btc,
            "funding_btc": self.funding_btc, "net_btc": self.net_btc,
            "realized_usd": self.realized_usd, "net_usd": self.net_usd,
            "equity_at_entry": self.equity_at_entry,
            "ret": self.net_btc / self.equity_at_entry if self.equity_at_entry > 0 else 0.0,
            "r": self.net_btc / self.planned_loss_btc if self.planned_loss_btc > 0 else None,
            "bars_held": self.logic.bars_held if self.logic else 0,
            "tp_filled": list(self.logic.tp_filled) if self.logic else [],
            "ladder_step": self.logic.ladder_step if self.logic else 0,
            "skip_reason": self.skip_reason,
        }


@dataclass
class SimResult:
    config: SimConfig
    trades: List[Dict[str, Any]]
    skipped: List[Dict[str, Any]]
    equity_curve: np.ndarray               # 봉 마감별 equity (BTC, 미실현 포함)
    equity_ts: np.ndarray
    final_equity: float
    funding_total: float


class Simulator:
    def __init__(self, ltf: Bars, mark: Optional[Bars], funding: Sequence[FundingRecord],
                 spec: ContractSpec, pre: Precomputed):
        self.ltf = ltf
        self.mark = mark if mark is not None else ltf
        self.spec = spec
        self.pre = pre
        self.cs = float(spec.contract_size)
        self.funding_by_bar: Dict[int, List[FundingRecord]] = {}
        for fr in funding:
            # 펀딩 시각을 포함하는 봉 (봉 시작 <= 펀딩 시각 < 봉 끝)
            ft = fr.funding_time_ms / 1000.0
            k = int(np.searchsorted(ltf.t, ft, side="right")) - 1
            if 0 <= k < len(ltf) and ft < float(ltf.t[k]) + ltf.period:
                self.funding_by_bar.setdefault(k, []).append(fr)

    # ------------------------------------------------------------------ 체결 보조
    def _px(self, raw: float, side: str, bps: float) -> float:
        return float(self.spec.round_price(im.price_with_slippage(raw, side, bps)))

    def _close_part(self, tr: SimTrade, qty: int, px: float, ts: float, reason: str,
                    cfg: SimConfig) -> float:
        """부분/전량 청산. 실현손익(BTC)을 돌려준다 (수수료 별도)."""
        qty = min(qty, tr.qty)
        if qty <= 0:
            return 0.0
        pnl = im.pnl_btc(tr.direction, qty, self.cs, tr.fill_px, px)
        fee = im.fee_btc(qty, self.cs, px, cfg.taker_fee)
        tr.realized_btc += pnl
        tr.realized_usd += pnl * px
        tr.fee_btc += fee
        tr.fee_usd += fee * px
        tr.qty -= qty
        tr.exits.append((reason, qty, px))
        if tr.qty == 0:
            tr.status = "closed"
            tr.exit_ts = ts
            tr.exit_reason = reason
        return pnl - fee

    # ------------------------------------------------------------------
    def run(self, cfg: SimConfig) -> SimResult:
        L, M, pre = self.ltf, self.mark, self.pre
        n = len(L)
        equity = cfg.start_equity_btc
        trades: List[SimTrade] = []
        skipped: List[Dict[str, Any]] = []
        curve = np.full(n, np.nan)
        tr: Optional[SimTrade] = None
        funding_total = 0.0
        stop_series = M if cfg.stop_trigger == "MARK_PRICE" else L
        tp_series = M if cfg.tp_trigger == "MARK_PRICE" else L
        dirs = set(cfg.directions)
        first = int(np.argmax(pre.valid)) if pre.valid.any() else n
        for i in range(first, n):
            t = float(L.t[i])
            o, h, l, c = float(L.o[i]), float(L.h[i]), float(L.l[i]), float(L.c[i])
            exited_at_close = False     # 봉 마감 청산/취소 -> 이번 봉 신호로 새 진입 금지
            # ---- (a) 펀딩: 봉 시작 시각에 보유 중이면 ----
            if tr is not None and tr.status == "open" and tr.fill_bar < i:
                for fr in self.funding_by_bar.get(i, ()):
                    mk = fr.mark_price or float(M.o[i]) or o
                    f = im.funding_fee_btc(tr.direction, tr.qty, self.cs, mk, fr.funding_rate)
                    tr.funding_btc += f
                    tr.funding_usd += f * mk
                    equity += f
                    funding_total += f
            # ---- (b) 돌파 대기 ----
            if tr is not None and tr.status == "pending":
                d = tr.direction
                if t >= tr.expire_ts:
                    tr.status, tr.exit_reason = "cancelled", "expired"
                    tr = None
                elif (d > 0 and h >= tr.trigger) or (d < 0 and l <= tr.trigger):
                    raw = max(tr.trigger, o) if d > 0 else min(tr.trigger, o)
                    side = "BUY" if d > 0 else "SELL"
                    sz = size_position(SizingInput(
                        equity_btc=equity, available_btc=equity, risk_fraction=cfg.risk_fraction,
                        direction=d, entry_price=raw, stop_price=tr.stop, leverage=cfg.leverage,
                        taker_fee=cfg.taker_fee, entry_slippage_bps=cfg.slippage_bps,
                        stop_slippage_bps=cfg.stop_slippage_bps,
                        max_exposure_multiple=cfg.max_exposure_multiple,
                        liq_guard_min_ratio=cfg.liq_guard_min_ratio), self.spec)
                    if not sz.ok or d * (raw - tr.stop) <= 0:
                        tr.status = "skipped"
                        tr.skip_reason = sz.reason if not sz.ok else "gap_beyond_stop"
                        skipped.append(tr.to_dict())
                        tr = None
                    else:
                        fill = self._px(raw, side, cfg.slippage_bps)
                        tr.status = "open"
                        tr.fill_bar, tr.fill_ts, tr.fill_px = i, t, fill
                        tr.qty0 = tr.qty = int(sz.qty)
                        tr.planned_loss_btc = sz.planned_loss_btc
                        tr.equity_at_entry = equity
                        fee = im.fee_btc(tr.qty, self.cs, fill, cfg.taker_fee)
                        tr.fee_btc += fee
                        tr.fee_usd += fee * fill
                        equity -= fee
                        sch = tp_schedule(cfg.variant, len(tr.targets), cfg.fractions)
                        tr.tp_plan = [(lv, int(q)) for lv, q in tp_quantities(tr.qty0, sch, 1)]
                        tr.logic = PositionLogic(d, fill, tr.stop, list(tr.targets), cfg.variant,
                                                 cfg.max_hold_bars)
                        trades.append(tr)
                        # 같은 봉 손절 (보수적: 진입 후 손절로 본다)
                        sl, so = float(stop_series.l[i]), float(stop_series.h[i])
                        if (d > 0 and sl <= tr.stop) or (d < 0 and so >= tr.stop):
                            px = self._px(tr.stop, "SELL" if d > 0 else "BUY", cfg.stop_slippage_bps)
                            equity += self._close_part(tr, tr.qty, px, t + L.period * 0.5, "stop", cfg)
                            tr = None
                elif (d > 0 and l <= tr.stop) or (d < 0 and h >= tr.stop):
                    tr.status, tr.exit_reason = "cancelled", "stop_before_entry"
                    tr = None
            # ---- (c) 보유 봉 내부: 손절 우선, 그다음 목표 ----
            elif tr is not None and tr.status == "open":
                d = tr.direction
                lg = tr.logic
                stop = lg.stop
                s_o, s_h, s_l = float(stop_series.o[i]), float(stop_series.h[i]), float(stop_series.l[i])
                hit_stop = (d > 0 and s_l <= stop) or (d < 0 and s_h >= stop)
                if hit_stop:
                    gapped = (d > 0 and s_o <= stop) or (d < 0 and s_o >= stop)
                    raw = o if gapped else stop
                    raw = min(raw, stop) if d > 0 else max(raw, stop)
                    px = self._px(raw, "SELL" if d > 0 else "BUY", cfg.stop_slippage_bps)
                    reason = ("trailing_stop" if lg.trailing_active else
                              "ladder_stop" if (lg.ladder_step or lg.tp_filled) else "stop")
                    equity += self._close_part(tr, tr.qty, px, t + L.period * 0.5, reason, cfg)
                    tr = None
                else:
                    tp_h, tp_l, tp_o = float(tp_series.h[i]), float(tp_series.l[i]), float(tp_series.o[i])
                    for lv, q in list(tr.tp_plan):
                        if lv in lg.tp_filled or lv >= len(tr.targets):
                            continue
                        tgt = tr.targets[lv]
                        if (d > 0 and tp_h >= tgt) or (d < 0 and tp_l <= tgt):
                            raw = max(tgt, o) if d > 0 else min(tgt, o)
                            px = self._px(raw, "SELL" if d > 0 else "BUY", cfg.slippage_bps)
                            equity += self._close_part(tr, q, px, t + L.period * 0.5, f"tp{lv + 1}", cfg)
                            lg.on_tp_filled(lv)
                            if tr.qty == 0:
                                tr = None
                                break
                        else:
                            break                    # 목표는 가까운 것부터 차례로
            # ---- (d) 봉 마감: 원본 청산 규칙 ----
            opp_sig = None
            if tr is not None and tr.status == "open" and i >= tr.fill_bar:
                d = tr.direction
                reversal = bool(pre.exit_long[i]) if d > 0 else bool(pre.exit_short[i])
                opp_sig = pre.entry(i, -d) if (-d in dirs) else None
                dec = tr.logic.on_bar_close(L, i, reversal, opp_sig is not None)
                if dec.exit_reason:
                    nxt = float(L.o[i + 1]) if i + 1 < n else c
                    px = self._px(nxt, "SELL" if d > 0 else "BUY", cfg.slippage_bps)
                    equity += self._close_part(tr, tr.qty, px, t + L.period, dec.exit_reason, cfg)
                    tr = None
                    exited_at_close = True
            # ---- (e) 신호 ----
            consumed = set()
            if tr is not None and tr.status == "pending":
                opp = pre.entry(i, -tr.direction)
                if opp is not None and (-tr.direction in dirs):
                    tr.status, tr.exit_reason = "cancelled", "opposite_signal"
                    consumed.add(-tr.direction)
                    tr = None
                    exited_at_close = True
            if opp_sig is not None:
                consumed.add(opp_sig.direction)
            if tr is None and not exited_at_close and pre.valid[i]:
                for d in (1, -1):
                    if d not in dirs or d in consumed:
                        continue
                    sig = pre.entry(i, d)
                    if sig is None:
                        continue
                    trig = float(self.spec.round_price_away(sig.entry, d, is_stop=False))
                    stp = float(self.spec.round_price_away(sig.stop, d, is_stop=True))
                    tg = [float(self.spec.round_target(x, d)) for x in sig.targets]
                    tr = SimTrade(d, sig, trig, stp, tg, sig.close_time + cfg.valid_bars * L.period)
                    break
            # ---- 봉 마감 equity (미실현 포함, 마크가) ----
            unreal = 0.0
            if tr is not None and tr.status == "open":
                unreal = im.pnl_btc(tr.direction, tr.qty, self.cs, tr.fill_px, float(M.c[i]))
            curve[i] = equity + unreal
        if tr is not None and tr.status == "open":
            px = float(L.c[n - 1])
            equity += self._close_part(tr, tr.qty, px, float(L.t[n - 1]) + L.period, "end_of_data", cfg)
        mask = ~np.isnan(curve)
        return SimResult(cfg, [x.to_dict() for x in trades if x.status in ("closed",)],
                         skipped, curve[mask], L.t[mask], equity, funding_total)
