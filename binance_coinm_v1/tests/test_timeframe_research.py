"""Causality, UTC aggregation and unchanged EMA rules for timeframe research."""

from dataclasses import replace

import numpy as np
import pytest

from binance_coinm_v1.backtest.data import Dataset
from binance_coinm_v1.backtest.research_signals import (
    CANDIDATES as PREVIOUS_CANDIDATES, build_precomputed as previous_precomputed,
)
from binance_coinm_v1.backtest.timeframe_research import (
    CANDIDATES, aggregate_complete_utc, build_precomputed, closed_filter_direction,
    freeze_protocol,
)
from binance_coinm_v1.strategy.price_action import Bars

from .helpers import load_spec
from .synth import bars_from, random_walk


def dataset(bars):
    return Dataset("BTCUSD_PERP", bars, aggregate_complete_utc(bars, 4), bars, 0, [], load_spec())


def signal_dict(signal):
    return signal.to_dict() if signal else None


def aligned_walk(n, seed):
    bars = random_walk(n, seed=seed)
    return replace(bars, t=np.arange(n, dtype=float) * 3600)


def test_utc_aggregation_drops_partial_and_missing_buckets():
    # Starts at 01:00. 00:00-04:00 is incomplete. 08:00-12:00 lacks 09:00.
    times = [i * 3600 for i in range(1, 15) if i != 9]
    b = Bars.from_rows([(t, i + 100, i + 103, i + 98, i + 101, i + 1)
                       for i, t in enumerate(times)], 3600)
    aggregated = aggregate_complete_utc(b, 4)
    assert aggregated.t.tolist() == [4 * 3600]
    ix = np.flatnonzero((b.t >= 4 * 3600) & (b.t < 8 * 3600))
    assert aggregated.o[0] == b.o[ix[0]]
    assert aggregated.c[0] == b.c[ix[-1]]
    assert aggregated.h[0] == max(b.h[ix])
    assert aggregated.l[0] == min(b.l[ix])
    assert aggregated.v[0] == sum(b.v[ix])
    assert aggregated.period == 4 * 3600


def test_utc_aggregation_rejects_unaligned_duplicate_and_subhour_input():
    b = bars_from([(100, 101, 99, 100)] * 5, t0=0)
    with pytest.raises(ValueError, match="UTC-aligned"):
        aggregate_complete_utc(replace(b, t=b.t + 1), 4)
    with pytest.raises(ValueError, match="unique"):
        aggregate_complete_utc(replace(b, t=np.array([0, 3600, 3600, 7200, 10800])), 4)
    with pytest.raises(ValueError, match="1h input"):
        aggregate_complete_utc(replace(b, period=900), 4)
    assert len(aggregate_complete_utc(b.upto(0), 4)) == 0


@pytest.mark.parametrize("hours", (1, 4))
def test_ema_1h_and_4h_match_previous_research_exactly(hours):
    # UTC alignment guarantees identical completed source candles.
    ds = dataset(aligned_walk(3600, seed=731))
    c = next(c for c in CANDIDATES if c.source_hours == hours and c.filter_hours is None)
    old = next(c for c in PREVIOUS_CANDIDATES if c.name == c_name(hours))
    actual, expected = build_precomputed(ds, c), previous_precomputed(ds, old)
    assert sum(actual.signal_count().values()) > 0
    assert list(map(signal_dict, actual.long)) == list(map(signal_dict, expected.long))
    assert list(map(signal_dict, actual.short)) == list(map(signal_dict, expected.short))
    np.testing.assert_array_equal(actual.valid, expected.valid)
    np.testing.assert_array_equal(actual.exit_long, expected.exit_long)
    np.testing.assert_array_equal(actual.exit_short, expected.exit_short)


def c_name(hours):
    return f"ema_cross_{hours}h"


@pytest.mark.parametrize("candidate", CANDIDATES, ids=lambda c: c.name)
def test_future_candles_do_not_change_signals_or_htf_filters(candidate):
    ds = dataset(aligned_walk(8500, seed=487))
    cutoff = 7603  # Inside all HTF buckets, including the UTC daily bar.
    prefix = dataset(ds.ltf.upto(cutoff))
    all_pre, past_pre = build_precomputed(ds, candidate), build_precomputed(prefix, candidate)
    assert sum(past_pre.signal_count().values()) > 0
    assert list(map(signal_dict, all_pre.long[:cutoff])) == list(map(signal_dict, past_pre.long))
    assert list(map(signal_dict, all_pre.short[:cutoff])) == list(map(signal_dict, past_pre.short))
    for attr in ("valid", "exit_long", "exit_short"):
        np.testing.assert_array_equal(getattr(all_pre, attr)[:cutoff], getattr(past_pre, attr))


def test_filter_uses_last_closed_candle_and_waits_for_its_warmup():
    # A positive daily candle has no effect at any time before its close.
    rows = [(100, 101, 99, 100)] * (200 * 24)
    rows += [(100, 121, 99, 120)] * 24
    rows += [(120, 121, 69, 70)] * 24
    bars = bars_from(rows, t0=0)
    daily = aggregate_complete_utc(bars, 24)
    close_times = np.array([200 * 86400, 201 * 86400 - 3600, 201 * 86400,
                            202 * 86400 - 3600, 202 * 86400])
    directions, ready = closed_filter_direction(daily, close_times)
    assert ready.tolist() == [False, False, True, True, True]
    assert directions.tolist() == [0, 0, 1, 1, -1]


@pytest.mark.parametrize("candidate", [c for c in CANDIDATES if c.filter_hours])
def test_filter_only_removes_entries_and_never_changes_cross_exits(candidate):
    ds = dataset(aligned_walk(8500, seed=487))
    unfiltered = next(c for c in CANDIDATES if c.source_hours == candidate.source_hours
                      and c.filter_hours is None)
    original, filtered = build_precomputed(ds, unfiltered), build_precomputed(ds, candidate)
    direction, ready = closed_filter_direction(aggregate_complete_utc(ds.ltf, candidate.filter_hours),
                                               ds.ltf.t + ds.ltf.period)
    np.testing.assert_array_equal(original.exit_long, filtered.exit_long)
    np.testing.assert_array_equal(original.exit_short, filtered.exit_short)
    removed = 0
    for side in (1, -1):
        for i in range(len(ds.ltf)):
            old, new = original.entry(i, side), filtered.entry(i, side)
            if new:
                assert old is not None and ready[i] and direction[i] == side
                assert new.entry == old.entry and new.stop == old.stop and new.targets == old.targets
            elif old:
                removed += 1
                assert not ready[i] or direction[i] != side
    assert removed > 0


def test_protocol_is_frozen_but_identical_json_roundtrip_can_rerun(tmp_path):
    path = tmp_path / "protocol.json"
    initial = {"created_at": "first", "rules": {"directions": (1, -1)}, "data_hash": "abc"}
    freeze_protocol(path, initial)
    repeated = freeze_protocol(path, {**initial, "created_at": "second"})
    assert repeated["created_at"] == "first"
    with pytest.raises(ValueError, match="Frozen research protocol changed"):
        freeze_protocol(path, {**initial, "data_hash": "changed"})


def test_candidate_space_fixed_and_unsupported_tuning_rejected():
    assert len(CANDIDATES) == 9 and len({c.name for c in CANDIDATES}) == 9
    ds = dataset(bars_from([(100, 101, 99, 100)] * 210, t0=0))
    with pytest.raises(ValueError, match="nine prespecified"):
        build_precomputed(ds, replace(CANDIDATES[0], target_r=9))
