"""Pre-registered test: can the learned short-term model time trades the swing rule makes anyway?

A trade that must happen (buy or sell) at a 4h close s may wait up to W bars.
It executes at the first 5m close t in [s, s+W] where the model favours it
(buy: prediction >= 0, sell: prediction <= 0), else at s+W. No extra trades are
added, so the cost question differs from regime_switch.

Part 1, rule value: every 4h close in a period is a hypothetical buy and a
hypothetical sell. Improvement is measured against executing at once; the
buy/sell average cancels market drift. Pass: side-averaged improvement > 0
with t > 2 in both the walk (2022-07..2024-06) and holdout periods.
Part 2, application: the swing COIN-M signal (4h EMA20/80 long/short, flips at
4h closes, no stops) with each flip timed by the rule, versus immediate flips,
under taker costs.

Predictions are the monthly walk-forward LightGBM outputs saved by
regime_switch (lgbm_pred_12 / lgbm_pred_24); none exist before 2022-07.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np

from btc_lab import regime_switch as rs
from btc_lab import strategy_search as ss

OUTPUT = rs.OUTPUT
MODELS = (12, 24)
WINDOWS = (12, 48)
FOUR_H = 48


def timed_index(pred, s, w, side):
    """First bar in [s, s+w] where the model favours the side (+1 buy, -1 sell)."""
    window = pred[s:s + w + 1]
    ok = window >= 0 if side > 0 else window <= 0
    ok &= ~np.isnan(window)
    hit = np.flatnonzero(ok)
    return s + int(hit[0]) if len(hit) else s + w


def improvement(close, s, t, side):
    """Price gain from executing at t instead of s, as a fraction (positive = better)."""
    return (close[s] - close[t]) / close[s] if side > 0 else (close[t] - close[s]) / close[s]


def four_hour_closes(fr, a, b):
    """Indices of 5m bars whose close is a 4h boundary."""
    idx = np.arange(a, b)
    return idx[((fr.open_ms[idx] // 300_000 + 1) % FOUR_H) == 0]


def rule_value(fr, pred, w, period):
    a, b = fr.index(period[0]), fr.index(period[1])
    close = fr.px["close"]
    starts = four_hour_closes(fr, a, b - w - 1)
    starts = starts[~np.isnan(pred[starts])]
    out = {}
    both = []
    for side, label in ((1, "buy"), (-1, "sell")):
        ts = np.array([timed_index(pred, s, w, side) for s in starts])
        imp = np.array([improvement(close, s, t, side) for s, t in zip(starts, ts)])
        both.append(imp)
        out[label] = {"n": int(len(imp)), "mean_bps": float(imp.mean() * 1e4),
                      "t": float(imp.mean() / imp.std(ddof=1) * math.sqrt(len(imp))),
                      "immediate_share": float(np.mean(ts == starts)), "mean_wait_min": float((ts - starts).mean() * 5)}
    avg = (both[0] + both[1]) / 2
    out["side_average"] = {"mean_bps": float(avg.mean() * 1e4),
                           "t": float(avg.mean() / avg.std(ddof=1) * math.sqrt(len(avg)))}
    return out


def ema_flip_target(fr):
    """4h EMA20/80 direction known at each 4h close, held until the next 4h close."""
    close = fr.px["close"]
    closes = four_hour_closes(fr, 0, fr.n)
    c4 = close[closes]
    direction = np.sign(ss.ema(c4, 20) - ss.ema(c4, 80))
    direction[:80] = 0
    target = np.zeros(fr.n)
    for k, i in enumerate(closes):
        end = closes[k + 1] if k + 1 < len(closes) else fr.n
        target[i:end] = direction[k]                 # decided at close i
    return target, closes


def timed_target(target, pred, w):
    """Delay each change of target until the model favours the trade (deadline w bars)."""
    out = target.copy()
    changes = np.flatnonzero(np.diff(target, prepend=target[0]) != 0)
    for k, s in enumerate(changes):
        side = 1 if target[s] > target[s - 1] else -1
        limit = changes[k + 1] - 1 if k + 1 < len(changes) else len(target) - 1
        t = min(timed_index(pred, s, w, side), limit)
        out[s:t] = target[s - 1]
    return out


def application(fr, pred, w):
    close = fr.px["close"]
    target, _ = ema_flip_target(fr)
    rows = {}
    for label, tgt in (("immediate", target), ("timed", timed_target(target, pred, w))):
        pos = np.r_[0.0, tgt[:-1]]
        r = rs.path_returns(close, fr.funding, pos, rs.per_side("taker", close))
        trades = ss.position_trades(r, pos, 1)
        rows[label] = {p: rs.period_metrics(fr, r, pos, trades, per) for p, per in (("walk", rs.WALK), ("holdout", rs.HOLDOUT))}
    rows["flips"] = {p: int(np.sum(np.diff(target[fr.index(per[0]):fr.index(per[1])]) != 0))
                     for p, per in (("walk", rs.WALK), ("holdout", rs.HOLDOUT))}
    return rows


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    fr = rs.Frame()
    report = {"models": MODELS, "windows": WINDOWS, "rows": []}
    for h in MODELS:
        pred = np.load(OUTPUT / f"lgbm_pred_{h}.npy")
        for w in WINDOWS:
            row = {"model_h": h, "window": w,
                   "walk": rule_value(fr, pred, w, rs.WALK), "holdout": rule_value(fr, pred, w, rs.HOLDOUT),
                   "application": application(fr, pred, w)}
            row["passes"] = all(row[p]["side_average"]["mean_bps"] > 0 and row[p]["side_average"]["t"] > 2
                                for p in ("walk", "holdout"))
            report["rows"].append(row)
            for p in ("walk", "holdout"):
                v = row[p]
                ap = row["application"]
                print(f"model {h:2d} W {w:2d} {p:7} buy {v['buy']['mean_bps']:+6.2f}bp (t {v['buy']['t']:+5.2f}) "
                      f"sell {v['sell']['mean_bps']:+6.2f}bp (t {v['sell']['t']:+5.2f}) avg {v['side_average']['mean_bps']:+6.2f}bp "
                      f"(t {v['side_average']['t']:+5.2f}) | swing immediate {ap['immediate'][p]['return']:+8.2%} "
                      f"timed {ap['timed'][p]['return']:+8.2%} flips {ap['flips'][p]}", flush=True)
    (OUTPUT / "timing_overlay.json").write_text(json.dumps(report, indent=1, default=float))


if __name__ == "__main__":
    main()
