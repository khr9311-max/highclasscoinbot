"""Pre-registered test of short-cycle strategy switching by market situation.

Question: if the position is re-decided every 5 minutes from the current
situation (trend, volatility, taker flow, open interest, positioning, funding),
does the switching beat trading costs on BTCUSD_PERP?

Everything below is fixed before the results are read:
- DEV 2021-07-20..2024-07-01 chooses; HOLDOUT 2024-07-01..2026-09-25 only reports.
  The holdout was already used for other rules on 2026-09-25, but none of the
  flow/positioning features here were examined there.
- Decision at each 5m close, position held from that close (run_positions
  convention). An opposite signal flips at once; a neutral signal exits only
  after the minimum hold H. A repeated signal extends the hold.
- Families:
  table_H      36 situation cells (trend x volatility x taker flow x OI change),
               action per cell from DEV: |mean forward H return| > round-trip cost
               and non-overlapping t > 2.
  table_loose  same cells, |mean| > cost only.
  lgbm_H       LightGBM regression of the forward H return, retrained every month
               on all earlier data (purged), first prediction 2022-07. Trades when
               |prediction| > round-trip cost.
  gemini_H     the hourly Gemini report's score: price vs daily open and VWAP,
               OI build, taker B/S 5m/15m/1h, top-trader position L/S change,
               funding sign. Enter at |score| >= 4.
  cm02_H       the report ledger's rule CM02: long when taker B/S 5m and 15m
               > 1.15 and 1h OI rises.
- Costs per side: taker = fee 0.05% + 3 bps + half tick (same as
  strategy_search), taker_x2, maker_optimistic = 0.02% with fills assumed at the
  decision price (an upper bound), zero (gross edge).
- Flow and positioning come from USDT-M BTCUSDT (see flow_data). Metrics rows
  are used one bar after their snapped create_time (10 minutes after it).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from btc_lab import strategy_search as ss

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "btc_lab/state/flow_market/flow_5m.npz"
OUTPUT = ROOT / "btc_lab/state/regime_switch_20260926"
DEV = ("2021-07-20", "2024-07-01")
WALK = ("2022-07-01", "2024-07-01")
HOLDOUT = ("2024-07-01", "2026-09-25")
HORIZONS = (6, 12, 24)
GEMINI_HORIZONS = (12, 48)
DAY = 288
MAKER_FEE = 0.0002
LGBM_PARAMS = dict(n_estimators=300, learning_rate=0.03, num_leaves=31, min_child_samples=2000,
                   subsample=0.5, subsample_freq=1, colsample_bytree=0.7, reg_lambda=1.0,
                   random_state=7, verbose=-1)
TRAIN_STEP = 3


def stamp(text):
    return ss.stamp(text)


# ---------------------------------------------------------------- data

class Frame:
    """BTCUSD_PERP prices, funding and flow on the flow_data 5m grid."""

    def __init__(self, flow_path=FLOW, market_dir=ss.MARKET, funding_path=ss.FUNDING):
        flow = np.load(flow_path)
        self.open_ms = flow["open_ms"]
        self.n = len(self.open_ms)
        z = np.load(market_dir / "BTCUSD_PERP_5m.npz")
        px = pd.DataFrame({k: z[k] for k in ("open", "high", "low", "close", "volume")},
                          index=z["open_ms"]).reindex(self.open_ms)
        close = px["close"].ffill().bfill()
        for k in ("open", "high", "low"):
            px[k] = px[k].fillna(close)
        px["close"] = close
        self.px = {k: px[k].to_numpy() for k in ("open", "high", "low", "close")}
        self.flow = {k: flow[k] for k in flow.files if k != "open_ms"}
        self.funding = np.zeros(self.n)            # charged in the bar containing the funding time
        self.funding_known = np.full(self.n, np.nan)
        rows = pd.read_csv(funding_path)
        t0 = int(self.open_ms[0])
        for t, rate in zip(rows["funding_time_ms"], rows["funding_rate"]):
            i = (int(t) - t0) // 300_000
            if 0 <= i < self.n:
                self.funding[i] += float(rate)
                self.funding_known[i] = float(rate)
        self.funding_known = pd.Series(self.funding_known).ffill().to_numpy()

    def index(self, text):
        return int(np.clip((stamp(text) - int(self.open_ms[0])) // 300_000, 0, self.n))


# ---------------------------------------------------------------- features

def rolling_sum(x, n):
    return pd.Series(x).rolling(n, min_periods=n).sum().to_numpy()


def lag(x, k=1):
    out = np.full(len(x), np.nan)
    out[k:] = x[:-k]
    return out


def log_change(x, k):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(x / lag(x, k))


def features(fr):
    """Values known at the close of each 5m bar (index i uses bars <= i)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return _features(fr)


