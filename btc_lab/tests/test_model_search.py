"""Depth parsing and causality of the model-search features."""
import io
import zipfile

import numpy as np
import pytest

from btc_lab import extra_data as ex
from btc_lab import model_search as ms


def depth_zip(rows):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("d.csv", "timestamp,percentage,depth,notional\n" + "\n".join(rows))
    return buf.getvalue()


def test_depth_day_keeps_the_last_snapshot_before_each_bar_close():
    start = 1_790_208_000_000                                    # 2026-09-24 00:00 UTC
    snap = lambda t, bid, ask: [f"{t},-{b},{bid},0" for b in (0.2, 1.00, 5.00)] + [f"{t},{b},{ask},0" for b in (0.2, 1.00, 5.00)]
    rows = snap("2026-09-24 00:00:01", 10, 10) + snap("2026-09-24 00:04:59", 30, 10) + snap("2026-09-24 00:05:00", 10, 30)
    f = ex.depth_day(depth_zip(rows), start)
    assert list(f.index) == [0, 1]
    assert f.loc[0, "imb_1"] == pytest.approx((30 - 10) / 40)     # 00:04:59 is the last one in bar 0
    assert f.loc[1, "imb_1"] == pytest.approx(-0.5)
    assert f.loc[0, "total_1"] == pytest.approx(40)


def stub(n=2000, seed=4):
    rng = np.random.default_rng(seed)
    data = {}
    for v in ("um", "cm"):
        for b in ("0.2", "1", "5"):
            data[f"{v}_imb_{b}"] = rng.uniform(-0.3, 0.3, n)
        data[f"{v}_depth_1"] = rng.uniform(1000, 5000, n)
    for s in ex.COINS:
        data[f"{s}_close"] = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
        data[f"{s}_volume"] = rng.uniform(10, 20, n)
        data[f"{s}_taker_buy"] = data[f"{s}_volume"] * rng.uniform(0.3, 0.7, n)
    base = {"ret_12": rng.normal(0, 0.003, n)}
    return data, base


def test_new_features_do_not_read_future_bars():
    data, base = stub()
    cut = 1500
    later = {k: v.copy() for k, v in data.items()}
    for k in later:
        later[k][cut + 1:] *= 1.37
    a = {**ms.depth_features(data), **ms.cross_features(data, base)}
    b = {**ms.depth_features(later), **ms.cross_features(later, base)}
    for name in a:
        np.testing.assert_array_equal(a[name][:cut + 1], b[name][:cut + 1], err_msg=name)


def test_long_timeframe_features_do_not_read_future_bars():
    from types import SimpleNamespace
    rng = np.random.default_rng(8)
    n, cut = 7000, 6000
    close = 50000 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    px = {"close": close, "high": close * 1.001, "low": close * 0.999}
    later = {k: v.copy() for k, v in px.items()}
    for k in later:
        later[k][cut + 1:] *= 1.2
    base = {"vol_288": np.full(n, 0.002)}
    a = ms.long_features(SimpleNamespace(px=px), base)
    b = ms.long_features(SimpleNamespace(px=later), base)
    for name in a:
        np.testing.assert_array_equal(a[name][:cut + 1], b[name][:cut + 1], err_msg=name)
