"""
V1 신호: 1시간봉 마감 -> 4시간봉 존 -> Trendy Kangaroo -> LONG / SHORT.

롱은 원본 evaluate_bar(patterns=("trendy_kangaroo",)) 를 그대로 호출해 만든다 -
원본 업비트 규칙과 같은 신호가 나온다는 것을 코드로 보장한다.
숏은 원본 trendy_kangaroo 를 가격 반전 봉에 적용하고(price_action.mirror_bars),
진입·손절·목표를 정확히 대칭으로 만든다:

  롱: 진입 = 신호봉 고가 + 0.05 ATR (돌파 매수)   손절 = 신호봉 저가 - 0.05 ATR
      목표 = 진입가 위 존들의 하단 (가까운 순, 첫 목표가 손절폭 x min_rr 보다 가까우면 건너뜀)
  숏: 진입 = 신호봉 저가 - 0.05 ATR (돌파 매도)   손절 = 신호봉 고가 + 0.05 ATR
      목표 = 진입가 아래 존들의 상단 (가까운 순, 같은 min_rr 규칙)

필터(원본 기본값 그대로): 손절폭/진입가가 0.24% 미만이거나 8% 초과면 버린다.

반전 청산 신호(13장, 원본 EXIT_ONLY 의 대칭):
  보유 롱  청산: 저항 존의 약세 캥거루 꼬리 / 약세 빅섀도 / 물라
  보유 숏  청산: 지지 존의 강세 캥거루 꼬리 / 강세 빅섀도 / 와미
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .price_action import (ENTRY, Bars, Zone, _base_features, atr, big_shadow, evaluate_bar,
                           find_zones, kangaroo_tail, mirror_bars, targets_below,
                           trendy_kangaroo_short, wammie)

PATTERN = "trendy_kangaroo"
BUF_ATR = 0.05                 # 책의 '몇 핍' (원본 evaluate_bar 와 같은 값)
TARGET_MIN_DIST_ATR = 0.3      # 원본 _finish 와 같은 값
MIN_RISK_PCT = 0.0024          # 원본 evaluate_bar 기본값
MAX_RISK_PCT = 0.08


@dataclass
class TradeSignal:
    pattern: str
    direction: int                  # +1 롱 / -1 숏
    bar_index: int
    bar_time: float                 # 신호봉 시작 (UTC epoch 초)
    close_time: float               # 신호봉 마감 = 판정 시각
    entry: float                    # 돌파 트리거
    stop: float
    targets: List[float]
    atr: float
    features: Dict[str, float] = field(default_factory=dict)

    @property
    def risk_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry if self.entry > 0 else 0.0

    @property
    def side(self) -> str:
        return "LONG" if self.direction > 0 else "SHORT"

    def to_dict(self) -> Dict[str, Any]:
        return {"pattern": self.pattern, "direction": self.direction, "bar_index": self.bar_index,
                "bar_time": self.bar_time, "close_time": self.close_time, "entry": self.entry,
                "stop": self.stop, "targets": list(self.targets), "atr": self.atr,
                "features": dict(self.features), "risk_pct": self.risk_pct}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TradeSignal":
        return cls(d["pattern"], int(d["direction"]), int(d.get("bar_index", -1)),
                   float(d["bar_time"]), float(d["close_time"]), float(d["entry"]),
                   float(d["stop"]), [float(x) for x in d.get("targets") or []],
                   float(d.get("atr", 0.0)), dict(d.get("features") or {}))


@dataclass
class BarEvaluation:
    entries: List[TradeSignal]
    exit_long: bool
    exit_short: bool
    reversal_patterns: List[str]

    def entry_for(self, direction: int) -> Optional[TradeSignal]:
        return next((s for s in self.entries if s.direction == direction), None)


def zones_at(htf: Bars, ts: float, zone_bars: int) -> List[Zone]:
    """ts 시각까지 '마감된' 상위 시간봉만으로 존을 그린다 (미래 참조 없음)."""
    h = htf.closed_by(ts).tail(zone_bars)
    return find_zones(h)


def _short_signal(bars: Bars, mirrored: Bars, i: int, zones: List[Zone], min_rr: float,
                  min_risk_pct: float, max_risk_pct: float) -> Optional[TradeSignal]:
    tk = trendy_kangaroo_short(mirrored, i)
    if tk is None:
        return None
    a = atr(bars, i)
    if a <= 0:
        return None
    buf = BUF_ATR * a
    entry = float(bars.l[i]) - buf
    stop = float(bars.h[i]) + buf
    risk = stop - entry
    tg = targets_below(zones, entry, TARGET_MIN_DIST_ATR * a)
    while tg and risk > 0 and (entry - tg[0]) < min_rr * risk:
        tg = tg[1:]
    feats = {**_base_features(mirrored, i, a), "pause": float(tk["pause"])}
    feats["rr"] = ((entry - tg[0]) / risk) if (tg and risk > 0) else 0.0
    sig = TradeSignal(PATTERN, -1, i, float(bars.t[i]), bars.close_time(i), entry, stop,
                      tg[:3], a, feats)
    feats["risk_pct"] = sig.risk_pct
    if not (min_risk_pct <= sig.risk_pct <= max_risk_pct):
        return None
    return sig


def _long_signal(bars: Bars, i: int, zones: List[Zone], min_rr: float,
                 min_risk_pct: float, max_risk_pct: float) -> Optional[TradeSignal]:
    sigs = evaluate_bar(bars, i, zones, patterns=(PATTERN,), min_rr=min_rr,
                        min_risk_pct=min_risk_pct, max_risk_pct=max_risk_pct)
    s = next((x for x in sigs if x.kind == ENTRY and x.pattern == PATTERN), None)
    if s is None:
        return None
    return TradeSignal(PATTERN, +1, i, s.bar_time, s.close_time, float(s.entry), float(s.stop),
                       [float(x) for x in s.targets], atr(bars, i), dict(s.features))


def reversal_flags(bars: Bars, i: int, zones: List[Zone]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {"exit_long": [], "exit_short": []}
    for name, fn in (("kangaroo", kangaroo_tail), ("big_shadow", big_shadow), ("wammie", wammie)):
        if fn(bars, i, zones, bullish=False):
            out["exit_long"].append("bear_" + name)
        if fn(bars, i, zones, bullish=True):
            out["exit_short"].append("bull_" + name)
    return out


class SignalEngine:
    def __init__(self, directions: Sequence[int] = (1, -1), min_rr: float = 1.0,
                 min_risk_pct: float = MIN_RISK_PCT, max_risk_pct: float = MAX_RISK_PCT):
        self.directions = tuple(directions)
        self.min_rr = min_rr
        self.min_risk_pct = min_risk_pct
        self.max_risk_pct = max_risk_pct

    def evaluate(self, bars: Bars, i: int, zones: List[Zone], mirrored: Optional[Bars] = None,
                 with_exits: bool = True) -> BarEvaluation:
        """i 번째 '마감된' 봉 판정. bars[i+1:] 는 보지 않는다."""
        entries: List[TradeSignal] = []
        if 1 in self.directions:
            s = _long_signal(bars, i, zones, self.min_rr, self.min_risk_pct, self.max_risk_pct)
            if s:
                entries.append(s)
        if -1 in self.directions:
            m = mirrored if mirrored is not None else mirror_bars(bars)
            s = _short_signal(bars, m, i, zones, self.min_rr, self.min_risk_pct, self.max_risk_pct)
            if s:
                entries.append(s)
        flags = reversal_flags(bars, i, zones) if with_exits else {"exit_long": [], "exit_short": []}
        return BarEvaluation(entries, bool(flags["exit_long"]), bool(flags["exit_short"]),
                             flags["exit_long"] + flags["exit_short"])