def _features(fr):
    c, h, low = fr.px["close"], fr.px["high"], fr.px["low"]
    lr = np.diff(np.log(c), prepend=np.nan)
    f = {}
    for k in (1, 3, 12, 48, 288):
        f[f"ret_{k}"] = log_change(c, k)
    vol12 = pd.Series(lr).rolling(12).std().to_numpy()
    vol288 = pd.Series(lr).rolling(288).std().to_numpy()
    f["vol_12"], f["vol_288"] = vol12, vol288
    f["vol_ratio"] = vol12 / vol288
    f["trend_z"] = f["ret_12"] / (vol288 * math.sqrt(12))
    path12 = rolling_sum(np.abs(np.diff(c, prepend=np.nan)), 12)
    f["er_12"] = np.abs(c - lag(c, 12)) / path12
    path48 = rolling_sum(np.abs(np.diff(c, prepend=np.nan)), 48)
    f["er_48"] = np.abs(c - lag(c, 48)) / path48
    hi48 = pd.Series(h).rolling(48).max().to_numpy()
    lo48 = pd.Series(low).rolling(48).min().to_numpy()
    f["range_pos_48"] = (c - lo48) / np.where(hi48 > lo48, hi48 - lo48, np.nan)
    day = (fr.open_ms // 86_400_000).astype(np.int64)
    first = np.r_[True, day[1:] != day[:-1]]
    day_open = pd.Series(np.where(first, fr.px["open"], np.nan)).ffill().to_numpy()
    cm_vol = np.nan_to_num(fr.flow["cm_volume"])
    typical = (h + low + c) / 3
    cum_pv = pd.Series(typical * cm_vol).groupby(day).cumsum().to_numpy()
    cum_v = pd.Series(cm_vol).groupby(day).cumsum().to_numpy()
    vwap = np.where(cum_v > 0, cum_pv / np.where(cum_v > 0, cum_v, 1), c)
    f["dist_open"] = np.log(c / day_open) / vol288
    f["dist_vwap"] = np.log(c / vwap) / vol288
    f["above_open"] = np.sign(c - day_open)
    f["above_vwap"] = np.sign(c - vwap)
    f["volume_z"] = np.log(rolling_sum(cm_vol, 12) / (rolling_sum(cm_vol, 288) / 24))
    for venue in ("cm", "um"):
        vol = fr.flow[f"{venue}_volume"]
        buy = fr.flow[f"{venue}_taker_buy"]
        for k in (1, 3, 12):
            b, v = rolling_sum(buy, k), rolling_sum(vol, k)
            with np.errstate(divide="ignore", invalid="ignore"):
                f[f"{venue}_flow_{k}"] = b / v - 0.5
                f[f"{venue}_bs_{k}"] = b / (v - b)
    metric = {k: lag(pd.Series(fr.flow[f"um_{k}"]).ffill(limit=3).to_numpy())
              for k in ("oi", "top_account_ls", "top_position_ls", "global_ls")}
    for k in (1, 3, 12, 48):
        f[f"doi_{k}"] = log_change(metric["oi"], k)
    for k in ("top_account_ls", "top_position_ls", "global_ls"):
        f[k] = np.log(metric[k])
        f[f"d_{k}_12"] = f[k] - lag(f[k], 12)
    f["funding"] = fr.funding_known
    prem = fr.flow["premium_index"]
    f["premium"] = prem
    f["premium_12"] = pd.Series(prem).rolling(12).mean().to_numpy()
    minute = (fr.open_ms // 60_000 + 5) % 1440          # at the bar close
    f["hour_sin"] = np.sin(2 * np.pi * minute / 1440)
    f["hour_cos"] = np.cos(2 * np.pi * minute / 1440)
    f["to_funding"] = ((480 - minute % 480) % 480) / 480
    return f


def forward_return(close, h):
    """Inverse long return from the close of bar i to the close of bar i+h (BTC)."""
    out = np.full(len(close), np.nan)
    out[:-h] = 1 - close[:-h] / close[h:]
    return out


# ---------------------------------------------------------------- engine

def hold_path(signal, h):
    """Positions held over each bar from signals decided at the prior closes.

    Opposite signals flip immediately; a neutral signal leaves only after the
    minimum hold h; a repeated signal extends the hold.
    """
    s = np.nan_to_num(np.asarray(signal, dtype=float)).astype(np.int8)
    target = np.zeros(len(s), dtype=np.int8)
    cur, until = 0, -1
    for i in range(len(s)):
        v = s[i]
        if cur != 0 and i < until:
            if v == -cur:
                cur, until = v, i + h
        elif v != 0:
            until = i + h
            cur = v
        else:
            cur = 0
        target[i] = cur
    pos = np.zeros(len(s))
    pos[1:] = target[:-1]
    return pos


def per_side(kind, close):
    if kind == "taker":
        return ss.per_side_cost("BTCUSD_PERP", close, 1.0)
    if kind == "taker_x2":
        return ss.per_side_cost("BTCUSD_PERP", close, 2.0)
    if kind == "maker_optimistic":
        return np.full(len(close), MAKER_FEE)
    if kind == "zero":
        return np.zeros(len(close))
    raise ValueError(kind)


COSTS = ("taker", "taker_x2", "maker_optimistic", "zero")


def path_returns(close, funding, pos, cost):
    prev = np.concatenate(([close[0]], close[:-1]))
    r = pos * (1 - prev / close) - pos * funding
    return r - np.abs(np.diff(pos, prepend=0.0)) * cost


def period_metrics(fr, r, pos, trades, period):
    a, b = fr.index(period[0]), fr.index(period[1])
    rr = r[a:b]
    equity = np.cumprod(1 + rr)
    peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:]
    years = (b - a) / DAY / 365.25
    sel = (trades.entry_bar >= a) & (trades.entry_bar < b)
    rets = trades.ret[sel]
    hold = (trades.exit_bar[sel] - trades.entry_bar[sel] + 1)
    n = len(rets)
    turnover = np.abs(np.diff(pos, prepend=0.0))[a:b].sum()
    return {"return": float(equity[-1] - 1) if len(equity) else 0.0,
            "cagr": float(equity[-1] ** (1 / years) - 1) if len(equity) and equity[-1] > 0 else -1.0,
            "max_dd": float(np.max(1 - equity / peak)) if len(equity) else 0.0,
            "trades": int(n), "trades_per_day": float(n / max((b - a) / DAY, 1)),
            "mean_trade": float(rets.mean()) if n else None,
            "win_rate": float((rets > 0).mean()) if n else None,
            "t_stat": float(rets.mean() / rets.std(ddof=1) * math.sqrt(n)) if n > 2 and rets.std() > 0 else None,
            "mean_hold_min": float(hold.mean() * 5) if n else None,
            "exposure": float(np.mean(pos[a:b] != 0)), "turnover_per_day": float(turnover / max((b - a) / DAY, 1))}


def evaluate(fr, pos):
    close = fr.px["close"]
    out = {}
    for kind in COSTS:
        r = path_returns(close, fr.funding, pos, per_side(kind, close))
        trades = ss.position_trades(r, pos, 1)
        out[kind] = {label: period_metrics(fr, r, pos, trades, period)
                     for label, period in (("dev", DEV), ("walk", WALK), ("holdout", HOLDOUT))}
    return out


# ---------------------------------------------------------------- candidates

def terciles(x, mask):
    lo, hi = np.nanpercentile(x[mask], [100 / 3, 200 / 3])
    return np.where(np.isnan(x), np.nan, np.where(x < lo, -1, np.where(x > hi, 1, 0))), [float(lo), float(hi)]


def situation_cells(f, dev_mask):
    """36 cells from DEV thresholds: trend x volatility x taker flow x OI change."""
    trend, t_cut = terciles(f["trend_z"], dev_mask)
    flow, f_cut = terciles(f["um_flow_3"], dev_mask)
    v_cut = float(np.nanmedian(f["vol_ratio"][dev_mask]))
    vol = np.where(np.isnan(f["vol_ratio"]), np.nan, (f["vol_ratio"] > v_cut).astype(float))
    oi = np.where(np.isnan(f["doi_12"]), np.nan, (f["doi_12"] > 0).astype(float))
    cell = (trend + 1) * 12 + vol * 6 + (flow + 1) * 2 + oi
    return cell, {"trend_z": t_cut, "um_flow_3": f_cut, "vol_ratio": v_cut, "doi_12": 0.0}


def non_overlapping(idx, h):
    """Greedy subset of sorted bar indices whose forward windows of h bars do not overlap."""
    keep, nxt = [], -1
    for i in idx:
        if i >= nxt:
            keep.append(i)
            nxt = i + h
    return np.array(keep, dtype=np.int64)


def cell_table(cell, fwd, h, dev_idx, cost_rt, need_t):
    actions, stats = {}, {}
    for c in range(36):
        idx = dev_idx[(cell[dev_idx] == c) & ~np.isnan(fwd[dev_idx])]
        if len(idx) == 0:
            actions[c] = 0
            continue
        sparse = non_overlapping(idx, h)
        m = float(fwd[idx].mean())
        s = fwd[sparse]
        sd = s.std(ddof=1) if len(s) > 2 else np.nan
        t_long = (s.mean() - cost_rt) / sd * math.sqrt(len(s)) if sd > 0 else 0.0
        t_short = (-s.mean() - cost_rt) / sd * math.sqrt(len(s)) if sd > 0 else 0.0
        act = 0
        if m > cost_rt and (not need_t or t_long > 2):
            act = 1
        elif -m > cost_rt and (not need_t or t_short > 2):
            act = -1
        actions[c] = act
        stats[c] = {"bars": int(len(idx)), "mean": m, "t_long": float(t_long), "t_short": float(t_short)}
    return actions, stats


def run_lengths(label):
    x = np.asarray(label)
    ok = ~np.isnan(x)
    change = np.flatnonzero(np.r_[True, x[1:] != x[:-1]] & ok)
    runs = np.diff(np.r_[change, len(x)])
    return runs


def lgbm_predictions(fr, f, fwd, h, names):
    import lightgbm as lgb
    x = np.column_stack([f[k] for k in names]).astype(np.float32)
    pred = np.full(fr.n, np.nan)
    start = fr.index(DEV[0]) + DAY
    months = pd.date_range(WALK[0], HOLDOUT[1], freq="MS", tz="UTC")
    edges = [fr.index(m.strftime("%Y-%m-%d")) for m in months] + [fr.index(HOLDOUT[1])]
    fit_log = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        stop = a - h - DAY                                   # purge label overlap plus one day
        idx = np.arange(start, stop, TRAIN_STEP)
        idx = idx[~np.isnan(fwd[idx])]
        y = fwd[idx]
        lo, hi = np.percentile(y, [0.5, 99.5])
        model = lgb.LGBMRegressor(**LGBM_PARAMS)
        model.fit(x[idx], np.clip(y, lo, hi))
        pred[a:b] = model.predict(x[a:b])
        fit_log.append({"month_start": int(fr.open_ms[a]), "train_rows": int(len(idx))})
    return pred, fit_log


def rank_ic(pred, fwd, a, b, h):
    idx = np.arange(a, b, h)
    ok = ~np.isnan(pred[idx]) & ~np.isnan(fwd[idx])
    if ok.sum() < 30:
        return None
    return ss.spearman(pred[idx][ok], fwd[idx][ok])


def decile_table(pred, fwd, a, b, h):
    idx = np.arange(a, b, h)
    ok = ~np.isnan(pred[idx]) & ~np.isnan(fwd[idx])
    p, y = pred[idx][ok], fwd[idx][ok]
    edges = np.percentile(p, np.linspace(0, 100, 11))
    bucket = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, 9)
    return [{"decile": d + 1, "pred_mean": float(p[bucket == d].mean()), "realized_mean": float(y[bucket == d].mean()),
             "n": int((bucket == d).sum())} for d in range(10)]


