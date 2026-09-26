"""Live ledger: tree export parity, REST-to-frame parity, scoring and report."""
from types import SimpleNamespace

import numpy as np
import pytest

from btc_lab import ledger as lg
from btc_lab import regime_switch as rs
from btc_lab.tests.test_regime_switch import stub_frame


def raw_from(fr):
    """REST-shaped responses equivalent to a research frame (metrics stamped +5m like the API)."""
    bar = lg.BAR

    def kline(i, volume, taker, o=None, h=None, low=None, c=None):
        t = int(fr.open_ms[i])
        return [t, str(o), str(h), str(low), str(c), str(volume), t + bar - 1, "0", 0, str(taker), "0", "0"]

    cm = [kline(i, fr.flow["cm_volume"][i], fr.flow["cm_taker_buy"][i], fr.px["open"][i], fr.px["high"][i],
                fr.px["low"][i], fr.px["close"][i]) for i in range(fr.n)]
    um = [kline(i, fr.flow["um_volume"][i], fr.flow["um_taker_buy"][i], 1, 1, 1, 1) for i in range(fr.n)]
    prem = [kline(i, 0, 0, 0, 0, 0, fr.flow["premium_index"][i]) for i in range(fr.n)]

    def metric(field, key):
        return [{"timestamp": int(fr.open_ms[i]) + bar, field: str(fr.flow[key][i])} for i in range(fr.n)]
    funding = [{"fundingTime": int(fr.open_ms[i]), "fundingRate": str(fr.funding[i])}
               for i in np.flatnonzero(fr.funding)]
    return {"cm": cm, "um": um, "premium": prem, "oi": metric("sumOpenInterest", "um_oi"),
            "top_account": metric("longShortRatio", "um_top_account_ls"),
            "top_position": metric("longShortRatio", "um_top_position_ls"),
            "global": metric("longShortRatio", "um_global_ls"), "funding": funding}


def test_live_frame_reproduces_research_features():
    fr = stub_frame()
    now = int(fr.open_ms[-1]) + lg.BAR + 20_000
    raw = raw_from(fr)
    raw["cm"].append([now - 20_000, "1", "1", "1", "1", "1", now + 280_000, "0", 0, "1", "0", "0"])  # still open
    live = lg.LiveFrame(raw, now, lookback=fr.n)
    assert live.open_ms[-1] == fr.open_ms[-1]
    a, b = rs.features(fr), rs.features(live)
    for name in rs.LGBM_FEATURES:
        np.testing.assert_allclose(b[name][300:], a[name][300:], rtol=1e-12, equal_nan=True, err_msg=name)


def test_metrics_are_placed_five_minutes_before_their_api_stamp():
    fr = stub_frame()
    raw = raw_from(fr)
    live = lg.LiveFrame(raw, int(fr.open_ms[-1]) + lg.BAR + 1, lookback=fr.n)
    np.testing.assert_allclose(live.flow["um_oi"], fr.flow["um_oi"])


def test_live_frame_refuses_a_missing_latest_bar():
    fr = stub_frame()
    raw = raw_from(fr)
    with pytest.raises(ValueError):
        lg.LiveFrame(raw, int(fr.open_ms[-1]) + 2 * lg.BAR + 1, lookback=fr.n)


def test_tree_export_matches_lightgbm_with_missing_values():
    lgb = pytest.importorskip("lightgbm")
    rng = np.random.default_rng(0)
    x = rng.normal(size=(3000, 5)).astype(np.float32)
    x[rng.random(x.shape) < 0.1] = np.nan
    x[:50, 2] = 0.0
    y = np.nan_to_num(x[:, 0]) * 0.5 - np.nan_to_num(x[:, 1]) ** 2 + rng.normal(0, 0.1, 3000)
    model = lgb.LGBMRegressor(n_estimators=40, num_leaves=15, min_child_samples=20, verbose=-1).fit(x, y)
    trees = lg.export_booster(model.booster_)
    test = x[:400]
    mine = np.array([lg.predict_one(trees, [float(v) for v in row]) for row in test])
    np.testing.assert_allclose(mine, model.predict(test), atol=1e-12)


def test_score_fills_inverse_forward_returns(tmp_path):
    db = lg.open_db(tmp_path)
    open_ms = 1_700_000_000_000 // lg.BAR * lg.BAR + lg.BAR * np.arange(100, dtype=np.int64)
    close = np.linspace(100, 110, 100)
    fr = SimpleNamespace(open_ms=open_ms, n=100, px={"close": close})
    t = int(open_ms[10]) + lg.BAR
    lg.store(db, {"bar_close_ms": t, "created_ms": t, "close": float(close[10]), "pred": {"12": 0.001, "24": 0.002}})
    lg.score(db, fr)
    f12, f24, f48 = db.execute("SELECT fwd_12, fwd_24, fwd_48 FROM snapshots").fetchone()
    assert f12 == pytest.approx(1 - close[10] / close[22])
    assert f48 == pytest.approx(1 - close[10] / close[58])
    db.close()


def test_report_handles_missing_values():
    snap = {"bar_close_ms": 1_790_380_800_000, "funding_next_ms": 1_790_409_600_000, "mark": 83900.0,
            "index": 83950.0, "premium": -0.0006, "close": 83900.0, "daily_open": 84000.0, "vwap": None,
            "taker_bs": {"um": {"1": None, "3": 1.1, "12": 0.9}, "cm": {"1": None, "3": None, "12": None}},
            "cm_oi_contracts": 12_600_000.0, "cm_oi_btc": 15000.0, "um_oi_btc": None,
            "um_doi": {"1": None, "3": None, "12": 0.001, "48": None}, "top_position_long": None,
            "top_position_ls": None, "top_position_ls_d1h": None, "global_ls": 1.3, "funding_last": -6.5e-5,
            "funding_next_est": 4e-5, "atr": {"5m": None, "15m": 80.0, "1h": 290.0}, "situation": None,
            "pred": {"12": -0.0001, "24": 0.0002}, "cost_round_trip": 0.0016, "swing": None, "metrics_fresh": False}
    text = lg.report_text(snap, {"7일": {"n": 3}})
    assert "매수 체결에 유리" in text and "표본 부족" in text and "주의" in text


def test_cell_label_matches_research_cells():
    f = {"trend_z": np.array([0.5]), "um_flow_3": np.array([-0.1]), "vol_ratio": np.array([2.0]),
         "doi_12": np.array([0.01])}
    cuts = {"trend_z": [-0.28, 0.30], "um_flow_3": [-0.03, 0.03], "vol_ratio": 0.78}
    cell = lg.cell_label(f, 0, cuts)
    assert cell["id"] == 2 * 12 + 6 + 0 * 2 + 1
    assert cell["text"] == "상승·고변동·매도우위·OI증가"


def test_prediction_file_is_what_the_trading_timing_reads(tmp_path):
    from btc_portfolio.timing import read_prediction
    snap = {"bar_close_ms": 1_790_403_000_000, "created_ms": 1_790_403_020_000, "pred": {"12": 1e-4, "24": -3e-4}}
    lg.write_prediction(tmp_path, snap, {"train_end_ms": 1})
    assert read_prediction(tmp_path / "prediction.json", 1_790_403_020_000) == (-3e-4, None)
    assert not (tmp_path / "prediction.json.tmp").exists()
