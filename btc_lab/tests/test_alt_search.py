"""Alt search: timing (no look-ahead), costs and exits of the BTC-denominated simulator."""
import math

import numpy as np
import pytest

from btc_lab import alt_search as s


def panel(close, cost=0.0, low=None):
    """A tiny panel with opens equal to the previous close."""
    close = np.asarray(close, dtype=np.float32)
    p = object.__new__(s.Panel)
    p.bases = [f"A{i}" for i in range(close.shape[0])]
    p.c = close
    p.o = np.concatenate([close[:, :1], close[:, :-1]], axis=1)
    p.h = np.maximum(p.o, p.c)
    p.l = np.minimum(p.o, p.c) if low is None else np.asarray(low, dtype=np.float32)
    p.cost = np.full(close.shape, cost, dtype=np.float32)
    p.listed = np.isfinite(close)
    p.open_ms = s.HOUR * np.arange(close.shape[1], dtype=np.int64)
    return p


def test_forward_return_starts_at_the_next_open():
    p = panel([[1, 2, 4, 8, 16, 32]])
    y = s.forward(p, 2)
    # signal at the close of t=0 fills at open[1] (=close[0]=1) and exits at open[3] (=close[2]=4)
    assert y[0, 0] == pytest.approx(math.log(1.5))                 # log 4 clipped to +50%
    p = panel([[1.0, 1.1, 1.2, 1.3, 1.4, 1.5]])
    assert s.forward(p, 2)[0, 0] == pytest.approx(math.log(1.2 / 1.0))
    assert np.isnan(s.forward(p, 2)[0, -3:]).all()


def test_slot_entry_uses_next_open_and_pays_costs_both_ways():
    p = panel([[1.0, 1.0, 1.1, 1.21, 1.21, 1.21]], cost=0.01)
    curve, trades = s.slots(p, 1, 6, lambda t: [0] if t == 1 else [], hold=2, chase=0.0)
    # signal at close of t=1 -> buy at open[2]=1.0 with a third of equity, sell at open[4]=1.21
    assert trades[0]["t_in"] == 2 and trades[0]["t_out"] == 4
    assert trades[0]["ret"] == pytest.approx(1.21 * 0.99 * 0.99 - 1, rel=1e-5)
    assert curve[-1] == pytest.approx(2 / 3 + (1 / 3) * 1.21 * 0.99 * 0.99, rel=1e-5)


def test_stop_fills_at_the_worse_of_stop_and_gap_open():
    close = [[1.0, 1.0, 1.0, 0.5, 0.5]]
    low = [[1.0, 1.0, 1.0, 0.5, 0.5]]
    p = panel(close, low=low)
    _, trades = s.slots(p, 1, 5, lambda t: [0] if t == 1 else [], hold=10, stop=0.08, chase=0.0)
    assert trades[0]["reason"] == "stop"
    # the gap bar opens at 1.0 (previous close) so the stop at 0.92 fills, minus slippage
    assert trades[0]["ret"] == pytest.approx(0.92 * (1 - s.STOP_SLIP) - 1, rel=1e-5)


def test_delisted_holding_exits_at_last_close_minus_haircut():
    p = panel([[1.0, 1.0, 1.2, np.nan, np.nan]])
    _, trades = s.slots(p, 1, 5, lambda t: [0] if t == 1 else [], hold=10, chase=0.0)
    assert trades[0]["reason"] == "delisted"
    assert trades[0]["ret"] == pytest.approx(1.2 * (1 - s.DELIST_HAIRCUT) - 1, rel=1e-5)


def test_rotation_trades_at_the_day_open_on_the_previous_day_close():
    hours = 3 * s.DAY
    a = np.r_[np.ones(s.DAY), np.linspace(1, 2, 2 * s.DAY)]
    p = panel([a, np.ones(hours)])
    seen = []

    def pick(t):
        seen.append(t)
        return [0]
    curve, trades = s.rotation(p, s.DAY, hours, pick)
    assert seen == [s.DAY - 1, 2 * s.DAY - 1]                       # decided on the 23:00 bar's close
    assert curve[-1] == pytest.approx(a[-1] / a[s.DAY - 1], rel=1e-5)


def test_top_skips_non_positive_and_masked_scores():
    score = np.array([0.3, -0.1, 0.5, np.nan, 0.9])
    mask = np.array([True, True, True, True, False])
    assert s.top(score, mask, 3) == [2, 0]
    assert s.top(score, mask, 3, positive=False) == [2, 0, 1]