def gemini_score(f):
    price = np.where((f["above_open"] > 0) & (f["above_vwap"] > 0), 1,
                     np.where((f["above_open"] < 0) & (f["above_vwap"] < 0), -1, 0))
    build = f["doi_12"] > 0
    oi = np.where(build & (f["ret_12"] > 0), 1, np.where(build & (f["ret_12"] < 0), -1, 0))
    bs = np.vstack([f["um_bs_1"], f["um_bs_3"], f["um_bs_12"]])
    taker = np.where(np.all(bs > 1.05, 0), 2, np.where(np.all(bs < 1 / 1.05, 0), -2, 0))
    top = np.sign(np.nan_to_num(f["d_top_position_ls_12"]))
    funding = -np.sign(np.nan_to_num(f["funding"]))
    score = price + oi + taker + top + funding
    valid = ~(np.isnan(f["doi_12"]) | np.isnan(bs).any(0) | np.isnan(f["vol_288"]))
    return np.where(valid, score, 0)


def cm02_signal(f):
    ok = (f["um_bs_1"] > 1.15) & (f["um_bs_3"] > 1.15) & (f["doi_12"] > 0)
    return ok.astype(float)


# ---------------------------------------------------------------- study

LGBM_FEATURES = ("ret_1", "ret_3", "ret_12", "ret_48", "ret_288", "vol_12", "vol_288", "vol_ratio", "trend_z",
                 "er_12", "er_48", "range_pos_48", "dist_open", "dist_vwap", "volume_z",
                 "cm_flow_1", "cm_flow_3", "cm_flow_12", "um_flow_1", "um_flow_3", "um_flow_12",
                 "doi_1", "doi_3", "doi_12", "doi_48", "top_account_ls", "top_position_ls", "global_ls",
                 "d_top_account_ls_12", "d_top_position_ls_12", "d_global_ls_12", "funding", "premium",
                 "premium_12", "hour_sin", "hour_cos", "to_funding")


