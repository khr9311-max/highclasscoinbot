"""합성 봉 생성기 (전략 테스트용)."""

import numpy as np

from binance_coinm_v1.strategy.price_action import Bars


def bars_from(rows, period=3600, t0=1_700_000_000):
    arr = np.asarray(rows, dtype=float)
    t = t0 + np.arange(len(arr)) * period
    return Bars(t.astype(float), arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3],
                np.full(len(arr), 100.0), period)


def bullish_tk_rows():
    """40봉+ 상승 추세 -> 6봉 쉬어가기 -> 추세 캥거루(꼬리가 쉬어가기 저점 아래)."""
    rows = []
    c_prev = 100.0
    for k in range(60):
        c = 100.0 + 1.0 * k
        o = c_prev
        rows.append((o, max(o, c) + 0.3, min(o, c) - 0.3, c))
        c_prev = c
    for k in range(6):
        o, c = 159.9 + 0.05 * (k % 2), 160.0 - 0.05 * (k % 2)
        rows.append((o, 160.5, 159.5, c))
    rows.append((160.3, 160.5, 157.0, 160.4))          # 신호봉 (index 66)
    return rows


def mirror_rows(rows, k=400.0):
    """p' = k - p (고가/저가 교환)."""
    return [(k - o, k - l, k - h, k - c) for (o, h, l, c) in rows]


def random_walk(n=1500, seed=0, period=3600, start=30000.0):
    rng = np.random.default_rng(seed)
    rows = []
    c = start
    drift = 0.0
    for k in range(n):
        if k % 120 == 0:
            drift = rng.choice([-1, 0, 1]) * 0.0015
        o = c
        c = o * (1 + drift + rng.normal(0, 0.006))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.003)))
        l = min(o, c) * (1 - abs(rng.normal(0, 0.003)))
        rows.append((o, h, l, c))
    return bars_from(rows, period)


def aggregate(bars, factor):
    n = len(bars) // factor
    rows = []
    for j in range(n):
        s = slice(j * factor, (j + 1) * factor)
        rows.append((bars.o[s][0], bars.h[s].max(), bars.l[s].min(), bars.c[s][-1]))
    return bars_from(rows, bars.period * factor, t0=float(bars.t[0]))
