"""Pre-registered model search for the 2h BTCUSD_PERP model.

Question: do order-book depth, cross-coin flow and a volatility-normalized target
raise the out-of-sample rank IC of the model the ledger and timing overlay use?

Fixed before the results were read:
- Data from 2023-01-01 (start of the order-book depth archive) on the flow grid.
  Target: inverse forward 2h (24-bar) BTC return of BTCUSD_PERP.
- Monthly walk-forward: fit on all bars since 2023-01-02 whose labels end one
  day before the test month; test months 2024-01 .. 2026-08. LightGBM
  parameters as regime_switch.
- Models: A (the live 37 features), B (A + depth), C (A + cross-coin),
  L (A + longer timeframes: 3/7-day returns, 4h EMA20/80 gap, distance to the
  20-day high/low, 1-day vs 7-day volatility), D (A + depth + cross + longer),
  A_norm and D_norm (target divided by the prior day's 5m volatility x sqrt(24)).
  L was added on 2026-09-26 before any result was read.
- Metrics on non-overlapping 2h windows: rank IC for the whole test, each
  half and each month; and the top 20% |score| windows of each month: hit
  rate, gross and net (-0.16% round trip) BTC return per trade.
- Adoption: a model replaces A only if its test IC exceeds A's by >= 0.01, it
  is higher in both halves, and the monthly IC difference has t > 2.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

from btc_lab import extra_data
from btc_lab import regime_switch as rs
from btc_lab import strategy_search as ss

EXTRA = extra_data.OUTPUT / "extra_5m.npz"
OUTPUT = rs.ROOT / "btc_lab/state/model_search_20260926"
H = 24
START = "2023-01-01"
TEST = ("2024-01-01", "2026-09-01")
HALVES = (("2024-01-01", "2025-05-01"), ("2025-05-01", "2026-09-01"))
COST_RT = 0.0016
MODELS = ("A", "B", "C", "L", "D", "A_norm", "D_norm")


def ffill(x, limit=None):
    return pd.Series(x).ffill(limit=limit).to_numpy()


def depth_features(ex):
    f = {}
    for v in ("um", "cm"):
        for b in ("0.2", "1", "5"):
            f[f"{v}_imb_{b}"] = ffill(ex[f"{v}_imb_{b}"], limit=2)
        i1 = f[f"{v}_imb_1"]
        f[f"{v}_dimb_1_3"] = i1 - rs.lag(i1, 3)
        f[f"{v}_dimb_1_12"] = i1 - rs.lag(i1, 12)
        total = ffill(ex[f"{v}_depth_1"], limit=2)
        with np.errstate(divide="ignore", invalid="ignore"):
            f[f"{v}_liq_1"] = np.log(total / pd.Series(total).rolling(288, min_periods=100).median().to_numpy())
    return f


def cross_features(ex, base):
    f, r3s, r12s, fl3s, fl12s = {}, [], [], [], []
    with np.errstate(divide="ignore", invalid="ignore"):
        for s in extra_data.COINS:
            c, v, tb = ex[f"{s}_close"], ex[f"{s}_volume"], ex[f"{s}_taker_buy"]
            r3, r12 = rs.log_change(c, 3), rs.log_change(c, 12)
            fl3 = rs.rolling_sum(tb, 3) / rs.rolling_sum(v, 3) - 0.5
            fl12 = rs.rolling_sum(tb, 12) / rs.rolling_sum(v, 12) - 0.5
            f[f"{s}_r3"], f[f"{s}_r12"], f[f"{s}_flow12"] = r3, r12, fl12
            r3s.append(r3), r12s.append(r12), fl3s.append(fl3), fl12s.append(fl12)
        mean = lambda xs: np.nanmean(np.vstack(xs), 0)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            f["alt_r3"], f["alt_r12"] = mean(r3s), mean(r12s)
            f["alt_flow3"], f["alt_flow12"] = mean(fl3s), mean(fl12s)
        f["alt_minus_btc_r12"] = f["alt_r12"] - base["ret_12"]
    return f


def long_features(fr, base):
    c, h, low = fr.px["close"], fr.px["high"], fr.px["low"]
    f = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        f["ret_864"], f["ret_2016"] = rs.log_change(c, 864), rs.log_change(c, 2016)
        fast = pd.Series(c).ewm(span=20 * 48, adjust=False).mean().to_numpy()      # 4h EMA20 on 5m closes
        slow = pd.Series(c).ewm(span=80 * 48, adjust=False).mean().to_numpy()      # 4h EMA80
        f["ema_4h_gap"] = fast / slow - 1
        f["dist_high_20d"] = np.log(c / pd.Series(h).rolling(5760, min_periods=2000).max().to_numpy())
        f["dist_low_20d"] = np.log(c / pd.Series(low).rolling(5760, min_periods=2000).min().to_numpy())
        lr = np.diff(np.log(c), prepend=np.nan)
        f["vol_ratio_7d"] = base["vol_288"] / pd.Series(lr).rolling(2016, min_periods=1000).std().to_numpy()
    return f


def feature_sets(base, depth, cross, longer):
    a = list(rs.LGBM_FEATURES)
    return {"A": a, "B": a + list(depth), "C": a + list(cross), "L": a + list(longer),
            "D": a + list(depth) + list(cross) + list(longer)}


def walk_forward(fr, cols, target, start):
    import lightgbm as lgb
    x = np.column_stack(cols).astype(np.float32)
    pred = np.full(fr.n, np.nan)
    months = pd.date_range(TEST[0], TEST[1], freq="MS", tz="UTC")
    for m0, m1 in zip(months[:-1], months[1:]):
        a, b = fr.index(m0.strftime("%Y-%m-%d")), fr.index(m1.strftime("%Y-%m-%d"))
        idx = np.arange(start, a - H - rs.DAY, rs.TRAIN_STEP)
        idx = idx[np.isfinite(target[idx])]
        y = target[idx]
        lo, hi = np.percentile(y, [0.5, 99.5])
        model = lgb.LGBMRegressor(**rs.LGBM_PARAMS).fit(x[idx], np.clip(y, lo, hi))
        pred[a:b] = model.predict(x[a:b])
    return pred


def evaluate(fr, pred, long_r, short_r):
    a, b = fr.index(TEST[0]), fr.index(TEST[1])
    w = np.arange(a, b - H, H)
    w = w[np.isfinite(pred[w]) & np.isfinite(long_r[w])]
    out = {"ic": ss.spearman(pred[w], long_r[w]), "windows": int(len(w))}
    for k, (h0, h1) in enumerate(HALVES):
        m = (w >= fr.index(h0)) & (w < fr.index(h1))
        out[f"ic_half{k + 1}"] = ss.spearman(pred[w[m]], long_r[w[m]])
    month = (fr.open_ms[w] // 86_400_000).astype("datetime64[D]").astype("datetime64[M]")
    monthly, hits, gross = {}, [], []
    for mth in np.unique(month):
        ww = w[month == mth]
        monthly[str(mth)] = ss.spearman(pred[ww], long_r[ww])
        top = ww[np.abs(pred[ww]) >= np.percentile(np.abs(pred[ww]), 80)]
        r = np.where(pred[top] > 0, long_r[top], short_r[top])
        hits += list(r > 0)
        gross += list(r)
    out["monthly_ic"] = monthly
    out["top20_trades"] = len(gross)
    out["top20_hit"] = float(np.mean(hits))
    out["top20_gross"] = float(np.mean(gross))
    out["top20_net"] = float(np.mean(gross) - COST_RT)
    return out


def study(output=OUTPUT):
    fr = rs.Frame()
    base = rs.features(fr)
    ex = np.load(EXTRA)
    if not np.array_equal(ex["open_ms"], fr.open_ms):
        raise ValueError("Extra data grid differs from the flow grid")
    ex = {k: ex[k] for k in ex.files}
    depth, cross, longer = depth_features(ex), cross_features(ex, base), long_features(fr, base)
    allf = {**base, **depth, **cross, **longer}
    sets = feature_sets(base, depth, cross, longer)
    close = fr.px["close"]
    long_r = rs.forward_return(close, H)
    short_r = np.full(fr.n, np.nan)
    short_r[:-H] = close[:-H] / close[H:] - 1
    norm = long_r / (base["vol_288"] * math.sqrt(H))
    start = fr.index(START) + rs.DAY
    results, preds = {}, {}
    for name in MODELS:
        target = norm if name.endswith("_norm") else long_r
        cols = [allf[k] for k in sets[name[0]]]
        pred = walk_forward(fr, cols, target, start)
        preds[name] = pred
        results[name] = {"features": len(cols), **evaluate(fr, pred, long_r, short_r)}
        r = results[name]
        print(f"{name:7} feats {r['features']:3d} IC {r['ic']:+.4f} (halves {r['ic_half1']:+.4f} / {r['ic_half2']:+.4f}) "
              f"top20 hit {r['top20_hit']*100:.1f}% gross {r['top20_gross']*100:+.3f}% net {r['top20_net']*100:+.3f}%", flush=True)
    base_monthly = results["A"]["monthly_ic"]
    for name in MODELS[1:]:
        r = results[name]
        diff = np.array([r["monthly_ic"][m] - base_monthly[m] for m in base_monthly])
        r["monthly_diff_mean"] = float(diff.mean())
        r["monthly_diff_t"] = float(diff.mean() / diff.std(ddof=1) * math.sqrt(len(diff)))
        r["adopt"] = bool(r["ic"] - results["A"]["ic"] >= 0.01 and r["ic_half1"] > results["A"]["ic_half1"]
                          and r["ic_half2"] > results["A"]["ic_half2"] and r["monthly_diff_t"] > 2)
        print(f"{name:7} vs A: IC {r['ic'] - results['A']['ic']:+.4f}, monthly diff {r['monthly_diff_mean']:+.4f} "
              f"(t {r['monthly_diff_t']:+.2f}) -> adopt {r['adopt']}", flush=True)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps({"test": TEST, "halves": HALVES, "models": results,
                                                     "depth_features": list(depth), "cross_features": list(cross),
                                                     "long_features": list(longer)},
                                                    indent=1, default=float))
    np.savez_compressed(output / "predictions.npz", open_ms=fr.open_ms, **preds)
    return results


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    study()


if __name__ == "__main__":
    main()
