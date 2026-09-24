"""Fixed exploratory candidates; never imported by the live strategy.

Signals use completed source bars only. A signal's trigger is its closing price;
the existing simulator waits for that price to be crossed during the next 1h
bar (with gap handling and adverse slippage). This is not next-open execution.
All candidates use a 2 x simple ATR(14) initial stop and one full-position target.
The six definitions below are fixed before reading their research results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from .data import Dataset
from ..strategy.price_action import Bars, atr
from ..strategy.signals import TradeSignal


@dataclass(frozen=True)
class Candidate:
    name: str
    family: str
    period_hours: int
    target_r: float
    warmup_bars: int = 200
    atr_bars: int = 14
    atr_stop_multiple: float = 2.0
    variant: str = "zone"
    valid_bars: int = 1

    @property
    def max_hold_bars(self) -> int:
        """Simulator counts 1h execution bars, including the entry bar."""
        return 72 * self.period_hours


CANDIDATES: Tuple[Candidate, ...] = tuple(
    Candidate(f"{family}_{hours}h", family, hours, target_r)
    for family, target_r in (
        ("donchian", 3.0), ("ema_cross", 3.0), ("bollinger_reentry", 1.5)
    )
    for hours in (1, 4)
)


class ResearchPrecomputed:
    """Duck-typed Simulator input, with source signals placed exactly once."""

    def __init__(self, ltf: Bars):
        self.ltf = ltf
        self.n = len(ltf)
        self.long: List[Optional[TradeSignal]] = [None] * self.n
        self.short: List[Optional[TradeSignal]] = [None] * self.n
        self.exit_long = np.zeros(self.n, dtype=bool)
        self.exit_short = np.zeros(self.n, dtype=bool)
        self.valid = np.zeros(self.n, dtype=bool)

    def entry(self, i: int, d: int) -> Optional[TradeSignal]:
        return self.long[i] if d > 0 else self.short[i]

    def signal_count(self) -> Dict[str, int]:
        return {"long": sum(s is not None for s in self.long),
                "short": sum(s is not None for s in self.short)}


def _ema(closes: np.ndarray, span: int) -> np.ndarray:
    """Causal first-close seeded EMA; no future-dependent initialization."""
    result = np.empty(len(closes), dtype=float)
    if len(closes):
        result[0] = closes[0]
        alpha = 2.0 / (span + 1)
        for j in range(1, len(closes)):
            result[j] = alpha * closes[j] + (1 - alpha) * result[j - 1]
    return result


def _bollinger(closes: np.ndarray, j: int) -> Tuple[float, float, float]:
    window = closes[j - 19:j + 1]
    mean = float(window.mean())
    width = 2.0 * float(window.std(ddof=0))
    return mean - width, mean, mean + width


def build_precomputed(ds: Dataset, candidate: Candidate) -> ResearchPrecomputed:
    """Build signals without future source bars or repeating 4h signals on 1h.

    Donchian: close breaks the preceding 20-bar high/low; exit beyond the
    opposite preceding 10-bar channel. EMA: 20/80 crossing and opposite-cross
    exit. Bollinger: previous close outside its 20-bar, 2-population-SD band,
    then current close inside its current band; exit at/beyond current mean.
    Two hundred earlier source bars are required before any signal.
    """
    if candidate not in CANDIDATES:
        raise ValueError("Only the six prespecified research candidates are supported")
    if ds.ltf.period != 3600:
        raise ValueError("Research execution bars must be 1h")
    source = ds.ltf if candidate.period_hours == 1 else ds.htf
    if source.period != candidate.period_hours * 3600:
        raise ValueError("Candidate source timeframe does not match dataset")
    pre = ResearchPrecomputed(ds.ltf)
    if not len(source) or not pre.n:
        return pre
    source_close_times = source.t + source.period
    base_close_times = ds.ltf.t + ds.ltf.period
    # A signal at source index 200 has 200 prior completed source bars.
    pre.valid[:] = (np.searchsorted(source_close_times, base_close_times,
                                   side="right") > candidate.warmup_bars)
    fast = _ema(source.c, 20) if candidate.family == "ema_cross" else None
    slow = _ema(source.c, 80) if candidate.family == "ema_cross" else None
    for j in range(candidate.warmup_bars, len(source)):
        closed_at = float(source_close_times[j])
        i = int(np.searchsorted(base_close_times, closed_at))
        if i >= pre.n:
            break
        # Do not backdate or forward-fill a signal across a missing base bar.
        if float(base_close_times[i]) != closed_at:
            continue
        close = float(source.c[j])
        long_entry = short_entry = False
        if candidate.family == "donchian":
            long_entry = close > float(source.h[j - 20:j].max())
            short_entry = close < float(source.l[j - 20:j].min())
            pre.exit_long[i] = close < float(source.l[j - 10:j].min())
            pre.exit_short[i] = close > float(source.h[j - 10:j].max())
        elif candidate.family == "ema_cross":
            long_entry = fast[j] > slow[j] and fast[j - 1] <= slow[j - 1]
            short_entry = fast[j] < slow[j] and fast[j - 1] >= slow[j - 1]
            pre.exit_long[i], pre.exit_short[i] = short_entry, long_entry
        else:
            lower, mean, upper = _bollinger(source.c, j)
            old_lower, _, old_upper = _bollinger(source.c, j - 1)
            inside = lower <= close <= upper
            long_entry = inside and float(source.c[j - 1]) < old_lower
            short_entry = inside and float(source.c[j - 1]) > old_upper
            pre.exit_long[i], pre.exit_short[i] = close >= mean, close <= mean
        volatility = atr(source, j, candidate.atr_bars)
        distance = candidate.atr_stop_multiple * volatility
        if not np.isfinite([close, volatility, distance]).all() or distance <= 0:
            continue
        for direction, enter in ((1, long_entry), (-1, short_entry)):
            if not enter:
                continue
            stop = close - direction * distance
            target = close + direction * candidate.target_r * distance
            if min(close, stop, target) <= 0 or not np.isfinite([stop, target]).all():
                continue
            signal = TradeSignal(
                candidate.name, direction, i, float(source.t[j]), closed_at,
                close, stop, [target], volatility,
                {"source_period_hours": float(candidate.period_hours),
                 "source_bar_index": float(j), "target_r": candidate.target_r},
            )
            (pre.long if direction > 0 else pre.short)[i] = signal
    return pre
