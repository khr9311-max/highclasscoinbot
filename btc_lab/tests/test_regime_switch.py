"""Causality, switching and accounting checks for the regime-switch study."""
import numpy as np
import pytest

from btc_lab import regime_switch as rs


def stub_frame(days=6, seed=5):
    rng = np.random.default_rng(seed)
    fr = object.__new__(rs.Frame)
    fr.n = days * rs.DAY
    fr.open_ms = 1_700_006_400_000 // 86_400_000 * 86_400_000 + 300_000 * np.arange(fr.n, dtype=np.int64)
    close = 50000 * np.exp(np.cumsum(rng.normal(0, 0.002, fr.n)))
    open_ = np.concatenate(([50000.0], close[:-1]))
    wiggle = np.abs(rng.normal(0, 0.001, fr.n)) * close
    fr.px = {"open": open_, "close": close, "high": np.maximum(open_, close) + wiggle,
             "low": np.minimum(open_, close) - wiggle}
    vol = rng.uniform(100, 300, fr.n)
    fr.flow = {"cm_volume": vol, "cm_taker_buy": vol * rng.uniform(0.3, 0.7, fr.n),
               "um_volume": vol, "um_taker_buy": vol * rng.uniform(0.3, 0.7, fr.n),
               "premium_index": rng.normal(0, 1e-4, fr.n),
               "um_oi": 1e5 * np.exp(np.cumsum(rng.normal(0, 1e-3, fr.n)))}
    for k in ("top_account_ls", "top_position_ls", "global_ls"):
        fr.flow["um_" + k] = rng.uniform(0.8, 2.0, fr.n)
    fr.funding = np.zeros(fr.n)
    fr.funding[::96] = 1e-4
    fr.funding_known = np.full(fr.n, 1e-4)
    return fr


def test_features_do_not_read_future_bars():
    fr = stub_frame()
    base = rs.features(fr)
    cut = 3 * rs.DAY + 17
    later = stub_frame()
    rng = np.random.default_rng(99)
    for k in later.px:
        later.px[k] = later.px[k].copy()
        later.px[k][cut + 1:] *= rng.uniform(0.9, 1.1, later.n - cut - 1)
    for k in later.flow:
        later.flow[k] = later.flow[k].copy()
        later.flow[k][cut + 1:] *= rng.uniform(0.5, 1.5, later.n - cut - 1)
    changed = rs.features(later)
    for name, values in base.items():
        np.testing.assert_array_equal(values[:cut + 1], changed[name][:cut + 1], err_msg=name)


def test_metrics_enter_one_bar_after_their_row():
    fr = stub_frame()
    j = 2 * rs.DAY + 5
    bumped = stub_frame()
    bumped.flow["um_oi"] = bumped.flow["um_oi"].copy()
    bumped.flow["um_oi"][j] *= 1.5
    a, b = rs.features(fr)["doi_1"], rs.features(bumped)["doi_1"]
    assert a[j] == b[j]
    assert b[j + 1] > a[j + 1]


def test_hold_path_flips_extends_and_keeps_minimum_hold():
    s = np.array([1, 0, 0, 0, 0, 0, -1, 0, 1, 0, 0, 0], dtype=float)
    pos = rs.hold_path(s, 3)
    # position over bar i reflects the decision at close i-1
    assert list(pos) == [0, 1, 1, 1, 0, 0, 0, -1, -1, 1, 1, 1]
    extend = rs.hold_path(np.array([1, 0, 1, 0, 0, 0, 0], dtype=float), 2)
    assert list(extend) == [0, 1, 1, 1, 1, 0, 0]


def test_hold_path_ignores_missing_signals():
    pos = rs.hold_path(np.array([np.nan, 1, np.nan, np.nan, np.nan]), 1)
    assert list(pos) == [0, 0, 1, 0, 0]


def test_inverse_path_returns_and_costs():
    close = np.array([100.0, 110.0, 110.0])
    pos = np.array([0.0, 1.0, 0.0])
    r = rs.path_returns(close, np.zeros(3), pos, np.full(3, 0.001))
    assert r[1] == pytest.approx((1 - 100 / 110) - 0.001)
    assert r[2] == pytest.approx(-0.001)
    short = rs.path_returns(close, np.array([0.0, 0.0002, 0.0]), -pos, np.zeros(3))
    assert short[1] == pytest.approx(-(1 - 100 / 110) + 0.0002)   # shorts receive positive funding


def test_forward_return_is_inverse_and_undefined_at_the_end():
    close = np.array([100.0, 105.0, 110.0, 120.0])
    fwd = rs.forward_return(close, 2)
    assert fwd[0] == pytest.approx(1 - 100 / 110)
    assert np.isnan(fwd[-2:]).all()


def test_cell_table_needs_cost_and_significance():
    rng = np.random.default_rng(1)
    n = 6000
    cell = np.zeros(n)
    cell[n // 2:] = 1
    fwd = rng.normal(0, 0.001, n)
    fwd[n // 2:] += 0.01          # cell 1: large positive forward return
    actions, _ = rs.cell_table(cell, fwd, 2, np.arange(n), cost_rt=0.0016, need_t=True)
    assert actions[1] == 1 and actions[0] == 0
    fwd[n // 2:] = 0.002 + rng.normal(0, 0.05, n // 2)   # beyond cost but noisy
    strict, _ = rs.cell_table(cell, fwd, 2, np.arange(n), 0.0016, True)
    assert strict[1] == 0


def test_cm02_requires_both_flows_and_oi_build():
    f = {"um_bs_1": np.array([1.2, 1.2, 1.1]), "um_bs_3": np.array([1.2, 1.2, 1.2]),
         "doi_12": np.array([0.001, -0.001, 0.001])}
    assert list(rs.cm02_signal(f)) == [1, 0, 0]


def test_non_overlapping_keeps_every_h_th_bar_of_a_run():
    idx = np.array([0, 1, 2, 3, 4, 5, 6, 10, 11])
    assert list(rs.non_overlapping(idx, 3)) == [0, 3, 6, 10]
