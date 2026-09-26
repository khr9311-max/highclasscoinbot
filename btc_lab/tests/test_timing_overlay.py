"""Timing rule mechanics for the swing execution overlay."""
import numpy as np
import pytest

from btc_lab import timing_overlay as to
from btc_lab.tests.test_regime_switch import stub_frame


def test_timed_index_waits_for_a_favourable_prediction_or_the_deadline():
    pred = np.array([-1.0, -1.0, 0.5, -1.0, np.nan])
    assert to.timed_index(pred, 0, 3, 1) == 2          # buy when the model expects a rise
    assert to.timed_index(pred, 0, 3, -1) == 0         # sell at once when it expects a fall
    assert to.timed_index(-pred, 0, 1, 1) == 0
    assert to.timed_index(np.full(5, np.nan), 1, 2, 1) == 3


def test_improvement_signs():
    close = np.array([100.0, 98.0, 103.0])
    assert to.improvement(close, 0, 1, 1) == pytest.approx(0.02)     # bought 2% cheaper
    assert to.improvement(close, 0, 2, -1) == pytest.approx(0.03)    # sold 3% higher


def test_four_hour_closes_fall_on_utc_boundaries():
    fr = stub_frame(days=2)
    idx = to.four_hour_closes(fr, 0, fr.n)
    assert len(idx) == 12
    assert all((fr.open_ms[i] + 300_000) % 14_400_000 == 0 for i in idx)


def test_timed_target_delays_a_flip_but_not_past_the_next_change():
    target = np.array([1, 1, -1, -1, -1, -1, 1, 1], dtype=float)
    pred = np.array([0, 0, 1, 1, 1, 1, -1, -1], dtype=float)   # sell unfavoured after bar 2
    out = to.timed_target(target, pred, 10)
    assert list(out[:6]) == [1, 1, 1, 1, 1, -1]   # sell waits, then forced before the next change
    assert list(out[6:]) == [-1, 1]               # buy waits too (model expects a fall), capped at the end
