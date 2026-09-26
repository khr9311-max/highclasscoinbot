"""Exploratory search for a stronger BTCUSD_PERP direction model, with a locked holdout.

Protocol, fixed on 2026-09-26 before any run of this module:
- Search window: walk-forward test months 2024-01 .. 2025-12. Every choice of
  horizon, features, target and model settings is made on this window only.
- Locked holdout: 2026-01 .. 2026-08, evaluated once for finalists written
  down before that run; the result is reported whatever it is. Caveat:
  model_search already reported 2h results for feature sets A-D over a
  window that included these months.
- Training starts 2023-01-02 (order-book depth archive), expands, is refit
  every month, and its labels end one day before the test month.
- Economic metric: each month the top 20% |score| non-overlapping windows are
  traded long/short for the horizon; net = BTC return - 0.16% round trip.
  Reported per trade and per day.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

from btc_lab import model_search as ms
from btc_lab import regime_switch as rs
from btc_lab import strategy_search as ss

OUTPUT = rs.ROOT / "btc_lab/state/predict_search_20260926"
START = "2023-01-01"
SEARCH = ("2024-01-01", "2026-01-01")
HOLDOUT = ("2026-01-01", "2026-09-01")
COST_RT = 0.0016
PARAMS = {
    "base": rs.LGBM_PARAMS,
    "slow": dict(rs.LGBM_PARAMS, n_estimators=600, learning_rate=0.015, num_leaves=15, min_child_samples=4000,
                 colsample_bytree=0.5, reg_lambda=5.0),
}


EXTERNAL = rs.ROOT / "btc_lab/state/external_market/external_5m.npz"


def external_features(ext, base, cm_close):
    """Coinbase premium, kimchi premium and DVOL features; index i uses bars <= i."""
    ff = lambda x, limit: pd.Series(x).ffill(limit=limit).to_numpy()
    f = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        cb = ff(ext["cb_close"], 2)
        f["cb_prem"] = np.log(cb / cm_close)
        f["cb_prem_12"] = pd.Series(f["cb_prem"]).rolling(12, min_periods=6).mean().to_numpy()
        f["cb_prem_d12"] = f["cb_prem"] - rs.lag(f["cb_prem"], 12)
        f["cb_prem_d48"] = f["cb_prem"] - rs.lag(f["cb_prem"], 48)
        cbv = np.nan_to_num(ext["cb_volume"])
        f["cb_vol_z"] = np.log(rs.rolling_sum(cbv, 12) / (rs.rolling_sum(cbv, 288) / 24))
        kim = np.log(ff(ext["up_btc_close"], 6) / (ff(ext["up_usdt_close"], 288) * cm_close))
        f["kimchi"] = kim
        f["kimchi_d12"], f["kimchi_d288"] = kim - rs.lag(kim, 12), kim - rs.lag(kim, 288)
        roll = pd.Series(kim).rolling(2016, min_periods=500)
        f["kimchi_z"] = (kim - roll.mean().to_numpy()) / roll.std().to_numpy()
        upv = np.nan_to_num(ext["up_btc_volume"])
        f["up_vol_z"] = np.log(rs.rolling_sum(upv, 12) / (rs.rolling_sum(upv, 288) / 24))
        dv = ff(ext["dvol"], 24)
        f["dvol"], f["dvol_d12"], f["dvol_d288"] = dv, dv - rs.lag(dv, 12), dv - rs.lag(dv, 288)
        f["iv_rv"] = (dv / 100) / (base["vol_288"] * math.sqrt(288 * 365))
    return f


def load_external(base, cm_close, open_ms):
    z = np.load(EXTERNAL)
    if not np.array_equal(z["open_ms"], open_ms):
        raise ValueError("External data grid differs from the flow grid")
    return external_features({k: z[k] for k in z.files}, base, cm_close)


class Data:
    """Research frame, all feature sets, and forward returns for any horizon."""

    def __init__(self, external=False):
        self.fr = fr = rs.Frame()
        base = rs.features(fr)
        external = load_external(base, fr.px["close"], fr.open_ms) if external else None
        ex = np.load(ms.EXTRA)
        ex = {k: ex[k] for k in ex.files}
        depth, cross, longer = ms.depth_features(ex), ms.cross_features(ex, base), ms.long_features(fr, base)
        self.f = {**base, **depth, **cross, **longer, **(external or {})}
        a = list(rs.LGBM_FEATURES)
        self.sets = {"A": a, "D": a + list(depth) + list(cross) + list(longer)}
        if external:
            self.sets["DX"] = self.sets["D"] + list(external)
        self.close = fr.px["close"]
        self.vol288 = base["vol_288"]
        self.start = fr.index(START) + rs.DAY

    def returns(self, h):
        c = self.close
        long_r, short_r = np.full(len(c), np.nan), np.full(len(c), np.nan)
        long_r[:-h] = 1 - c[:-h] / c[h:]
        short_r[:-h] = c[:-h] / c[h:] - 1
        return long_r, short_r


def target_for(kind, long_r, vol288, h):
    if kind == "ret":
        return long_r
    if kind == "big":                                        # only moves that could pay the round trip
        return np.where(np.abs(long_r) > COST_RT, long_r, 0.0) * np.where(np.isnan(long_r), np.nan, 1)
    if kind == "sign":
        return np.where(np.isnan(long_r), np.nan, (long_r > 0).astype(float))
    raise ValueError(kind)


def fit_predict(x, y, idx, rows, kind, params, seeds=1):
    import lightgbm as lgb
    out = np.zeros(len(rows))
    for s in range(seeds):
        p = dict(params, random_state=params.get("random_state", 7) + s)
        if kind == "sign":
            m = lgb.LGBMClassifier(**p).fit(x[idx], y[idx].astype(int))
            out += m.predict_proba(x[rows])[:, 1] - 0.5
        else:
            yy = y[idx]
            lo, hi = np.percentile(yy, [0.5, 99.5])
            m = lgb.LGBMRegressor(**p).fit(x[idx], np.clip(yy, lo, hi))
            out += m.predict(x[rows])
    return out / seeds


def walk_forward(d, cfg, window):
    h = cfg["h"]
    long_r, _ = d.returns(h)
    y = target_for(cfg["target"], long_r, d.vol288, h)
    x = np.column_stack([d.f[k] for k in d.sets[cfg["features"]]]).astype(np.float32)
    pred = np.full(d.fr.n, np.nan)
    months = pd.date_range(window[0], window[1], freq="MS", tz="UTC")
    for m0, m1 in zip(months[:-1], months[1:]):
        a, b = d.fr.index(m0.strftime("%Y-%m-%d")), d.fr.index(m1.strftime("%Y-%m-%d"))
        idx = np.arange(d.start, a - h - rs.DAY, rs.TRAIN_STEP)
        idx = idx[np.isfinite(y[idx])]
        pred[a:b] = fit_predict(x, y, idx, np.arange(a, b), cfg["target"], PARAMS[cfg["params"]], cfg.get("seeds", 1))
    return pred


def evaluate(d, pred, h, window):
    fr = d.fr
    long_r, short_r = d.returns(h)
    a, b = fr.index(window[0]), fr.index(window[1])
    w = np.arange(a, b - h, h)
    w = w[np.isfinite(pred[w]) & np.isfinite(long_r[w])]
    month = (fr.open_ms[w] // 86_400_000).astype("datetime64[D]").astype("datetime64[M]")
    monthly, gross = [], []
    for mth in np.unique(month):
        ww = w[month == mth]
        monthly.append(ss.spearman(pred[ww], long_r[ww]))
        top = ww[np.abs(pred[ww]) >= np.percentile(np.abs(pred[ww]), 80)]
        gross += list(np.where(pred[top] > 0, long_r[top], short_r[top]))
    gross = np.array(gross)
    monthly = np.array(monthly)
    days = (b - a) / rs.DAY
    # Held positions use thresholds from earlier months only (the first month of the
    # search window uses itself), as a live bot would.
    hist = np.arange(fr.index(SEARCH[0]), b - h, h)
    hist = hist[np.isfinite(pred[hist])]
    hist_month = (fr.open_ms[hist] // 86_400_000).astype("datetime64[D]").astype("datetime64[M]")
    paths = {}
    for label, q in (("top10", 90), ("top20", 80), ("top30", 70), ("always", 0)):
        side = np.zeros(len(w))
        for mth in np.unique(month):
            m = month == mth
            past = np.abs(pred[hist[hist_month < mth]])
            ref = past if len(past) else np.abs(pred[w[m]])
            cut = np.percentile(ref, q) if q else 0.0
            side[m] = np.where(np.abs(pred[w[m]]) >= cut, np.sign(pred[w[m]]), 0.0)
        r = np.where(side > 0, long_r[w], np.where(side < 0, short_r[w], 0.0))
        cost = np.abs(np.diff(np.r_[0.0, side])) * COST_RT / 2      # pay only when the position changes
        net = r - cost
        paths[label] = {"net_per_day": float(net.sum() / days), "exposure": float(np.mean(side != 0)),
                        "turnover_per_day": float(np.abs(np.diff(np.r_[0.0, side])).sum() / days),
                        "net_t_daily": float(daily_t(net, fr.open_ms[w]))}
    return {"paths": paths, "ic": ss.spearman(pred[w], long_r[w]), "monthly_ic_mean": float(monthly.mean()),
            "monthly_ic_t": float(monthly.mean() / monthly.std(ddof=1) * math.sqrt(len(monthly))),
            "windows": int(len(w)), "trades": int(len(gross)), "hit": float(np.mean(gross > 0)),
            "gross": float(gross.mean()), "net": float(gross.mean() - COST_RT),
            "net_t": float((gross - COST_RT).mean() / gross.std(ddof=1) * math.sqrt(len(gross))),
            "net_per_day": float((gross - COST_RT).sum() / days)}


def daily_t(net, open_ms):
    day = open_ms // 86_400_000
    s = pd.Series(net).groupby(day).sum()
    return s.mean() / s.std(ddof=1) * math.sqrt(len(s)) if len(s) > 2 and s.std() > 0 else 0.0


def show(name, r):
    p = r["paths"]
    held = " ".join(f"{k} {v['net_per_day']*100:+.3f}%/d(t{v['net_t_daily']:+.1f})" for k, v in p.items())
    print(f"{name:44} IC {r['ic']:+.4f} (t {r['monthly_ic_t']:+.2f}) hit {r['hit']*100:4.1f}% gross {r['gross']*100:+.3f}% "
          f"net {r['net']*100:+.3f}% | held: {held}", flush=True)


def run_stage(stage, configs, external=None):
    d = Data(external)
    out = {}
    for cfg in configs:
        name = "|".join(f"{k}={v}" for k, v in cfg.items())
        pred = walk_forward(d, cfg, SEARCH)
        out[name] = {"config": cfg, **evaluate(d, pred, cfg["h"], SEARCH)}
        show(name, out[name])
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / f"{stage}.json").write_text(json.dumps(out, indent=1, default=float))
    return out


STAGES = {
    "horizon": [{"h": h, "features": f, "target": "ret", "params": "base"}
                for h in (12, 24, 48, 96, 288) for f in ("A", "D")],
    "method": [{"h": h, "features": f, "target": t, "params": p, "seeds": n}
               for h in (48, 96) for f in ("A", "D")
               for t, p, n in (("ret", "base", 1), ("sign", "base", 1), ("big", "base", 1),
                               ("ret", "slow", 1), ("ret", "base", 5))],
    "external": [{"h": h, "features": f, "target": "ret", "params": "base"}
                 for h in (48, 96) for f in ("D", "DX")],
}
EXTERNAL_STAGES = {"external"}

# Declared on 2026-09-26 after the search stages and before any holdout run.
FINALISTS = {
    "F1_primary": {"h": 96, "features": "D", "target": "ret", "params": "base", "seeds": 5},
    "F2": {"h": 96, "features": "D", "target": "ret", "params": "base", "seeds": 1},
    "F3": {"h": 48, "features": "DX", "target": "ret", "params": "base", "seeds": 1},
    "ref_A_8h": {"h": 96, "features": "A", "target": "ret", "params": "base", "seeds": 1},
}
ADOPTION = ("F1_primary 'always' held path on the holdout: net per day > 0, above both always-long and "
            "the 4h EMA20/80 sign, and daily t > 1")


def holdout(output=OUTPUT):
    """Run once: walk forward through the holdout and report finalists and benchmarks there."""
    d = Data(external=True)
    window = (SEARCH[0], HOLDOUT[1])
    out = {"declared": FINALISTS, "adoption_rule": ADOPTION, "results": {}}
    for name, cfg in FINALISTS.items():
        pred = walk_forward(d, cfg, window)
        out["results"][name] = {"config": cfg, **evaluate(d, pred, cfg["h"], HOLDOUT)}
        show(name, out["results"][name])
    longer = ms.long_features(d.fr, {"vol_288": d.vol288})
    for name, score in (("bench_always_long", np.ones(d.fr.n)), ("bench_ema_4h_sign", longer["ema_4h_gap"])):
        for h in (48, 96):
            key = f"{name}_h{h}"
            out["results"][key] = evaluate(d, score, h, HOLDOUT)
            show(key, out["results"][key])
    f1 = out["results"]["F1_primary"]["paths"]["always"]
    bench = max(out["results"][f"bench_always_long_h96"]["paths"]["always"]["net_per_day"],
                out["results"][f"bench_ema_4h_sign_h96"]["paths"]["always"]["net_per_day"])
    out["adopt"] = bool(f1["net_per_day"] > 0 and f1["net_per_day"] > bench and f1["net_t_daily"] > 1)
    print("ADOPT F1_primary:", out["adopt"], flush=True)
    output.mkdir(parents=True, exist_ok=True)
    (output / "holdout.json").write_text(json.dumps(out, indent=1, default=float))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("stage", choices=sorted(STAGES) + ["holdout"])
    args = parser.parse_args(argv)
    if args.stage == "holdout":
        holdout()
    else:
        run_stage(args.stage, STAGES[args.stage], external=args.stage in EXTERNAL_STAGES)


if __name__ == "__main__":
    main()
