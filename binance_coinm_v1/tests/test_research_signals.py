"""Prespecified exploratory signals: causality, timing, and rule semantics."""

from dataclasses import replace

import numpy as np
import pytest

from binance_coinm_v1.backtest.data import Dataset
from binance_coinm_v1.backtest.research_signals import CANDIDATES, build_precomputed
from binance_coinm_v1.backtest.simulator import SimConfig, Simulator

from .helpers import load_spec
from .synth import aggregate, bars_from, random_walk


def dataset(bars):
    return Dataset("BTCUSD_PERP", bars, aggregate(bars, 4), bars, 0, [], load_spec())


def candidate(name):
    return next(c for c in CANDIDATES if c.name == name)


def signal_dict(signal):
    return signal.to_dict() if signal is not None else None


@pytest.mark.parametrize("descriptor", CANDIDATES, ids=lambda c: c.name)
def test_adding_future_bars_does_not_change_past_signals(descriptor):
    ds = dataset(random_walk(2200, seed=871))
    cutoff = 1705  # Ends inside a 4h bar, so the incomplete 4h bar is unavailable.
    prefix = replace(ds, ltf=ds.ltf.upto(cutoff), mark=ds.mark.upto(cutoff),
                     htf=ds.htf.closed_by(ds.ltf.close_time(cutoff - 1)))
    whole, past = build_precomputed(ds, descriptor), build_precomputed(prefix, descriptor)
    assert sum(past.signal_count().values()) > 0
    assert [signal_dict(x) for x in whole.long[:cutoff]] == [signal_dict(x) for x in past.long]
    assert [signal_dict(x) for x in whole.short[:cutoff]] == [signal_dict(x) for x in past.short]
    np.testing.assert_array_equal(whole.exit_long[:cutoff], past.exit_long)
    np.testing.assert_array_equal(whole.exit_short[:cutoff], past.exit_short)
    np.testing.assert_array_equal(whole.valid[:cutoff], past.valid)


def test_four_hour_signal_waits_for_close_and_is_not_repeated():
    rows = [(100, 101, 99, 100)] * 800
    rows += [(100, 102, 99, 101), (101, 103, 100, 102),
             (102, 104, 101, 103), (103, 111, 102, 110)]
    rows += [(110, 111, 109, 110)] * 8
    ds = dataset(bars_from(rows, t0=0))
    descriptor = candidate("donchian_4h")
    pre = build_precomputed(ds, descriptor)
    assert [i for i, x in enumerate(pre.long) if x is not None] == [803]
    signal = pre.long[803]
    assert signal.close_time == 804 * 3600
    assert signal.bar_time == 800 * 3600
    assert signal.bar_index == 803
    assert not pre.valid[:803].any()
    assert pre.valid[803:].all()
    # Even if the full future HTF cache is present, an unfinished bar cannot leak.
    truncated = replace(ds, ltf=ds.ltf.upto(803), mark=ds.mark.upto(803))
    assert build_precomputed(truncated, descriptor).signal_count() == {"long": 0, "short": 0}


@pytest.mark.parametrize("direction", (1, -1))
def test_donchian_uses_prior_channel_and_atr_stop(direction):
    rows = [(100, 101, 99, 100)] * 200
    close = 100 + direction * 10
    rows.append((100, max(100, close) + 1, min(100, close) - 1, close))
    ds = dataset(bars_from(rows, t0=0))
    pre = build_precomputed(ds, candidate("donchian_1h"))
    signal = pre.entry(200, direction)
    assert signal is not None
    assert pre.entry(200, -direction) is None
    assert signal.entry == close
    assert signal.atr == pytest.approx((13 * 2 + 12) / 14)
    distance = 2 * signal.atr
    assert signal.stop == pytest.approx(close - direction * distance)
    assert signal.targets == pytest.approx([close + direction * 3 * distance])
    assert (pre.exit_short if direction > 0 else pre.exit_long)[200]


def test_ema_enters_only_on_cross_and_exits_opposite_side():
    rows = [(100, 101, 99, 100)] * 200
    rows += [(100, 111, 99, 110)] + [(110, 111, 109, 110)] * 9
    pre = build_precomputed(dataset(bars_from(rows, t0=0)), candidate("ema_cross_1h"))
    assert [i for i, x in enumerate(pre.long) if x is not None] == [200]
    assert pre.exit_short[200]
    assert not pre.exit_short[201:].any()
    assert pre.signal_count()["short"] == 0


@pytest.mark.parametrize("direction", (1, -1))
def test_bollinger_requires_reentry_and_exits_at_mean(direction):
    outside = 100 - direction * 10
    inside = 100 - direction * 5
    rows = [(100, 101, 99, 100)] * 200
    rows += [(100, max(100, outside) + 1, min(100, outside) - 1, outside),
             (outside, max(outside, inside) + 1, min(outside, inside) - 1, inside),
             (inside, max(inside, 100) + 1, min(inside, 100) - 1, 100)]
    pre = build_precomputed(dataset(bars_from(rows, t0=0)), candidate("bollinger_reentry_1h"))
    assert pre.entry(200, direction) is None  # Outside the band is not itself an entry.
    signal = pre.entry(201, direction)
    assert signal is not None
    assert signal.targets == pytest.approx([inside + direction * 1.5 * (2 * signal.atr)])
    assert (pre.exit_long if direction > 0 else pre.exit_short)[202]


def test_signal_is_executed_on_next_hour_with_gap_and_slippage():
    rows = [(80000, 80080, 79920, 80000)] * 200
    rows += [(80000, 81080, 79920, 81000),
             (81100, 81200, 81000, 81150),
             (81150, 81200, 81000, 81100)]
    ds = dataset(bars_from(rows, t0=0))
    descriptor = candidate("donchian_1h")
    pre = build_precomputed(ds, descriptor)
    result = Simulator(ds.ltf, ds.mark, ds.funding, ds.spec, pre).run(SimConfig(
        variant=descriptor.variant, valid_bars=descriptor.valid_bars,
        max_hold_bars=descriptor.max_hold_bars, slippage_bps=3,
    ))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade["signal_ts"] == trade["fill_ts"] == 201 * 3600
    assert trade["trigger"] == 81000
    assert trade["fill_px"] > 81100  # Next bar gaps above the trigger, then adverse slippage.


def test_candidate_space_is_fixed_and_custom_tuning_is_rejected():
    assert len(CANDIDATES) == 6
    assert len({c.name for c in CANDIDATES}) == 6
    assert {c.max_hold_bars for c in CANDIDATES} == {72, 288}
    ds = dataset(bars_from([(100, 101, 99, 100)] * 220, t0=0))
    with pytest.raises(ValueError, match="prespecified"):
        build_precomputed(ds, replace(CANDIDATES[0], target_r=20))