def summarize(name, family, h, result, extra=None):
    row = {"name": name, "family": family, "h_bars": h, "results": result}
    if extra:
        row.update(extra)
    t, d, x2 = result["taker"]["holdout"], result["taker"]["dev"], result["taker_x2"]["holdout"]
    z, mk = result["zero"]["holdout"], result["maker_optimistic"]["holdout"]
    print(f"{name:16} dev {d['return']:+9.2%} | holdout taker {t['return']:+9.2%} x2 {x2['return']:+9.2%} "
          f"maker* {mk['return']:+9.2%} zero {z['return']:+9.2%} | trades/day {t['trades_per_day']:5.2f} "
          f"gross/trade {z['mean_trade'] if z['mean_trade'] is not None else float('nan'):+.4%}", flush=True)
    return row


def study(output=OUTPUT):
    fr = Frame()
    f = features(fr)
    close = fr.px["close"]
    cost_rt = float(2 * np.nanmedian(per_side("taker", close)))
    a_dev, b_dev = fr.index(DEV[0]), fr.index(DEV[1])
    a_ho, b_ho = fr.index(HOLDOUT[0]), fr.index(HOLDOUT[1])
    dev_mask = np.zeros(fr.n, bool)
    dev_mask[a_dev:b_dev] = True
    dev_idx = np.arange(a_dev, b_dev)
    cell, cuts = situation_cells(f, dev_mask)
    runs_dev = run_lengths(cell[a_dev:b_dev])
    runs_ho = run_lengths(cell[a_ho:b_ho])
    diagnostics = {"cost_round_trip": cost_rt, "cell_thresholds": cuts,
                   "cell_run_bars": {"dev_median": float(np.median(runs_dev)), "dev_mean": float(runs_dev.mean()),
                                     "holdout_median": float(np.median(runs_ho)),
                                     "changes_per_day_holdout": float(len(runs_ho) / ((b_ho - a_ho) / DAY))},
                   "horizons": {}}
    rows = []
    for h in HORIZONS:
        fwd = forward_return(close, h)
        diag = {}
        for need_t, label in ((True, "table"), (False, "table_loose")):
            actions, stats = cell_table(cell, fwd, h, dev_idx, cost_rt, need_t)
            signal = np.array([actions.get(int(c), 0) if not np.isnan(c) else 0 for c in cell])
            res = evaluate(fr, hold_path(signal, h))
            rows.append(summarize(f"{label}_{h}", label, h, res, {"actions": actions}))
            if need_t:
                diag["cells_dev"] = stats
        # Does the situation -> mean forward return mapping carry into the holdout?
        dev_means, ho_means = [], []
        for c in range(36):
            i_d = dev_idx[(cell[dev_idx] == c) & ~np.isnan(fwd[dev_idx])]
            ho_idx = np.arange(a_ho, b_ho - h)
            i_h = ho_idx[cell[ho_idx] == c]
            if len(i_d) > 500 and len(i_h) > 500:
                dev_means.append(float(fwd[i_d].mean()))
                ho_means.append(float(fwd[i_h].mean()))
        diag["cell_mean_rank_corr_dev_holdout"] = ss.spearman(dev_means, ho_means) if len(dev_means) > 5 else None
        diag["cell_abs_mean_max_dev"] = float(np.max(np.abs(dev_means))) if dev_means else None
        diag["cell_abs_mean_max_holdout"] = float(np.max(np.abs(ho_means))) if ho_means else None
        diag["cells_holdout_beyond_cost"] = int(np.sum(np.abs(ho_means) > cost_rt))
        pred, fit_log = lgbm_predictions(fr, f, fwd, h, LGBM_FEATURES)
        output.mkdir(parents=True, exist_ok=True)
        np.save(output / f"lgbm_pred_{h}.npy", pred)
        signal = np.where(pred > cost_rt, 1, np.where(pred < -cost_rt, -1, 0))
        res = evaluate(fr, hold_path(signal, h))
        a_w, b_w = fr.index(WALK[0]), fr.index(WALK[1])
        diag["lgbm"] = {"rank_ic_walk": rank_ic(pred, fwd, a_w, b_w, h), "rank_ic_holdout": rank_ic(pred, fwd, a_ho, b_ho, h),
                        "deciles_holdout": decile_table(pred, fwd, a_ho, b_ho - h, h),
                        "share_beyond_cost_holdout": float(np.mean(np.abs(pred[a_ho:b_ho]) > cost_rt)),
                        "fits": len(fit_log)}
        rows.append(summarize(f"lgbm_{h}", "lgbm", h, res))
        diagnostics["horizons"][h] = diag
    score = gemini_score(f)
    diagnostics["gemini_score"] = {"share_long_signal": float(np.mean(score[a_ho:b_ho] >= 4)),
                                   "share_short_signal": float(np.mean(score[a_ho:b_ho] <= -4)),
                                   "sign_changes_per_day_holdout": float(
                                       np.sum(np.diff(np.sign(score[a_ho:b_ho])) != 0) / ((b_ho - a_ho) / DAY))}
    for h in GEMINI_HORIZONS:
        signal = np.where(score >= 4, 1, np.where(score <= -4, -1, 0))
        rows.append(summarize(f"gemini_{h}", "gemini", h, evaluate(fr, hold_path(signal, h))))
        rows.append(summarize(f"cm02_{h}", "cm02", h, evaluate(fr, hold_path(cm02_signal(f), h))))
    verdict = {"passes_holdout": [r["name"] for r in rows
                                  if r["results"]["taker"]["holdout"]["return"] > 0
                                  and r["results"]["taker_x2"]["holdout"]["return"] > 0
                                  and r["results"]["taker"]["holdout"]["trades"] >= 100]}
    output.mkdir(parents=True, exist_ok=True)
    report = {"periods": {"dev": DEV, "walk": WALK, "holdout": HOLDOUT}, "horizons": HORIZONS,
              "lgbm_params": LGBM_PARAMS, "lgbm_features": LGBM_FEATURES, "diagnostics": diagnostics,
              "verdict": verdict, "rows": rows,
              "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (output / "results.json").write_text(json.dumps(report, indent=1, default=float))
    return report


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    report = study()
    print(json.dumps({"diagnostics": {k: v for k, v in report["diagnostics"].items() if k != "horizons"},
                      "verdict": report["verdict"]}, indent=1, default=float))
    for h, d in report["diagnostics"]["horizons"].items():
        print(h, {k: v for k, v in d.items() if k not in ("cells_dev", "lgbm")})
        print("  lgbm", {k: v for k, v in d["lgbm"].items() if k != "deciles_holdout"})
        for row in d["lgbm"]["deciles_holdout"]:
            print("   ", row)


if __name__ == "__main__":
    main()
